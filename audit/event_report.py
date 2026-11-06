"""Компактный отчёт по причинному JSONL-логу экономики.

Пример: python audit/event_report.py /tmp/noveria-events.jsonl
"""
import argparse
import json
from collections import Counter, defaultdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path')
    args = parser.parse_args()
    events = [json.loads(line) for line in open(args.path, encoding='utf-8') if line.strip()]
    types = Counter(e['event_type'] for e in events)
    causes = Counter((e['event_type'], e.get('cause', 'none')) for e in events)
    print('events', len(events), 'months', sorted({e['month'] for e in events}))
    print('event_types')
    for key, value in types.most_common():
        print(f'  {key}: {value}')
    print('causes')
    for (kind, cause), value in causes.most_common(30):
        print(f'  {kind} / {cause}: {value}')
    unexplained = [e for e in events if e.get('cause') == 'unexplained_gap']
    print('unexplained_gap', len(unexplained))
    by_good = defaultdict(float)
    for event in unexplained:
        by_good[event.get('good', '?')] += event.get('quantity', event.get('deficit', 0))
    for good, quantity in sorted(by_good.items(), key=lambda item: -item[1]):
        print(f'  {good}: {quantity:.3f}')


if __name__ == '__main__':
    main()
