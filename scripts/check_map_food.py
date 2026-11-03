"""Физическая выполнимость хлебной цепочки на одной карте, вне игрового движка.

Решатель SciPy запускается отдельным Python, чтобы не менять зависимости игры.
"""
import argparse
import hashlib
import json
import pickle
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHAIN = ('grain', 'wood', 'flour', 'bread')


def model(engine, preserve_others=True, owner_bread=None):
    variables, bounds, costs, objective = [], [], [], []
    rows, rhs = [], []
    balance = defaultdict(dict)
    labor, resource = defaultdict(dict), defaultdict(dict)
    labor_cap, resource_cap = defaultdict(float), defaultdict(float)
    firms = [f for f in engine.enterprises.values() if f['good'] in CHAIN
        and not f.get('suspended') and f['available_from'] <= engine.meta['month']]
    # Закрытые, но физически существующие мощности допускают перезапуск без стройки.
    for a in engine.accounts.values():
        if a['kind'] == 'household':
            labor_cap[a['state_id'], a['province_id']] += engine.population(a)*engine.cfg['labor_share']
    for tile in engine.tiles.values():
        for r in tile['resources']:
            resource_cap[tile['province_id'], r] += engine.cfg['extraction_per_tile']
    external = defaultdict(float)
    if preserve_others:
        for tile, quantity in (owner_bread or {}).items():
            external[tile, 'bread'] += quantity
        for f in engine.enterprises.values():
            if f['good'] in CHAIN:
                continue
            recipe = engine.goods[f['good']]
            q = f['output']
            labor_cap[f['state_id'], f['province_id']] -= q*recipe['labor']
            if recipe['resource']:
                resource_cap[f['province_id'], recipe['resource']] -= q
            for g, ratio in recipe['inputs'].items():
                if g in CHAIN:
                    external[engine.market_tile(engine.accounts[f['id']]), g] += q*ratio
    needs = defaultdict(float)
    for a in engine.accounts.values():
        if a['kind'] == 'household':
            tile = engine.market_tile(a)
            needs[tile] += engine.population(a)*engine.cfg['basic_needs']['bread']

    def var(label, upper=None, cost=0, value=0):
        i = len(variables)
        variables.append(label); bounds.append((0, upper)); costs.append(cost); objective.append(-value)
        return i

    def constraint(row, upper):
        rows.append(row); rhs.append(upper)

    for key in external:
        balance[key]  # Невыполнимый внешний спрос также обязан попасть в ограничения.

    for f in firms:
        tile, recipe = engine.market_tile(engine.accounts[f['id']]), engine.goods[f['good']]
        i = var(dict(kind='production', firm=f['id'], good=f['good']), f['capacity'],
            recipe['labor']*engine.cfg['wage'])
        balance[tile, f['good']][i] = -1
        for g, ratio in recipe['inputs'].items():
            balance[tile, g][i] = balance[tile, g].get(i, 0)+ratio
        labor[f['state_id'], f['province_id']][i] = recipe['labor']
        if recipe['resource']:
            resource[f['province_id'], recipe['resource']][i] = 1
    destinations = defaultdict(set)
    for f in firms:
        tile = engine.market_tile(engine.accounts[f['id']])
        for g in engine.goods[f['good']]['inputs']:
            destinations[g].add(tile)
    for tile, g in external:
        destinations[g].add(tile)
    destinations['bread'].update(needs)
    # Один поток на пару расположений. Повторные фирмы на одном месте не дублируют путь.
    sources = {g: sorted({engine.market_tile(engine.accounts[f['id']]) for f in firms if f['good'] == g}) for g in CHAIN}
    for g in CHAIN:
        for source in sources[g]:
            for dest in sorted(destinations[g]):
                if source == dest:
                    continue
                quote = engine.landed(source, dest, g, 0)
                if quote is None:
                    continue
                i = var(dict(kind='shipping', good=g, source=source, dest=dest, freight=quote[1]), cost=quote[1])
                balance[source, g][i] = 1
                balance[dest, g][i] = -1
    for tile, needed in sorted(needs.items()):
        i = var(dict(kind='consumption', good='bread', tile=tile, need=needed), needed, value=1)
        balance[tile, 'bread'][i] = 1
    for key, row in balance.items():
        constraint(row, -external.get(key, 0))
    for key, row in labor.items():
        constraint(row, max(0, labor_cap[key]))
    for key, row in resource.items():
        constraint(row, max(0, resource_cap[key]))
    # Приоритет потребителям в этой физической задаче; касса и цены не ограничены.
    return dict(variables=variables, bounds=bounds, costs=costs, objective=objective,
        rows=rows, rhs=rhs, need=sum(needs.values()), external={str(k): v for k, v in external.items()},
        preserve_others=preserve_others)


