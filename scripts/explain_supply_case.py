"""Один воспроизводимый разбор продажи склада после нулевого плана."""
import argparse
import copy
import hashlib
import json
import pickle
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.economy import Economy
from scripts.research_agents import Probe
from scripts.research_supply_response import SupplyEconomy, diagnostic


def explain(checkpoint):
    path = Path(checkpoint)
    engine = pickle.loads(path.read_bytes())
    reference = copy.deepcopy(engine)
    reference.__class__ = Economy
    engine.__class__ = SupplyEconomy
    engine.probe = Probe('control')
    engine.probe.begin(1)
    engine.step(); reference.step()
    for key in ('accounts', 'enterprises', 'meta'):
        assert getattr(engine, key) == getattr(reference, key)
    assert engine.rng.getstate() == reference.rng.getstate()
    decisions = {d['id']: d for d in engine.probe.decisions}
    cases = []
    for f in diagnostic(engine):
        if not f['zero_plan_sold_out']:
            continue
        trades = []
        known = set(engine.probe.plans[f['id']]['known_ids'])
        for t in engine.report['trades']:
            if t['seller'] != f['id'] or t['good'] != f['good']:
                continue
            d = decisions[t['buy_order']]
            landed = (t['gold']+t['freight'])/t['quantity']
            trades.append(dict(buyer=t['buyer'], buy_order=t['buy_order'], quantity=t['quantity'],
                known_at_planning=t['buy_order'] in known, original_limit=d['original_limit'],
                execution_limit=d['execution_limit'], actual_landed_price=landed,
                late_bid=d['original_limit']+1e-7 < landed))
        cases.append(dict(f, late_bid_quantity=sum(t['quantity'] for t in trades if t['late_bid'] and t['known_at_planning']),
            trade_count=len(trades), largest_trades=sorted(trades, key=lambda t: -t['quantity'])[:5]))
    return dict(checkpoint_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        month=engine.meta['month'], seed=engine.seed, observer_state_and_rng_parity=True, cases=cases)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    result = explain(args.checkpoint)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
