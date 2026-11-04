"""Парная проверка baseline/adaptive на серии seed."""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402
from stock_planning_adaptive_experiment import adaptive_stock_sales  # noqa: E402


def metrics(rows):
    rows = [r for r in rows if int(r['month']) >= 6]
    avg = lambda key: sum(float(r[key]) for r in rows) / len(rows)
    return {
        'deficit_bread': avg('deficit_bread'),
        'prod_bread': avg('prod_bread'),
        'stock_bread': avg('goods_stock_bread'),
    }


def main():
    sr.install_shock()
    out = ROOT / 'audit/_runs/current/stock_20seed.csv'
    out.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for seed in range(1001, 1011):
        baseline = sr.run(seed, 24, 'baseline_10seed', None, None)
        original = Economy.expected_stock_sales
        try:
            Economy.expected_stock_sales = adaptive_stock_sales
            adaptive = sr.run(seed, 24, 'adaptive_10seed', None, None)
        finally:
            Economy.expected_stock_sales = original
        b, a = metrics(baseline), metrics(adaptive)
        records.append(dict(seed=seed, **{f'baseline_{k}': round(v, 3) for k, v in b.items()},
                            **{f'adaptive_{k}': round(v, 3) for k, v in a.items()}))
        print('completed seed', seed, flush=True)
    with out.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(records[0]))
        w.writeheader(); w.writerows(records)
    print('written', out)


if __name__ == '__main__':
    main()
