"""Разложение хлебного дефицита и финансовых ограничений, без причинных обещаний."""
import argparse
import csv
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path


def summarize(root, out):
    root, out = Path(root), Path(out)
    meta = json.loads((root/'metadata.json').read_text())
    if meta['status'] != 'completed' or not meta.get('observer_state_parity'):
        raise ValueError('Нужен завершённый нейтральный прогон')
    path = root/'monthly.jsonl'
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    window = rows[-30:]
    records = []
    reasons, incoming, outgoing = defaultdict(float), defaultdict(float), defaultdict(float)
    chain = defaultdict(lambda: defaultdict(float))
    zero_plans = defaultdict(lambda: defaultdict(int))
    for r in window:
        m = r['food_money']
        record = dict(month=r['month'])
        for key in ('need', 'requested', 'unfunded', 'unfilled', 'consumed', 'bread_only_affordable', 'cash'):
            record[key] = sum(h[key] for h in m['households'])
        records.append(record)
        record['incoming'] = sum(m['incoming'].values())
        record['outgoing'] = sum(m['outgoing'].values())
        for key, v in m['incoming'].items(): incoming[key] += v/len(window)
        for key, v in m['outgoing'].items(): outgoing[key] += v/len(window)
        for d in m['decisions']:
            if d['good'] == 'bread' and d['purpose'] == 'Базовые нужды' and d['remaining'] > 1e-7:
                reasons[d['reason']] += d['remaining']/len(window)
        for p in m['producers']:
            for key in ('cash', 'output', 'profit', 'wages', 'revenue', 'plan', 'requested'):
                chain[p['good']][key] += p[key]/len(window)
            if p['requested'] > 1e-7:
                chain[p['good']]['positive_requested_plans'] += 1
                chain[p['good']]['funding_cuts'] += p['funding_cut']
        for f in r['firms']:
            if f['good'] in chain and f['plan'] < 1e-7:
                zero_plans[f['good']][f['limit']] += 1
    avg = {key: statistics.mean(r[key] for r in records) for key in records[0] if key != 'month'}
    result = dict(metadata=meta, trace_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        first_month=window[0]['month'], last_month=window[-1]['month'], averages=avg,
        unfilled_bread_by_reason=dict(reasons), household_incoming=dict(incoming), household_outgoing=dict(outgoing),
        chain=dict(chain), zero_plan_reasons=dict(zero_plans),
        household_net_flow=sum(incoming.values())-sum(outgoing.values()),
        invariant_violations=sum(r['micro']['violated'] for r in rows))
    out.mkdir(parents=True, exist_ok=True)
    (out/'food_money_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    with (out/'food_money_monthly.csv').open('w', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(records[0]), lineterminator='\n')
        writer.writeheader(); writer.writerows(records)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    summarize(args.root, args.out)
