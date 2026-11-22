"""Ограниченный эксперимент CSR для денежных ограничений V4."""

from __future__ import annotations

import json
import sys
import tempfile
from time import perf_counter_ns
from pathlib import Path

import numpy as np
from scipy.sparse import bmat, csr_matrix
from scipy.sparse.csgraph import connected_components

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit.scenario_runner import build_world
from modules.economy.v4.market import allocate_market_exact, _result_from_x
from modules.economy.v4.residual import allocate_market_residual, allocate_residual
from scripts.experiment_v4_csr import max_flow_capacity
from scripts.experiment_v4_live_market_pass import prepare_market
from shadow.v4_market_adapter import apply_v4_result, build_v4_snapshot


def classify(snapshot, tolerance=1e-8):
    allowed = csr_matrix(
        (np.ones(len(snapshot.pair_buyer)),
         (snapshot.pair_buyer, snapshot.pair_seller)),
        shape=(len(snapshot.buyer_ids), len(snapshot.seller_ids)),
    )
    graph = bmat([[None, allowed], [allowed.T, None]], format="csr")
    count, labels = connected_components(graph, directed=False)
    components = []
    for component in range(count):
        buyers = np.flatnonzero(labels[:len(snapshot.buyer_ids)] == component)
        edges = np.flatnonzero(np.isin(snapshot.pair_buyer, buyers))
        variable_budget = []
        for buyer in buyers:
            prices = snapshot.pair_delivered_price[
                snapshot.pair_buyer == buyer
            ]
            if not len(prices):
                continue
            nonbinding = (
                snapshot.buyer_budget[buyer]
                >= snapshot.buyer_quantity[buyer] * prices.max() - tolerance
            )
            constant_price = np.ptp(prices) <= tolerance
            if not (nonbinding or constant_price):
                variable_budget.append(int(buyer))
        components.append({
            "component": int(component),
            "buyers": int(len(buyers)),
            "sellers": int(np.count_nonzero(
                labels[len(snapshot.buyer_ids):] == component
            )),
            "edges": int(len(edges)),
            "kind": "max_flow" if not variable_budget else "budgeted",
            "variable_budget_buyers": [
                snapshot.buyer_ids[index] for index in variable_budget
            ],
        })
    return components


def budget_report(snapshot, flow, tolerance=1e-7):
    spending = np.bincount(
        snapshot.pair_buyer,
        weights=flow * snapshot.pair_delivered_price,
        minlength=len(snapshot.buyer_ids),
    )
    violations = np.flatnonzero(
        spending > snapshot.buyer_budget + tolerance
    )
    return {
        "count": int(len(violations)),
        "buyers": [
            {
                "id": snapshot.buyer_ids[index],
                "spending": float(spending[index]),
                "budget": float(snapshot.buyer_budget[index]),
                "excess": float(spending[index] - snapshot.buyer_budget[index]),
            }
            for index in violations
        ],
    }


def result_flow(snapshot, result):
    edge_index = {
        (snapshot.buyer_ids[int(buyer)], snapshot.seller_ids[int(seller)]): edge
        for edge, (buyer, seller) in enumerate(
            zip(snapshot.pair_buyer, snapshot.pair_seller)
        )
    }
    flow = np.zeros(len(snapshot.pair_buyer), dtype=np.float64)
    for allocation in result.allocations:
        flow[edge_index[(allocation.buyer_id, allocation.seller_id)]] += (
            allocation.quantity
        )
    return flow


def priority_differences(snapshot, actual, exact):
    rank = np.lexsort((snapshot.buyer_ids, -snapshot.buyer_age,
                       -snapshot.buyer_limit))
    delta = actual.buyer_filled[rank] - exact.buyer_filled[rank]
    violations = []
    for index in range(len(rank)):
        if delta[index] < -1e-7 and np.any(delta[index + 1:] > 1e-7):
            violations.append(snapshot.buyer_ids[rank[index]])
    return {
        "changed_buyers": int(np.count_nonzero(abs(delta) > 1e-7)),
        "priority_violations": violations,
        "first_differences": [
            [snapshot.buyer_ids[rank[index]], float(delta[index])]
            for index in np.flatnonzero(abs(delta) > 1e-7)[:10]
        ],
    }


def solve_fixed(snapshot):
    exact_started = perf_counter_ns()
    exact = allocate_market_exact(snapshot)
    exact_ms = (perf_counter_ns() - exact_started) / 1e6
    flow_started = perf_counter_ns()
    flow = allocate_residual(snapshot)
    flow_ms = (perf_counter_ns() - flow_started) / 1e6
    result = allocate_market_residual(snapshot)
    return exact, exact_ms, flow, flow_ms, result


def main():
    with tempfile.TemporaryDirectory() as directory:
        db, service = build_world(73129, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            buys, sells, _ = prepare_market(engine)
            snapshot_started = perf_counter_ns()
            snapshot, routes = build_v4_snapshot(
                engine, buys, (order for group in sells.values() for order in group)
            )
            snapshot_ms = (perf_counter_ns() - snapshot_started) / 1e6
            exact, exact_ms, flow, flow_ms, result = solve_fixed(snapshot)
            component_rows = classify(snapshot)
            # scipy.maximum_flow uses a bounded integer capacity type.  Use the
            # largest scale that stays safe for this snapshot; the residual is
            # reported with a quantisation tolerance below.
            maxflow_scale = 1_000_000
            maxflow = max_flow_capacity(snapshot, scale=maxflow_scale)
            maxflow_result = _result_from_x(
                snapshot, maxflow["flow"], 0, tolerance=1e-8
            )
            exact_report = {
                "volume": float(exact.objective_volume),
                "budget": budget_report(snapshot, result_flow(snapshot, exact)),
            }
            maxflow_result = {
                "volume": maxflow["volume"],
                "budget": budget_report(
                    snapshot, maxflow["flow"], tolerance=2.0 / maxflow_scale
                ),
                "priority": priority_differences(
                    snapshot, maxflow_result, exact
                ),
            }
            repaired_result = {
                "volume": float(result.objective_volume),
                "loss": float(exact.objective_volume - result.objective_volume),
                "budget": budget_report(snapshot, flow),
                "priority": priority_differences(snapshot, result, exact),
            }
            apply_started = perf_counter_ns()
            applied = apply_v4_result(engine, result, routes)
            apply_ms = (perf_counter_ns() - apply_started) / 1e6
            print(json.dumps({
                "seed": 73129,
                "snapshot_ms": snapshot_ms,
                "solver_ms": {
                    "old_reference": 2.26,
                    "budget_repair": flow_ms,
                    "exact_lp": exact_ms,
                    "csr_max_flow": maxflow["time_ms"],
                },
                "components": {
                    "total": len(component_rows),
                    "max_flow": sum(
                        row["kind"] == "max_flow" for row in component_rows
                    ),
                    "budgeted": sum(
                        row["kind"] == "budgeted" for row in component_rows
                    ),
                    "variable_budget_buyers": [
                        buyer
                        for row in component_rows
                        for buyer in row["variable_budget_buyers"]
                    ],
                },
                "max_flow": maxflow_result,
                "budget_repair": repaired_result,
                "exact": exact_report,
                "full_phase_fixed_snapshot_ms": flow_ms + apply_ms,
                "full_phase_including_snapshot_ms": (
                    snapshot_ms + flow_ms + apply_ms
                ),
                "apply_ms": apply_ms,
                "applied_trades": len(applied),
            }, ensure_ascii=False, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    main()
