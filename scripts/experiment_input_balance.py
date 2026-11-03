"""Изолированная проверка закупок дополняющих входов. В игре не включена."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_agents as research

EPS = 1e-7


class BalancedEconomy(research.ObservedEconomy):
    def purchase_quantity(self, buy, sells):
        # Пользовательские заявки, стройки и рецепты с одним входом не меняются.
        if self.probe.variant != 'balanced' or self.input_bid_ceiling(buy) is None:
            return super().purchase_quantity(buy, sells)
        firm = self.enterprises[buy['participant']]
        recipe = self.goods[firm['good']]['inputs']
        if len(recipe) < 2:
            return super().purchase_quantity(buy, sells)
        plan = firm['production_plan']
        account = self.accounts[firm['id']]
        output = plan['output']
        free = self.free_cash(firm['id'])
        for good, ratio in sorted(recipe.items()):
            if good == buy['good']:
                continue
            held = self.free_stock(firm['id'], good)
            missing = max(0, output*ratio-held)
            orders = [o for o in self.report['orders'] if o['id'] in plan['input_orders']
                and o['good'] == good and o['remaining'] > EPS
                and o['status'] not in ('filled', 'expired', 'cancelled')]
            available = 0.0
            for other in orders:
                budget = other['reserved']+free
                ceiling = self.input_bid_ceiling(other)
                limit = other['price']
                if ceiling is not None:
                    quote = self.quoted_cost(account, good, other['remaining'], sells)[1]
                    limit = max(limit, min(quote, ceiling, budget/other['remaining']))
                spent = 0.0
                for price, sell, _ in self.offers(account, good, sells):
                    if price > limit+EPS:
                        continue
                    q = max(0, min(missing-available, other['remaining']-available,
                        sell['remaining'], (budget-spent)/price))
                    available += q
                    spent += q*price
                free = max(0, free-max(0, spent-other['reserved']))
            output = min(output, (held+available)/ratio)
        allowed = max(0, output*recipe[buy['good']]-self.free_stock(firm['id'], buy['good']))
        allowed = min(buy['remaining'], allowed)
        if allowed+EPS < buy['remaining']:
            buy['input_balance_limited'] = True
        return allowed


def imbalance(engine):
    excess, total = 0.0, 0.0
    for firm in engine.enterprises.values():
        inputs = engine.goods[firm['good']]['inputs']
        if len(inputs) < 2 or firm['status'] == 'closed':
            continue
        held = {g: engine.free_stock(firm['id'], g) for g in inputs}
        complete = min(held[g]/r for g, r in inputs.items())
        for good, ratio in inputs.items():
            price = engine.goods[good]['price']
            total += held[good]*price
            excess += max(0, held[good]-complete*ratio)*price
    return dict(unpaired_input_value=excess, total_multi_input_value=total)


def run(args):
    original_class, original_capture = research.ObservedEconomy, research.capture
    def capture(engine):
        row = original_capture(engine)
        row['input_balance'] = imbalance(engine)
        row['input_balance']['limited_orders'] = sum(o.get('input_balance_limited', False)
            for o in engine.report['orders'])
        row['input_balance']['intentional_missed'] = sum(d['strict_missed'] and
            any(o['id'] == d['id'] and o.get('input_balance_limited') for o in engine.report['orders'])
            for d in row['decisions'])
        return row
    research.ObservedEconomy, research.capture = BalancedEconomy, capture
    try:
        research.run(args)
    finally:
        research.ObservedEconomy, research.capture = original_class, original_capture
        metadata = Path(args.out)/'metadata.json'
        if metadata.exists():
            data = json.loads(metadata.read_text())
            data['experiment_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            metadata.write_text(json.dumps(data, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--months', type=int, default=60)
    parser.add_argument('--out', required=True)
    parser.add_argument('--variant', choices=('control', 'balanced'), required=True)
    args = parser.parse_args()
    args.warmup = 0
    args.verify_state = None
    run(args)
