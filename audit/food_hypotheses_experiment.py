"""Проверка влияния владельцев и сырьевого буфера на хлебный дефицит."""
import csv, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.extend([str(ROOT),str(ROOT/'audit')])
import scenario_runner as sr
from modules.economy import Economy

def run(seed, mode, months=12):
    with tempfile.TemporaryDirectory() as tmp:
        db, service=sr.build_world(seed,tmp)
        original_owner=Economy.owner_requests; original_step=Economy.step
        if mode=='owners_off': Economy.owner_requests=lambda self,sells: None
        if mode=='inputs_buffer':
            def buffered_step(self):
                for e in self.enterprises.values():
                    if e['good'] in ('flour','bread'):
                        self.add_stock(e['id'],'grain',5000,0)
                        self.add_stock(e['id'],'flour',5000,0)
                return original_step(self)
            Economy.step=buffered_step
        rows=[]
        try:
            for _ in range(months):
                service.advance_turn(command_id=f'hyp-{mode}-{seed}-{len(rows)+1}',source='audit')
                engine=service.economy_state().engine; report=service.economy_state().last_report
                rows.append(sr.row_for(engine,report,mode,seed))
        finally:
            Economy.owner_requests=original_owner; Economy.step=original_step; db.close()
        return rows

def main():
    out=ROOT/'audit/_runs/current/hypotheses.csv'; records=[]
    for mode in ('baseline','owners_off','inputs_buffer'):
        for seed in (17,2026):
            rows=run(seed,mode)
            rows=[r for r in rows if r['month']>=6]
            avg=lambda k:sum(float(r[k]) for r in rows)/len(rows)
            records.append(dict(mode=mode,seed=seed,deficit_bread=round(avg('deficit_bread'),1),prod_bread=round(avg('prod_bread'),1),prod_grain=round(avg('prod_grain'),1),prod_flour=round(avg('prod_flour'),1)))
    with out.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(records[0])); w.writeheader(); w.writerows(records)
    print('written',out)
if __name__=='__main__': main()
