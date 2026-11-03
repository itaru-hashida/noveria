"""Проверка переноса игрового дохода по четырём архивным парным опытам."""
import concurrent.futures
import hashlib
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from modules.economy import Economy
from scripts.validate_stock_planning import record


def run(case):
    label,checkpoint,archive=case
    e=pickle.loads(Path(checkpoint).read_bytes());e.__class__=Economy
    rows=[json.loads(l) for l in Path(archive).read_text().splitlines()][:60]
    for old in rows:
        e.step();r=record(e)
        for k in ('coverage','household_consumed','output','stocks','household_cash','household_net','open','starts'):
            assert r[k]==old[k],(label,e.meta['month'],k)
        assert all(r['invariants'].values())
    return dict(case=label,months=60,exact_numeric_parity=True,
        checkpoint_sha256=hashlib.sha256(Path(checkpoint).read_bytes()).hexdigest())


if __name__=='__main__':
    cases=[('pilot','/workspace/input-bid-promotion/fresh/seed-73129/engine-180.pickle',
        '/workspace/money-cycle-pilot/profit_income/monthly.jsonl')]
    cases += [(str(s),f'/workspace/audit-noveria/allocation-final/seed-{s}/engine-180.pickle',
        f'/workspace/money-cycle-validation/{s}-profit_income/monthly.jsonl') for s in (73129,17,2026)]
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as p:results=list(p.map(run,cases))
    Path('docs/research/business_income_promotion.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
