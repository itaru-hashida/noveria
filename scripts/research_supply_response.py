"""Проверка времени учёта готового склада; эксперимент не включён в игру."""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import research_agents as research

CHAIN = ('grain', 'wood', 'flour', 'bread')
EPS = 1e-7


def expected_stock_sales(engine, firm, sells):
    """Локальная оценка продаж старого склада по уже обеспеченным заявкам.

    Без будущих сделок и без RNG. Конкурирующие более дешёвые склады вычитаются
    существующим reachable_demand; равные цены не гарантируют долю рынка.
    """
    offered = sum(o['remaining'] for o in sells.get(firm['good'], [])
        if o['participant'] == firm['id'] and o['status'] not in ('cancelled', 'expired'))
    if offered <= EPS:
        return 0.0
    stock = engine.accounts[firm['id']]['stock'].get(firm['good'], 0)
    return min(stock, offered, engine.reachable_demand(firm, sells))


class SupplyEconomy(research.ObservedEconomy):
    # Историческая независимая реализация эксперимента до переноса в игру.
    STOCK_PLANNING_REVISION = 0
    def production_outlook(self, firm, sells=None):
        result = super().production_outlook(firm, sells)
        variant = self.probe.variant
        active = variant == 'lead_time' or (variant == 'raw_lead_time' and not self.goods[firm['good']]['inputs'])
        if not active or result['expected_margin'] < -EPS:
            return result
        stock = self.accounts[firm['id']]['stock'].get(firm['good'], 0)
        projected = max(0, stock-expected_stock_sales(self, firm, sells or {}))
        output = max(0, min(firm['capacity'], result['sales_forecast']-projected))
        result['planned_output'] = result['input_target_output'] = output
        if output > EPS:
            result['production_limit'] = 'План'
        return result

    def input_requests(self, firm, sells):
        if not hasattr(self, 'supply_rows') or self.supply_month != self.meta['month']:
            self.supply_rows = []
            self.supply_month = self.meta['month']
        super().input_requests(firm, sells)
        if firm['good'] not in CHAIN:
            return
        p = self.probe.plans[firm['id']]
        self.supply_rows.append(dict(id=firm['id'], good=firm['good'], opening_stock=p['stock'],
            expected_sales=expected_stock_sales(self, firm, sells), forecast=p['forecast'],
            demand=p['demand'], plan=p['plan'], requested=p['requested'], capacity=p['capacity'],
            margin=p['margin'], sale_price=firm['planned_price'], planning_reason=firm['production_plan']['reason']))

    def match(self, buy, sells):
        active = buy and buy['status'] not in ('filled', 'cancelled', 'expired')
        super().match(buy, sells)
        if not active or buy['purpose'] != 'Сырьё' or buy['good'] not in CHAIN or buy['remaining'] <= EPS:
            return
        options = self.offers(self.accounts[buy['participant']], buy['good'], sells)
        d = self.probe.decisions[-1]
        assert d['id'] == buy['id']
        d['cheapest_after'] = options[0][0] if options else None
        d['margin_ceiling'] = self.input_bid_ceiling(buy)


def diagnostic(engine):
    sold = defaultdict(float)
    for t in engine.report['trades']:
        f = engine.enterprises.get(t['seller'])
        if f and f['good'] == t['good']:
            sold[f['id']] += t['quantity']
    records = []
    for original in getattr(engine, 'supply_rows', []):
        r = dict(original)
        f = engine.enterprises[r['id']]
        obs = f.get('production_observation', {})
        left = obs.get('stocks_before', {}).get(r['good'], max(0, r['opening_stock']-sold[r['id']]))
        # Оценка задним числом нужна только для диагностики, не для изменения плана.
        replenishment = max(0, min(r['capacity'], r['forecast']-left)) if r['margin'] >= -EPS else 0
        r.update(sold=sold[r['id']], preproduction_stock=left, output=f['output'],
            production_limit=f.get('production_limit'),
            retrospective_replenishment_gap=max(0, replenishment-r['plan']),
            zero_plan_sold_out=r['plan'] <= EPS and r['opening_stock'] > EPS
                and left <= EPS and sold[r['id']] > EPS)
        records.append(r)
    return records


def run(args):
    if research.Economy.STOCK_PLANNING_REVISION:
        raise ValueError('Поправка склада уже включена в игру. Исторические опыты: ревизия 49eb0d0; новые длинные сравнения: scripts/validate_stock_planning.py')
    original_class, original_capture = research.ObservedEconomy, research.capture
    def capture(engine):
        row = original_capture(engine)
        row['supply_response'] = diagnostic(engine)
        row['supply_finance'] = dict(household_cash=engine.report['balances'].get('household', 0),
            household_net_flow=sum(f['net'] for f in engine.report['account_flows'] if f['kind'] == 'household'))
        return row
    research.ObservedEconomy, research.capture = SupplyEconomy, capture
    try:
        research.run(args)
    finally:
        research.ObservedEconomy, research.capture = original_class, original_capture
        path = Path(args.out)/'metadata.json'
        if path.exists():
            meta = json.loads(path.read_text())
            meta['supply_observer_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            path.write_text(json.dumps(meta, indent=2)+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--variant', choices=('control', 'lead_time', 'raw_lead_time'), required=True)
    p.add_argument('--verify-state')
    p.add_argument('--months', type=int, default=60)
    args = p.parse_args()
    args.warmup = 0
    run(args)
