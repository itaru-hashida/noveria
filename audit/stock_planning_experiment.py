"""Парный эксперимент складского планирования без изменения игрового кода."""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402


def full_stock_sales(self, e, sells):
    """Альтернатива: весь выставленный склад считается продаваемым."""
    offered = sum(o['remaining'] for o in sells.get(e['good'], [])
                  if o['participant'] == e['id']
                  and o['status'] not in ('cancelled', 'expired'))
    stock = self.accounts[e['id']]['stock'].get(e['good'], 0)
    return min(stock, offered)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seed', type=int, required=True)
    p.add_argument('--months', type=int, default=36)
    p.add_argument('--out', type=Path, default=ROOT / 'audit/_runs/current')
    a = p.parse_args()
    sr.install_shock()
    rows = []
    original = Economy.expected_stock_sales
    try:
        Economy.expected_stock_sales = full_stock_sales
        rows = sr.run(a.seed, a.months, 'stock_full', None, None)
    finally:
        Economy.expected_stock_sales = original
    a.out.mkdir(parents=True, exist_ok=True)
    target = a.out / f'stock_full_{a.seed}.csv'
    with target.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print('written', target)


if __name__ == '__main__':
    main()
