"""Описательная сводка четырёх проверок; месяцы не считаются независимыми опытами."""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

EPS = 1e-7


def ratio(numerator, denominator):
    return numerator/denominator if denominator > EPS else None


def idle_summary(rows, threshold=.01):
    streak, longest, opportunities, goods = defaultdict(int), defaultdict(int), set(), {}
    for row in rows:
        present = set()
        for firm in row['opportunity_reserve']['local']:
            identity = firm['id']
            present.add(identity)
            goods[identity] = firm['good']
            available = firm['extra'] >= max(EPS, threshold*firm['capacity'])
            if available:
                opportunities.add(identity)
            streak[identity] = streak[identity]+1 if available and firm['output'] <= EPS else 0
            longest[identity] = max(longest[identity], streak[identity])
        for identity in streak.keys()-present:
            streak[identity] = 0
    return dict(threshold_capacity_fraction=threshold, eligible=len(opportunities),
        persistent=sum(longest[i] >= 3 for i in opportunities),
        fraction=ratio(sum(longest[i] >= 3 for i in opportunities), len(opportunities)),
        longest=max(longest.values(), default=0),
        firms=[dict(id=i, good=goods[i], longest=longest[i]) for i in sorted(opportunities)])


def summarize(directory, window=30):
    if window < 1:
        raise ValueError('Окно должно быть положительным')
    path = Path(directory)
    metadata = json.loads((path/'metadata.json').read_text())
    if metadata['status'] != 'completed' or not metadata.get('observer_state_parity'):
        raise ValueError('Нужна законченная серия с проверкой нейтральности: '+str(path))
    trace = path/'monthly.jsonl'
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    if len(rows) != metadata['months'] or len(rows) < window:
        raise ValueError('Неполная серия: '+str(path))
    selected = rows[-window:]
    by_good = {}
    for good in selected[0]['goods']:
        output = sum(r['goods'][good]['output'] for r in selected)
        plan = sum(r['goods'][good]['plan'] for r in selected)
        extra = sum(r['opportunity_reserve']['joint_extra'].get(good, 0) for r in selected)
        capacity = sum(r['goods'][good]['capacity'] for r in selected)
        by_good[good] = dict(output_per_month=output/window, plan_per_month=plan/window,
            execution=ratio(output, plan), extra_per_month=extra/window,
            reserve_share=ratio(extra, output+extra), nominal_utilization=ratio(output, capacity),
            stock_coverage_per_month=sum(r['opportunity_reserve']['stock_coverage'].get(good, 0) for r in selected)/window)
    decisions = sum(r['micro']['input_decisions'] for r in selected)
    missed = sum(r['micro']['dominated'] for r in selected)
    shortfalls, unfilled = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
    for row in selected:
        for firm in row['firms']:
            shortfalls[firm['good']][firm['limit']] += max(0, firm['plan']-firm['output'])
        for decision in row['decisions']:
            if decision['purpose'] == 'Сырьё' and decision['remaining'] > EPS:
                unfilled[decision['good']][decision['reason']] += decision['remaining']
    return dict(name=path.name, metadata=metadata, trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
        first_month=selected[0]['month'], last_month=selected[-1]['month'], window=window,
        months_observed=len(rows), invariant_failures=sum(r['micro']['violated'] for r in rows),
        basic_coverage=sum(r['macro']['basic_coverage'] for r in selected)/window,
        input_refusals=dict(witnesses=missed, attempts=decisions, rate=ratio(missed, decisions)),
        strict_missed=sum(r['micro']['strict_missed'] for r in selected),
        trade_attempts=sum(r['micro']['trade_decisions'] for r in selected),
        shortfalls_by_limit={g: {reason: q for reason, q in reasons.items() if q > EPS}
            for g, reasons in shortfalls.items()}, unfilled_inputs_by_reason=dict(unfilled),
        goods=by_good, idle=idle_summary(selected),
        idle_sensitivity=[idle_summary(selected, t) for t in (.001, .05)])


def run(args):
    results = [summarize(path, args.window) for path in args.runs]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out/'opportunity_use_summary.json').write_text(json.dumps(dict(
        definition='four conditional diagnostics; no global optimum or causal effect estimate', runs=results),
        ensure_ascii=False, indent=2)+'\n')
    fields = ['run', 'first_month', 'last_month', 'good', 'execution', 'extra_per_month', 'reserve_share',
        'output_per_month', 'plan_per_month', 'nominal_utilization', 'stock_coverage_per_month']
    with (out/'opportunity_use_goods.csv').open('w', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for result in results:
            for good, values in result['goods'].items():
                writer.writerow(dict(run=result['name'], first_month=result['first_month'],
                    last_month=result['last_month'], good=good, **values))
    # Достаточные суммы для повторного расчёта первых трёх метрик по любому окну.
    fields = ['run', 'month', 'good', 'output', 'plan', 'joint_extra', 'capacity',
        'stock_coverage', 'input_attempts', 'input_refusals']
    with (out/'opportunity_use_monthly.csv').open('w', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for directory, result in zip(args.runs, results):
            for line in (Path(directory)/'monthly.jsonl').read_text().splitlines():
                row = json.loads(line)
                for good, values in row['goods'].items():
                    decisions = [d for d in row['decisions'] if d['purpose'] == 'Сырьё' and d['good'] == good]
                    writer.writerow(dict(run=result['name'], month=row['month'], good=good,
                        output=values['output'], plan=values['plan'], capacity=values['capacity'],
                        joint_extra=row['opportunity_reserve']['joint_extra'].get(good, 0),
                        stock_coverage=row['opportunity_reserve']['stock_coverage'].get(good, 0),
                        input_attempts=len(decisions), input_refusals=sum(d['witness'] is not None for d in decisions)))
    print(json.dumps([dict(run=r['name'], inputs=r['input_refusals'],
        idle={k: v for k, v in r['idle'].items() if k != 'firms'},
        goods={g: r['goods'][g] for g in ('bread', 'flour', 'grain', 'wood', 'tools', 'fish')}) for r in results], indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+')
    parser.add_argument('--window', type=int, default=30)
    parser.add_argument('--out', required=True)
    run(parser.parse_args())
