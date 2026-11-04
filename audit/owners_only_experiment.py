"""Изолированный эксперимент: отключить потребительские заявки владельцев."""
import csv, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.extend([str(ROOT),str(ROOT/'audit')])
import scenario_runner as sr
from modules.economy import Economy

def run(seed, owners_off, months=12):
    with tempfile.TemporaryDirectory() as tmp:
        db, service=sr.build_world(seed,tmp)
        original=Economy.owner_requests
        if owners_off: Economy.owner_requests=lambda self,sells: None
        rows=[]
        try:
            for step in range(1,months+1):
                service.advance_turn(command_id=f'owners-{seed}-{owners_off}-{step}',source='audit')
                engine=Economy.load(db); report=db.economy_month(engine.meta['month'])
                rows.append(sr.row_for(engine,report,'owners_off' if owners_off else 'baseline',seed))
        finally:
            Economy.owner_requests=original; db.close()
        return rows

def main():
    records=[]
    for mode, off in (('baseline',False),('owners_off',True)):
        for seed in (17,2026):
            rows=[r for r in run(seed,off) if r['month']>=6]
            avg=lambda k:sum(float(r[k]) for r in rows)/len(rows)
            records.append(dict(mode=mode,seed=seed,deficit_bread=round(avg('deficit_bread'),1),prod_bread=round(avg('prod_bread'),1),cons_bread=round(avg('cons_bread'),1)))
    out=ROOT/'audit/_runs/current/owners_only.csv'; out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(records[0])); w.writeheader(); w.writerows(records)
    print('written',out)
if __name__=='__main__': main()
