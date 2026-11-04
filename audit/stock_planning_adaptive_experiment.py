"""Адаптивный прогноз склада: текущий спрос плюс средние фактические продажи."""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402


def adaptive_stock_sales(self, e, sells):
    offered = sum(o['remaining'] for o in sells.get(e['good'], [])
                  if o['participant'] == e['id']
                  and o['status'] not in ('cancelled', 'expired'))
    stock = self.accounts[e['id']]['stock'].get(e['good'], 0)
    reachable = self.reachable_demand(e, sells)
    history = e.get('sales_history', [])[-3:]
    average_sold = (sum(h.get('sold', 0.0) for h in history) / len(history)
                    if history else 0.0)
    expected = max(reachable, average_sold)
    return min(stock, offered, expected)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seed', type=int, required=True)
    p.add_argument('--months', type=int, default=36)
    p.add_argument('--out', type=Path, default=ROOT / 'audit/_runs/current')
    a = p.parse_args()
    sr.install_shock()
    original = Economy.expected_stock_sales
    try:
        Economy.expected_stock_sales = adaptive_stock_sales
        rows = sr.run(a.seed, a.months, 'stock_adaptive', None, None)
    finally:
        Economy.expected_stock_sales = original
    a.out.mkdir(parents=True, exist_ok=True)
    target = a.out / f'stock_adaptive_{a.seed}.csv'
    with target.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print('written', target)


if __name__ == '__main__':
    main()