SOLVER = '''import json,sys,numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix,vstack,csr_matrix
m=json.load(sys.stdin);n=len(m['variables']);rr=[];cc=[];vv=[]
for r,row in enumerate(m['rows']):
 for c,v in row.items():rr.append(r);cc.append(int(c));vv.append(v)
A=coo_matrix((vv,(rr,cc)),shape=(len(m['rows']),n)).tocsr();b=np.array(m['rhs'])
first=linprog(m['objective'],A_ub=A,b_ub=b,bounds=m['bounds'],method='highs')
if not first.success:
 print(json.dumps(dict(success=False,message=first.message)));sys.exit(0)
target=-first.fun
# Второй проход минимизирует зарплату и фрахт при сохранении максимального питания.
A2=vstack([A,csr_matrix([m['objective']])]);b2=np.append(b,-target+1e-7)
result=linprog(m['costs'],A_ub=A2,b_ub=b2,bounds=m['bounds'],method='highs')
if not result.success:raise RuntimeError(result.message)
x=result.x
assert np.max(A@x-b)<1e-5
for v,(lo,hi) in zip(x,m['bounds']):assert v>=lo-1e-6 and (hi is None or v<=hi+1e-6)
print(json.dumps(dict(success=True,bread=target,cost=float(np.dot(x,m['costs'])),max_constraint_error=float(np.max(A@x-b)),x=x.tolist())))
'''


def solve(engine, preserve_others=True, solver_python=None, owner_bread=None):
    data = model(engine, preserve_others, owner_bread)
    result = subprocess.run([solver_python or shutil.which('python'), '-c', SOLVER],
        input=json.dumps(data), capture_output=True, text=True, check=True)
    result = json.loads(result.stdout)
    if not result['success']:
        return result
    witness = [dict(v, quantity=q) for v, q in zip(data['variables'], result.pop('x')) if q > 1e-6]
    freight = sum(v['quantity']*v['freight'] for v in witness if v['kind'] == 'shipping')
    return dict(result, need=data['need'], coverage=result['bread']/data['need'],
        freight=freight, wages=result['cost']-freight, preserve_others=preserve_others,
        external=data['external'], witness=witness)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--solver-python')
    parser.add_argument('--trace', help='Необязательно: средний выпуск прочих отраслей за последние 30 месяцев')
    args = parser.parse_args()
    path = Path(args.checkpoint)
    # Только собственные доверенные состояния исследования.
    engine = pickle.loads(path.read_bytes())
    actual = None
    owner_bread = defaultdict(float)
    if args.trace:
        trace = Path(args.trace)
        history = [json.loads(line) for line in trace.read_text().splitlines()][-30:]
        if len(history) != 30 or history[-1]['month'] != engine.meta['month']:
            raise ValueError('Трасса должна содержать 30 месяцев до checkpoint включительно')
        outputs = defaultdict(float)
        for row in history:
            for f in row['firms']:
                outputs[f['id']] += f['output']/30
            for d in row['decisions']:
                if d['good'] == 'bread' and d['purpose'] == 'Личное потребление собственника':
                    tile = engine.market_tile(engine.accounts[d['participant']])
                    owner_bread[tile] += (d['quantity']-d['remaining'])/30
        for f in engine.enterprises.values():
            if f['good'] not in CHAIN:
                f['output'] = outputs[f['id']]
        actual = dict(first_month=history[0]['month'], last_month=history[-1]['month'],
            bread_output=sum(r['goods']['bread']['output'] for r in history)/30,
            bread_household=sum(r['goods']['bread']['household_consumed'] for r in history)/30,
            trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest())
    else:
        for row in engine.report.get('owner_consumption', []):
            tile = engine.market_tile(engine.accounts[row['owner']])
            owner_bread[tile] += row['consumed'].get('bread', 0)
    initial = pickle.dumps((engine.accounts, engine.enterprises, engine.meta, engine.rng.getstate()))
    result = dict(checkpoint_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), month=engine.meta['month'],
        seed=engine.seed, actual=actual,
        observer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        owner_bread_preserved=sum(owner_bread.values()),
        assumptions='steady monthly flow; no initial stocks, no new factories; unlimited transport capacity as in game; money, asks and profitability excluded; preserved scenario also supplies existing owner bread consumption',
        bread_only=solve(engine, False, args.solver_python),
        other_industries_preserved=solve(engine, True, args.solver_python, owner_bread))
    assert pickle.dumps((engine.accounts, engine.enterprises, engine.meta, engine.rng.getstate())) == initial
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: {a: b for a, b in v.items() if a not in ('witness', 'external')} if isinstance(v, dict) else v for k, v in result.items()}, ensure_ascii=False, indent=2))
