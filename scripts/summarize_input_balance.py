"""Парное сравнение комплектности сырья; основной исход — обеспечение населения."""
import argparse
import csv
import hashlib
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.research_agents import paired_test, ratio


def load(path):
    meta = json.loads((path/'metadata.json').read_text())
    trace = path/'monthly.jsonl'
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    if meta['status'] != 'completed' or len(rows) != 60:
        raise ValueError('Нужен завершённый прогон 60 месяцев: '+str(path))
    if meta['variant'] == 'control' and not meta.get('observer_state_parity'):
        raise ValueError('Нет проверки контроля')
    window = rows[-30:]
    avg = lambda key: statistics.mean(r['input_balance'][key] for r in window)
    goods = {g: dict(output=statistics.mean(r['goods'][g]['output'] for r in window),
        execution=ratio(sum(r['goods'][g]['output'] for r in window), sum(r['goods'][g]['plan'] for r in window)),
        demand=statistics.mean(r['goods'][g]['order_quantity'] for r in window)) for g in window[0]['goods']}
    return dict(metadata=meta, trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest(),
        first_month=window[0]['month'], last_month=window[-1]['month'],
        coverage=statistics.mean(r['macro']['basic_coverage'] for r in window),
        household_bread=statistics.mean(r['macro']['household_bread'] for r in window),
        unpaired_value=avg('unpaired_input_value'), input_value=avg('total_multi_input_value'),
        unpaired_share=ratio(avg('unpaired_input_value'), avg('total_multi_input_value')),
        open_firms_end=rows[-1]['macro']['open'],
        limited_orders=sum(r['input_balance']['limited_orders'] for r in window),
        intentional_missed=sum(r['input_balance']['intentional_missed'] for r in window),
        strict_missed=sum(r['micro']['strict_missed'] for r in window),
        invariant_violations=sum(r['micro']['violated'] for r in rows), goods=goods)


def run(args):
    pairs = []
    for label in ('73129', '17', '2026', 'fresh-73129'):
        control, balanced = [load(Path(args.root)/(label+'-'+v)) for v in ('control', 'balanced')]
        for key in ('checkpoint_sha256', 'initial_month', 'engine', 'months', 'experiment_sha256', 'seed'):
            if control['metadata'][key] != balanced['metadata'][key]:
                raise ValueError('Несопоставимые прогоны: '+key)
        pairs.append(dict(label=label, control=control, balanced=balanced,
            coverage_delta_pp=100*(balanced['coverage']-control['coverage']),
            bread_delta_pct=100*(balanced['household_bread']/control['household_bread']-1),
            unpaired_delta_pct=100*(balanced['unpaired_value']/control['unpaired_value']-1)))
    primary = paired_test([p['coverage_delta_pp'] for p in pairs[:3]])
    result = dict(primary='basic coverage; last 30 of 60 months; three migrated seeds; fresh history separate',
        primary_test=primary, pairs=pairs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out/'input_balance_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    with (out/'input_balance_monthly.csv').open('w', newline='') as target:
        fields = ['case', 'variant', 'month', 'coverage', 'household_bread', 'unpaired_value', 'input_value',
            'limited_orders', 'intentional_missed', 'violated']+[g+'_output' for g in pairs[0]['control']['goods']]
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for pair in pairs:
            for variant in ('control', 'balanced'):
                for line in (Path(args.root)/(pair['label']+'-'+variant)/'monthly.jsonl').read_text().splitlines():
                    r = json.loads(line)
                    writer.writerow(dict(case=pair['label'], variant=variant, month=r['month'],
                        coverage=r['macro']['basic_coverage'], household_bread=r['macro']['household_bread'],
                        unpaired_value=r['input_balance']['unpaired_input_value'], input_value=r['input_balance']['total_multi_input_value'],
                        limited_orders=r['input_balance']['limited_orders'], intentional_missed=r['input_balance']['intentional_missed'],
                        violated=r['micro']['violated'], **{g+'_output': v['output'] for g, v in r['goods'].items()}))
    print(json.dumps(dict(primary=primary, pairs=[{k: v for k, v in p.items() if k not in ('control', 'balanced')} for p in pairs]), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--out', required=True)
    run(parser.parse_args())
