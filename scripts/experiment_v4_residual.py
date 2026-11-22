"""Сравнение на общих снимках. Запуск из корня: python -m scripts.experiment_v4_residual."""

import json
import tempfile
from time import perf_counter_ns

import numpy as np

from audit.scenario_runner import build_world
from modules.economy.v4.market import MarketSnapshot, allocate_market, allocate_market_exact
from modules.economy.v4.residual import allocate_market_residual, allocate_residual
from scripts.experiment_v4_live_market_pass import prepare_market
from scripts.experiment_v4_priority_lp import _synthetic_snapshots
from shadow.v4_greedy_matcher import allocate_market_greedy
from shadow.v4_market_adapter import apply_v4_result, build_v4_snapshot


def compare(label, snapshot, oracle=allocate_market_exact):
    start = perf_counter_ns()
    exact = oracle(snapshot)
    exact_ms = (perf_counter_ns() - start) / 1e6
    start = perf_counter_ns()
    greedy = allocate_market_greedy(snapshot)
    greedy_ms = (perf_counter_ns() - start) / 1e6
    start = perf_counter_ns()
    flow = allocate_residual(snapshot)
    repair_ms = (perf_counter_ns() - start) / 1e6
    b, s, price = snapshot.pair_buyer, snapshot.pair_seller, snapshot.pair_delivered_price
    fills = np.bincount(b, weights=flow, minlength=len(snapshot.buyer_ids))
    sales = np.bincount(s, weights=flow, minlength=len(snapshot.seller_ids))
    spend = np.bincount(b, weights=flow * price, minlength=len(fills))
    assert np.all(np.isfinite(flow)) and np.all(flow >= -1e-8)
    assert np.all(fills <= snapshot.buyer_quantity + 1e-7)
    assert np.all(sales <= snapshot.seller_quantity + 1e-7)
    assert np.all(spend <= snapshot.buyer_budget + 1e-7)
    assert np.all(price[flow > 0] <= snapshot.buyer_limit[b[flow > 0]] + 1e-8)
    assert np.all(fills >= greedy.buyer_filled - 1e-7)
    assert flow.sum() <= exact.objective_volume + 1e-7
    ranked = np.lexsort((snapshot.buyer_ids, -snapshot.buyer_age, -snapshot.buyer_limit))
    result = {"case": label, "edges": len(b), "constraint_errors": 0}
    for name, volume, executed, ms in (
        ("exact", exact.objective_volume, exact.buyer_filled, exact_ms),
        ("greedy", greedy.objective_volume, greedy.buyer_filled, greedy_ms),
        ("repair", flow.sum(), fills, repair_ms),
    ):
        result[name] = dict(volume=volume, loss=exact.objective_volume - volume, ms=ms,
                            changed_buyers=int(np.count_nonzero(
                                np.abs(executed - exact.buyer_filled) > 1e-7)))
        if len(executed) <= 10:
            result[name]["fills"] = dict(zip(snapshot.buyer_ids, executed.tolist()))
        delta = executed[ranked] - exact.buyer_filled[ranked]
        changed = np.flatnonzero(abs(delta) > 1e-7)
        if len(changed):
            i = changed[0]
            result[name]["first_rank_difference"] = [snapshot.buyer_ids[ranked[i]], delta[i]]
    print(json.dumps(result))
    return flow


def main():
    counterexample = MarketSnapshot.from_rows(
        [dict(id="high", good="x", quantity=1, limit=10, budget=10),
         dict(id="low", good="x", quantity=10, limit=5, budget=50)],
        [dict(id="cheap", good="x", quantity=10, price=5),
         dict(id="dear", good="x", quantity=1, price=10)],
    )
    assert abs(compare("10_to_11", counterexample).sum() - 11) < 1e-8
    for label, snapshot in _synthetic_snapshots():
        compare(label, snapshot)
    # c → s0 → a → s1 → b → s2 требует пяти рёбер.
    longer_path = MarketSnapshot.from_rows(
        [dict(id=b, good="x", quantity=1, limit=3, budget=3, unmet_age=2-i)
         for i, b in enumerate("abc")],
        [dict(id=s, good="x", quantity=1, price=i+1) for i, s in enumerate("012")],
        [dict(buyer_id=b, seller_id=s, good="x", reachable=s in allowed)
         for b, allowed in zip("abc", ("01", "12", "0")) for s in "012"],
    )
    assert abs(compare("five_edges_required", longer_path).sum() - 3) < 1e-8
    rng = np.random.default_rng(73129)
    random_losses = []
    for case in range(12):
        buyers = [
            dict(id=f"rb{case}-{i}", good="x", quantity=float(rng.integers(1, 7)),
                 limit=float(rng.integers(3, 12)), budget=float(rng.integers(4, 35)),
                 unmet_age=float(rng.integers(0, 4)))
            for i in range(4)
        ]
        sellers = [
            dict(id=f"rs{case}-{i}", good="x", quantity=float(rng.integers(1, 7)),
                 price=float(rng.integers(1, 10)))
            for i in range(4)
        ]
        routes = [
            dict(buyer_id=buyer["id"], seller_id=seller["id"], good="x",
                 reachable=bool(rng.integers(0, 2)))
            for buyer in buyers for seller in sellers
        ]
        snapshot = MarketSnapshot.from_rows(buyers, sellers, routes)
        flow = compare(f"random_{case}", snapshot)
        exact = allocate_market_exact(snapshot)
        random_losses.append(float(exact.objective_volume - flow.sum()))
    print(json.dumps({
        "random_cases": 12,
        "random_max_volume_loss": max(random_losses, default=0.0),
        "random_nonoptimal_cases": sum(loss > 1e-7 for loss in random_losses),
    }))
    with tempfile.TemporaryDirectory() as directory:
        db, service = build_world(73129, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            buys, sells, _ = prepare_market(engine)
            snapshot, routes = build_v4_snapshot(
                engine, buys, (order for group in sells.values() for order in group))
            flow = compare("73129", snapshot, oracle=allocate_market)
            result = allocate_market_residual(snapshot)
            stock, gold = engine.total_stock(), engine.gold()
            start = perf_counter_ns()
            trades = apply_v4_result(engine, result, routes)
            apply_ms = (perf_counter_ns() - start) / 1e6
            assert abs(sum(row["quantity"] for row in trades) - flow.sum()) < 1e-7
            assert abs(engine.gold() - gold) < 1e-7
            assert all(abs(value - stock[good]) < 1e-7
                       for good, value in engine.total_stock().items())
            print(json.dumps(dict(trades=len(trades), apply_ms=apply_ms)))
        finally:
            db.close()


if __name__ == "__main__":
    main()
