"""Зонд планирования выпуска: прогноз продаж со склада против факта (код движка не меняется).

Обёртка над Economy.input_requests читает состояние до планирования (чистые функции),
обёртка над Economy.step сравнивает прогноз с фактическими сделками месяца.

Запуск: python audit/probe_stock_planning.py --seed 17 --months 36
Пишет audit/_runs/probe_<seed>.json
"""
import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'audit'))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402

GOODS = ('bread', 'flour', 'grain')
PRE = defaultdict(dict)
ROWS = []
CASH = {}


def install():
    original_input = Economy.input_requests
    original_step = Economy.step

    def input_requests(self, e, sells):
        if e['good'] in GOODS and e['status'] != 'closed':
            a = self.accounts[e['id']]
            offered = sum(o['remaining'] for o in sells.get(e['good'], [])
                if o['participant'] == e['id'] and o['status'] not in ('cancelled', 'expired'))
            pre = dict(good=e['good'], stock=a['stock'].get(e['good'], 0), offered=offered,
                       expected=self.expected_stock_sales(e, sells),
                       history=[h['sold'] for h in e.get('sales_history', [])[-3:]])
            original_input(self, e, sells)
            plan = e.get('production_plan', {})
            pre.update(planned=plan.get('output') if plan.get('month') == self.meta['month'] else None,
                       forecast=e.get('sales_forecast'), reason=e.get('production_limit'))
            PRE[self.meta['month']][e['id']] = pre
        else:
            original_input(self, e, sells)

    def step(self):
        report = original_step(self)
        month = self.meta['month']
        sold = defaultdict(float)
        for t in report['trades']:
            sold[t['seller'], t['good']] += t['quantity']
        agg = defaultdict(lambda: defaultdict(float))
        for fid, pre in PRE.get(month, {}).items():
            g = pre['good']
            actual = sold[fid, g]
            r = agg[g]
            r['firms'] += 1
            r['stock'] += pre['stock']
            r['expected_sales'] += pre['expected']
            r['actual_sales'] += actual
            r['planned'] += pre['planned'] or 0
            r['output'] += self.enterprises[fid]['output']
            if pre['stock'] > 1e-6:
                r['firms_with_stock'] += 1
                if (pre['planned'] or 0) <= 1e-6:
                    r['zero_plan_with_stock'] += 1
                    if actual >= 0.9*pre['stock']:
                        r['zero_plan_sold_out'] += 1
                if actual >= 0.9*pre['stock']:
                    r['sold_out'] += 1
                if actual > pre['expected']+1e-6:
                    r['actual_above_expected'] += 1
        ROWS.append(dict(month=month, **{g: {k: round(v, 1) for k, v in d.items()} for g, d in agg.items()}))
        if month % 12 == 0:
            firms = [e for e in self.enterprises.values() if e['status'] != 'closed']
            CASH[str(month)] = dict(
                active_cash=round(sum(self.accounts[e['id']]['balance'] for e in firms if e['status'] == 'active'), 1),
                idle_cash=round(sum(self.accounts[e['id']]['balance'] for e in firms if e['status'] == 'idle'), 1),
                idle_firms=sum(e['status'] == 'idle' for e in firms), active_firms=sum(e['status'] == 'active' for e in firms),
                working_reserve_idle=round(sum(e.get('working_reserve', 0) for e in firms if e['status'] == 'idle'), 1),
                working_reserve_active=round(sum(e.get('working_reserve', 0) for e in firms if e['status'] == 'active'), 1),
                capacity_idle=round(sum(e['capacity'] for e in firms if e['status'] == 'idle'), 1),
                capacity_active=round(sum(e['capacity'] for e in firms if e['status'] == 'active'), 1))
        return report

    Economy.input_requests = input_requests
    Economy.step = step


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seed', type=int, required=True)
    p.add_argument('--months', type=int, default=36)
    p.add_argument('--out', type=Path, default=ROOT/'audit/_runs')
    a = p.parse_args()
    install()
    with tempfile.TemporaryDirectory() as tmp:
        db, service = sr.build_world(a.seed, tmp)
        try:
            for step in range(1, a.months+1):
                service.advance_turn(command_id=f'probe-{a.seed}-{step}', source='audit')
                print('probe month', step, flush=True)
        finally:
            db.close()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out/f'probe_{a.seed}.json').write_text(json.dumps(dict(rows=ROWS, cash=CASH), ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
