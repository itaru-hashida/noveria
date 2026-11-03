"""Длинная парная проверка игрового учёта склада, без изменения денег и карты."""
import argparse
import concurrent.futures
import hashlib
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.economy import Economy


class LegacyEconomy(Economy):
    STOCK_PLANNING_REVISION = 0


def record(e):
    hs = [a for a in e.accounts.values() if a['kind'] == 'household']
    consumed, need = {}, {}
    for g in e.goods:
        consumed[g] = sum(a.get('needs', {}).get(g, {}).get('consumed', 0) for a in hs)
        need[g] = sum(a.get('needs', {}).get(g, {}).get('needed', 0) for a in hs)
    basic = e.cfg['basic_needs']
    total = sum(need[g]*e.goods[g]['price'] for g in basic)
    return dict(month=e.meta['month'], coverage=sum(consumed[g]*e.goods[g]['price'] for g in basic)/total,
        household_consumed=consumed, needs=need, output=e.report['production'], stocks=e.report['stocks'],
        household_cash=e.report['balances']['household'],
        household_net=sum(a['net'] for a in e.report['account_flows'] if a['kind'] == 'household'),
        open=e.report['open_enterprises'], starts=sum(d['outcome']=='started' for d in e.report['investment_decisions']),
        invariants=e.report['invariants'])


def run_one(checkpoint, out, months, variant):
    path, out = Path(checkpoint), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    e = pickle.loads(path.read_bytes())
    e.__class__ = Economy if variant == 'current' else LegacyEconomy
    initial = e.meta['month']
    metadata = dict(checkpoint_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), seed=e.seed,
        variant=variant, months=months, initial_month=initial, status='running',
        engine_sha256=hashlib.sha256(Path('modules/economy/v3.py').read_bytes()).hexdigest())
    try:
        with (out/'monthly.jsonl').open('w') as target:
            for i in range(months):
                e.step()
                r = record(e)
                assert all(r['invariants'].values())
                target.write(json.dumps(r, ensure_ascii=False)+'\n');target.flush()
                if (i+1)%60==0:print(out.name, e.meta['month'], round(r['coverage'],3), flush=True)
        # Состояние переносимо между процессами; LegacyEconomy служит только
        # сравнительным прогоном, следующие игровые шаги используют актуальный код.
        e.__class__ = Economy
        with (out/'final.pickle').open('wb') as target:pickle.dump(e,target)
        metadata['status']='completed'
    except BaseException as exc:
        metadata.update(status='failed',error=str(exc))
        raise
    finally:
        (out/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')


if __name__ == '__main__':
    if Economy.PROFIT_DISTRIBUTION_REVISION:
        raise ValueError('Историческая проверка склада: используйте commit d446f27 до изменения доходов.')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True)
    p.add_argument('--months',type=int,default=360)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--cases',help='JSON: название случая -> путь доверенного checkpoint')
    args=p.parse_args()
    if not 360<=args.months<=10000 or not 1<=args.workers<=8:
        p.error('Нужно 360..10000 месяцев и 1..8 процессов')
    cases={str(s):f'/workspace/audit-noveria/allocation-final/seed-{s}/engine-180.pickle' for s in (73129,17,2026)}
    cases['fresh-73129']='/workspace/input-bid-promotion/fresh/seed-73129/engine-180.pickle'
    if args.cases:
        cases=json.loads(Path(args.cases).read_text())
    jobs=[(path,str(Path(args.out)/(label+'-'+v)),args.months,v) for label,path in cases.items() for v in ('legacy','current')]
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(run_one,*j) for j in jobs]
        for f in futures:f.result()
