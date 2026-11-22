"""Один snapshot V4: точный solver против двух LP с unit-весами."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from scipy.optimize import linprog

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from modules.economy.v4 import (  # noqa: E402
    MarketSnapshot,
    allocate_market_exact,
    build_enterprise_matrices,
    calculate_enterprise_orders,
    load_recipe_graph,
)
from modules.economy.v4.market import MarketResult, _result_from_x  # noqa: E402


def _constraints(snapshot: MarketSnapshot) -> tuple[np.ndarray, np.ndarray]:
    n = len(snapshot.pair_buyer)
    rows: list[np.ndarray] = []
    limits: list[float] = []
    for buyer in range(len(snapshot.buyer_ids)):
        row = np.zeros(n)
        row[snapshot.pair_buyer == buyer] = 1.0
        rows.append(row)
        limits.append(float(snapshot.buyer_quantity[buyer]))
    for seller in range(len(snapshot.seller_ids)):
        row = np.zeros(n)
        row[snapshot.pair_seller == seller] = 1.0
        rows.append(row)
        limits.append(float(snapshot.seller_quantity[seller]))
    for buyer in range(len(snapshot.buyer_ids)):
        row = np.zeros(n)
        mask = snapshot.pair_buyer == buyer
        row[mask] = snapshot.pair_delivered_price[mask]
        rows.append(row)
        limits.append(float(snapshot.buyer_budget[buyer]))
    return np.asarray(rows), np.asarray(limits)


def allocate_market_two_lp(snapshot: MarketSnapshot, *, tolerance: float = 1e-8) -> MarketResult:
    """Максимум объёма, затем один weighted LP с unit-весом по рангу.

    Вес равен N-rank: он строго убывает по (limit, unmet_age, ID),
    применяется за единицу товара и намеренно не нормируется на заявку.
    Это быстрый snapshot-кандидат, а не доказательство точной лексикографии.
    """
    if not len(snapshot.pair_buyer):
        return _result_from_x(snapshot, np.zeros(0), 2, tolerance=tolerance)
    a_ub, b_ub = _constraints(snapshot)
    bounds = [(0.0, None)] * len(snapshot.pair_buyer)
    volume = linprog(
        -np.ones(len(bounds)), A_ub=a_ub, b_ub=b_ub, bounds=bounds, method="highs"
    )
    if not volume.success:
        raise ValueError(volume.message)

    ranks = np.lexsort((np.asarray(snapshot.buyer_ids), -snapshot.buyer_age,
                        -snapshot.buyer_limit))
    rank = np.empty(len(ranks), dtype=np.int64)
    rank[ranks] = np.arange(len(ranks))
    unit_weight = len(ranks) - rank
    objective = np.zeros(len(bounds))
    for buyer in range(len(snapshot.buyer_ids)):
        objective[snapshot.pair_buyer == buyer] = -float(unit_weight[buyer])
    second = linprog(
        objective,
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=np.ones((1, len(bounds))),
        b_eq=np.asarray([-volume.fun]),
        bounds=bounds,
        method="highs",
    )
    if not second.success:
        raise ValueError(second.message)
    return _result_from_x(snapshot, second.x, 2, tolerance=tolerance)


def _snapshot(seed: int) -> MarketSnapshot:
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(seed, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            rows = [
                {
                    "id": enterprise["id"],
                    "output_good": enterprise["good"],
                    "production_plan": enterprise.get("capacity", 0.0),
                    "capacity": enterprise.get("capacity", 0.0),
                    "cash": 1e9,
                    "stock": {},
                }
                for enterprise in engine.enterprises.values()
            ]
            graph = load_recipe_graph()
            matrices = build_enterprise_matrices(rows, graph)
            production = calculate_enterprise_orders(matrices, graph, detailed=False)
            buyers: list[dict[str, object]] = []
            sellers: list[dict[str, object]] = []
            for enterprise_index, enterprise_id in enumerate(matrices.enterprise_ids):
                for good_index, good in enumerate(matrices.good_ids):
                    required = float(production.input_orders[enterprise_index, good_index])
                    if required > 1e-9:
                        buyers.append({"id": f"b:{enterprise_id}:{good}", "good": good,
                                       "quantity": required, "limit": 1e9,
                                       "budget": 1e12, "unmet_age": 0})
                    available = float(production.available_output[enterprise_index, good_index])
                    if available > 1e-9:
                        sellers.append({"id": f"s:{enterprise_id}:{good}", "good": good,
                                        "quantity": available, "price": 1})
            return MarketSnapshot.from_rows(buyers, sellers)
        finally:
            db.close()


def main() -> None:
    seed = 73129
    snapshot = _snapshot(seed)
    started = time.perf_counter()
    exact = allocate_market_exact(snapshot)
    exact_seconds = time.perf_counter() - started
    started = time.perf_counter()
    candidate = allocate_market_two_lp(snapshot)
    candidate_seconds = time.perf_counter() - started

    exact_by_id = {row.order_id: row for row in exact.outcomes if row.side == "buy"}
    candidate_by_id = {row.order_id: row for row in candidate.outcomes if row.side == "buy"}
    deltas = {key: candidate_by_id[key].executed - exact_by_id[key].executed
              for key in exact_by_id}
    ranks = np.lexsort((np.asarray(snapshot.buyer_ids), -snapshot.buyer_age,
                        -snapshot.buyer_limit))
    ordered_ids = [snapshot.buyer_ids[i] for i in ranks]
    ordered_exact = [exact_by_id[key].executed for key in ordered_ids]
    ordered_candidate = [candidate_by_id[key].executed for key in ordered_ids]
    violations = []
    for index, (expected, actual) in enumerate(zip(ordered_exact, ordered_candidate)):
        if actual < expected - 1e-7:
            later_gain = any(ordered_candidate[j] > ordered_exact[j] + 1e-7
                             for j in range(index + 1, len(ordered_ids)))
            if later_gain:
                violations.append(ordered_ids[index])
    print(json.dumps({
        "seed": seed,
        "buyers": len(snapshot.buyer_ids),
        "sellers": len(snapshot.seller_ids),
        "pairs": len(snapshot.pair_buyer),
        "exact": {"volume": exact.objective_volume, "lp_calls": exact.linprog_calls,
                  "seconds": exact_seconds},
        "two_lp": {"volume": candidate.objective_volume, "lp_calls": candidate.linprog_calls,
                    "seconds": candidate_seconds},
        "volume_delta": candidate.objective_volume - exact.objective_volume,
        "orders_changed": sum(abs(value) > 1e-7 for value in deltas.values()),
        "max_order_abs_delta": max((abs(value) for value in deltas.values()), default=0.0),
        "priority_violations": len(violations),
        "first_priority_violations": violations[:10],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
