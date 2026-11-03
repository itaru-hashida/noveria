"""Описательная и парная оценка ответа выпуска на обеспеченный спрос."""
import argparse
import csv
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.research_agents import paired_test, ratio


def load(path):
    meta = json.loads((path/'metadata.json').read_text())
    if meta['status'] != 'completed' or (meta['variant'] == 'control' and not meta.get('observer_state_parity')):
        raise ValueError('Нужен завершённый и проверенный прогон: '+str(path))
    trace = path/'monthly.jsonl'
    rows = [json.loads(l) for l in trace.read_text().splitlines()]
    if len(rows) != 60:
        raise ValueError('Нужны полные 60 месяцев')
    window = rows[-30:]
    avg = lambda f: statistics.mean(f(r) for r in window)
    goods = {}
    for g in window[0]['goods']:
        goods[g] = dict(output=avg(lambda r: r['goods'][g]['output']),
            stock=avg(lambda r: r['goods'][g]['stock']),
            household_consumed=avg(lambda r: r['goods'][g]['household_consumed']),
            used=avg(lambda r: r['goods'][g]['used']),
            need=avg(lambda r: r['goods'][g]['need']),
            execution=ratio(sum(r['goods'][g]['output'] for r in window), sum(r['goods'][g]['plan'] for r in window)))
    diagnostic = defaultdict(lambda: defaultdict(float))
    examples = []
    for r in window:
        for f in r['supply_response']:
            d = diagnostic[f['good']]
            d['firm_months'] += 1
            d['zero_plan_stock'] += f['plan'] <= 1e-7 and f['planning_reason'] == 'Готовые запасы'
            d['zero_plan_sold_out'] += f['zero_plan_sold_out']
            d['idle_after_sold_out'] += f['zero_plan_sold_out'] and f['output'] <= 1e-7
            d['unanticipated_sales'] += f['sold'] if f['expected_sales'] <= 1e-7 else 0
            d['retrospective_gap_per_month'] += f['retrospective_replenishment_gap']/30
            if f['zero_plan_sold_out'] and f['good'] == 'grain' and len(examples) < 3:
                examples.append(dict(month=r['month'], **f))
    monthly = []
    for r in rows:
        monthly.append(dict(run=path.name, month=r['month'], coverage=r['macro']['basic_coverage'],
            household_bread=r['macro']['household_bread'],
            household_net_flow=r['supply_finance']['household_net_flow'],
            household_cash=r['supply_finance']['household_cash'],
            **{g+'_output': r['goods'][g]['output'] for g in goods},
            **{g+'_stock': r['goods'][g]['stock'] for g in goods}))
    return dict(metadata=meta, trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
        coverage=avg(lambda r: r['macro']['basic_coverage']),
        household_bread=avg(lambda r: r['macro']['household_bread']),
        household_net_flow=avg(lambda r: r['supply_finance']['household_net_flow']),
        household_cash_end=rows[-1]['supply_finance']['household_cash'],
        open_firms_end=rows[-1]['macro']['open'], goods=goods, diagnostic=dict(diagnostic),
        investment_starts=sum(r['macro']['starts'] for r in window),
        examples=examples, invariant_violations=sum(r['micro']['violated'] for r in rows)), monthly


def run(args):
    root, out = Path(args.root), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases, monthly = {}, []
    for label in ('73129', '17', '2026', 'fresh-73129'):
        cases[label] = {}
        variants = ('control', 'lead_time', 'raw_lead_time', 'post_trade_raw') if label == 'fresh-73129' else ('control', 'lead_time')
        for variant in variants:
            result, records = load(root/(label+'-'+variant))
            cases[label][variant] = result
            monthly.extend(records)
        control = cases[label]['control']
        for variant, result in cases[label].items():
            for key in ('checkpoint_sha256', 'engine', 'seed', 'months', 'supply_observer_sha256'):
                if result['metadata'][key] != control['metadata'][key]:
                    raise ValueError('Несопоставимая пара: '+label+' '+key)
            result['coverage_delta_pp'] = 100*(result['coverage']-control['coverage'])
            result['bread_delta_pct'] = 100*(result['household_bread']/control['household_bread']-1)
    differences = [cases[label]['lead_time']['coverage_delta_pp'] for label in ('73129', '17', '2026')]
    result = dict(primary='control vs lead_time; last 30 of 60 months; three migrated seeds; fresh history and other policies separate',
        primary_test=paired_test(differences), cases=cases)
    (out/'supply_response_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    with (out/'supply_response_monthly.csv').open('w', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(monthly[0]), lineterminator='\n')
        writer.writeheader(); writer.writerows(monthly)
    print(json.dumps(dict(primary=result['primary_test'], results={label: {v: dict(coverage=r['coverage'],
        delta_pp=r['coverage_delta_pp'], household_bread=r['household_bread'], household_net_flow=r['household_net_flow'])
        for v, r in variants.items()} for label, variants in cases.items()}), indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True)
    p.add_argument('--out', required=True)
    run(p.parse_args())
