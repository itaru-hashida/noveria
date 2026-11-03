"""Четыре проверки использования возможностей; правила игрового движка не меняются."""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_agents as research

EPS = 1e-7


def feasible_reserve(engine, sells):
    """Совместный консервативный свидетель после реального выпуска.

    Цены и остаточные заявки заморожены. Доходы от гипотетических продаж и
    зарплаты не реинвестируются, новые входы не закупаются. Это не оптимум и
    не доказательство, что агент мог предвидеть результат торгов.
    """
    cash = {i: engine.free_cash(i) for i in engine.accounts}
    stock = {(i, g): engine.free_stock(i, g) for i, a in engine.accounts.items() for g in a['stock']}
    labor, resources = defaultdict(float), defaultdict(float)
    for a in engine.accounts.values():
        if a['kind'] == 'household':
            labor[a['state_id'], a['province_id']] += engine.population(a)*engine.cfg['labor_share']
    for t in engine.tiles.values():
        for resource in t['resources']:
            resources[t['province_id'], resource] += engine.cfg['extraction_per_tile']
    for f in engine.enterprises.values():
        labor[f['state_id'], f['province_id']] -= f['workers']
        resource = engine.goods[f['good']]['resource']
        if resource:
            resources[f['province_id'], resource] -= f['output']
    orders = []
    for o in getattr(engine, '_planning_orders', []):
        if o['side'] != 'buy' or o['remaining'] <= EPS or o['status'] in ('cancelled', 'expired'):
            continue
        orders.append(dict(o, remaining=o['remaining'], funds=o['reserved']))
    orders.sort(key=lambda o: o['id'])
    demand_before = defaultdict(float)
    for order in orders:
        demand_before[order['good']] += min(order['remaining'], order['funds']/order['price'])

    # Существующий готовый склад, включая новый выпуск, прежде дополнительной выработки.
    # Для чужого сырья учитываем только реально оставшуюся на торгах часть предложения.
    supplies = defaultdict(list)
    primary = set()
    for f in sorted(engine.enterprises.values(), key=lambda f: f['id']):
        if f.get('suspended'):
            continue
        a = engine.accounts[f['id']]
        primary.add((f['id'], f['good']))
        supplies[f['good']].append((f['id'], engine.market_tile(a), f['price'], stock.get((f['id'], f['good']), 0)))
    for good, offers in sells.items():
        for offer in offers:
            key = offer['participant'], good
            if key in primary or offer['status'] in ('cancelled', 'expired') or offer['remaining'] <= EPS:
                continue
            supplies[good].append((key[0], offer['tile_id'], offer['price'], min(offer['remaining'], stock.get(key, 0))))
    supply_remaining = {(good, index): row[3] for good, rows in supplies.items() for index, row in enumerate(rows)}
    stock_coverage = defaultdict(float)
    for order in orders:
        options = []
        for index, (seller, tile, price, _) in enumerate(supplies[order['good']]):
            if seller == order['participant']:
                continue
            quote = engine.landed(tile, order['tile_id'], order['good'], price)
            if quote and quote[0] <= order['price']+EPS:
                options.append((quote[0], seller, index))
        for price, seller, index in sorted(options):
            quantity = min(order['remaining'], order['funds']/price, cash[order['participant']]/price,
                supply_remaining[order['good'], index], stock.get((seller, order['good']), 0))
            if quantity <= EPS:
                continue
            order['remaining'] -= quantity
            order['funds'] -= quantity*price
            cash[order['participant']] -= quantity*price
            stock[seller, order['good']] -= quantity
            supply_remaining[order['good'], index] -= quantity
            stock_coverage[order['good']] += quantity

    candidates = []
    for f in sorted(engine.enterprises.values(), key=lambda f: f['id']):
        if f['status'] == 'closed' or f.get('suspended') or f['available_from'] > engine.meta['month']:
            continue
        a, recipe = engine.accounts[f['id']], engine.goods[f['good']]
        cost = recipe['labor']*engine.cfg['wage']
        for good, amount in recipe['inputs'].items():
            quantity = a['stock'].get(good, 0)
            book = a['values'].get(good, 0)/quantity if quantity > EPS else 0
            cost += amount*max(book, engine.reference_price(a, good, sells))
        price = f['price']
        candidates.append(dict(firm=f, price=price, cost=cost, recipe=recipe))

    def allocation(candidate, cash, stock, labor, resources, orders, apply):
        f, recipe = candidate['firm'], candidate['recipe']
        wages = recipe['labor']*engine.cfg['wage']
        capacity = max(0, f['capacity']-f['output'])
        total = 0.0
        own_cash = cash[f['id']]
        own_stock = {g: stock.get((f['id'], g), 0) for g in recipe['inputs']}
        own_labor = max(0, labor[f['state_id'], f['province_id']])
        own_resource = max(0, resources[f['province_id'], recipe['resource']]) if recipe['resource'] else float('inf')
        buyer_spent = defaultdict(float)
        for order in orders:
            if order['good'] != f['good'] or order['participant'] == f['id']:
                continue
            net_price = candidate['price']*(1-engine.cfg['consumption_tax'] if order.get('final_consumption') else 1)
            if net_price <= candidate['cost']+EPS:
                continue
            quote = engine.landed(engine.market_tile(engine.accounts[f['id']]), order['tile_id'], f['good'], candidate['price'])
            if quote is None or quote[0] > order['price']+EPS:
                continue
            constraints = [capacity, own_labor/recipe['labor'], own_resource, order['remaining'],
                order['funds']/quote[0], (cash[order['participant']]-buyer_spent[order['participant']])/quote[0]]
            if wages > EPS:
                constraints.append(own_cash/wages)
            constraints.extend(own_stock[g]/amount for g, amount in recipe['inputs'].items())
            quantity = max(0, min(constraints))
            if quantity <= EPS:
                continue
            total += quantity
            capacity -= quantity
            own_labor -= quantity*recipe['labor']
            own_resource -= quantity
            own_cash -= quantity*wages
            for g, amount in recipe['inputs'].items():
                own_stock[g] -= quantity*amount
            if apply:
                order['remaining'] -= quantity
                order['funds'] -= quantity*quote[0]
                cash[order['participant']] -= quantity*quote[0]
            else:
                buyer_spent[order['participant']] += quantity*quote[0]
            # У локальной оценки остаток одного order используется лишь однажды.
        if apply:
            cash[f['id']] = own_cash
            labor[f['state_id'], f['province_id']] = own_labor
            if recipe['resource']:
                resources[f['province_id'], recipe['resource']] = own_resource
            for g, q in own_stock.items():
                stock[f['id'], g] = q
        return total

    # Локальные оценки независимы и не суммируются как общий резерв.
    local = [dict(id=c['firm']['id'], good=c['firm']['good'], capacity=c['firm']['capacity'],
        output=c['firm']['output'], plan=c['firm'].get('planned_output', 0),
        limit=c['firm'].get('production_limit'), price=c['price'], unit_cost=c['cost'],
        extra=allocation(c, cash, stock, labor, resources, orders, False)) for c in candidates]
    extra = defaultdict(float)
    # Совместный свидетель использует каждый запас/ресурс/деньги/заявку однократно.
    for candidate in sorted(candidates, key=lambda c: (c['cost'], c['firm']['id'])):
        extra[candidate['firm']['good']] += allocation(candidate, cash, stock, labor, resources, orders, True)
    return dict(joint_extra=dict(extra), local=local, stock_coverage=dict(stock_coverage),
        funded_residual=dict(demand_before), order='unit cost then firm ID; orders by ID',
        assumptions='post-production; frozen quotes; no new raw purchases, no reinvestment of receipts; not an optimum')


class OpportunityEconomy(research.ObservedEconomy):
    def produce(self, existing, sells):
        super().produce(existing, sells)
        before = self.rng.getstate()
        self.opportunity_reserve = feasible_reserve(self, sells)
        assert self.rng.getstate() == before


def run(args):
    original_class, original_capture = research.ObservedEconomy, research.capture
    def capture(engine):
        row = original_capture(engine)
        row['opportunity_reserve'] = engine.opportunity_reserve
        return row
    research.ObservedEconomy, research.capture = OpportunityEconomy, capture
    try:
        research.run(args)
    finally:
        research.ObservedEconomy, research.capture = original_class, original_capture
        metadata = Path(args.out)/'metadata.json'
        if metadata.exists():
            data = json.loads(metadata.read_text())
            data['opportunity_observer_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            metadata.write_text(json.dumps(data, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--checkpoint', help='Только доверенный локальный pickle')
    source.add_argument('--seed', type=int)
    parser.add_argument('--warmup', type=int, default=180)
    parser.add_argument('--months', type=int, default=60)
    parser.add_argument('--out', required=True)
    parser.add_argument('--verify-state')
    args = parser.parse_args()
    args.variant = 'control'
    run(args)
