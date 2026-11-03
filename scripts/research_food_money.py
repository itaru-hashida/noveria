"""Наблюдение финансов хлебной цепочки без изменения правил."""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import research_agents as research


class MoneyEconomy(research.ObservedEconomy):
    def household_requests(self, households, needs, purpose, sells):
        if purpose == 'Базовые нужды':
            self.food_money = []
            for a in households:
                needed = self.population(a)*needs['bread']
                held = a['stock'].get('bread', 0)
                requested = max(0, needed-held)
                price = self.quoted_cost(a, 'bread', requested, sells)[1]
                cash = self.free_cash(a['id'])
                self.food_money.append(dict(id=a['id'], need=needed, held=held, requested=requested,
                    cash=cash, quoted_price=price, bread_only_affordable=min(requested, cash/price)))
        super().household_requests(households, needs, purpose, sells)


def details(engine, row):
    orders = {o['participant']: o for o in engine.report['orders']
        if o['side'] == 'buy' and o['purpose'] == 'Базовые нужды' and o['good'] == 'bread'}
    for h in engine.food_money:
        o = orders.get(h['id'], {})
        h.update(backed=o.get('quantity', 0), unfilled=o.get('remaining', 0),
            consumed=engine.accounts[h['id']]['needs']['bread']['consumed'])
        h['unfunded'] = h['requested']-h['backed']
        # Точное разложение дефицита: непоставленная заявка + невыставленный спрос.
        assert abs(h['need']-h['consumed']-h['unfunded']-h['unfilled']) < 1e-5
    incoming, outgoing = defaultdict(float), defaultdict(float)
    for flow in engine.report['flows']:
        if engine.accounts[flow['recipient']]['kind'] == 'household':
            incoming[flow['reason']] += flow['gold']
        if engine.accounts[flow['sender']]['kind'] == 'household':
            outgoing[flow['reason']] += flow['gold']
    producers = []
    for f in engine.enterprises.values():
        if f['good'] not in ('grain', 'wood', 'flour', 'bread'):
            continue
        p = engine.probe.plans.get(f['id'])
        producers.append(dict(id=f['id'], good=f['good'], cash=engine.free_cash(f['id']),
            output=f['output'], profit=f['profit'], wages=f['wages'], revenue=f['revenue'],
            plan=p['plan'] if p else 0, requested=p['requested'] if p else 0,
            funding_cut=bool(p and p['plan']+1e-7 < p['requested']),
            financial_feasible=p['financial_feasible'] if p else None))
    return dict(households=engine.food_money, incoming=dict(incoming), outgoing=dict(outgoing),
        producers=producers, decisions=[d for d in row['decisions'] if d['purpose'] in ('Сырьё', 'Базовые нужды')],
        gold=engine.gold(), money_shares=engine.report['money_shares'])


def run(args):
    original_class, original_capture = research.ObservedEconomy, research.capture
    def capture(engine):
        row = original_capture(engine)
        row['food_money'] = details(engine, row)
        return row
    research.ObservedEconomy, research.capture = MoneyEconomy, capture
    try:
        research.run(args)
    finally:
        research.ObservedEconomy, research.capture = original_class, original_capture
        path = Path(args.out)/'metadata.json'
        if path.exists():
            meta = json.loads(path.read_text())
            meta['money_observer_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            path.write_text(json.dumps(meta, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--verify-state')
    parser.add_argument('--out', required=True)
    parser.add_argument('--months', type=int, default=60)
    args = parser.parse_args()
    args.warmup = 0
    args.variant = 'control'
    run(args)
