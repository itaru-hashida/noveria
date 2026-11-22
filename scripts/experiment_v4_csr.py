"""CSR/max-flow prototype for the V4 market snapshot."""

from __future__ import annotations

import json
import sys
import tempfile
from time import perf_counter_ns
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_flow

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit.scenario_runner import build_world
from modules.economy.v4.market import allocate_market_exact
from modules.economy.v4.residual import allocate_residual
from scripts.experiment_v4_live_market_pass import prepare_market
from shadow.v4_market_adapter import build_v4_snapshot


def csr_allowed(snapshot):
    return csr_matrix(
        (snapshot.pair_delivered_price,
         (snapshot.pair_buyer, snapshot.pair_seller)),
        shape=(len(snapshot.buyer_ids), len(snapshot.seller_ids)),
    )


def max_flow_capacity(snapshot, scale=1_000_000):
    buyers = len(snapshot.buyer_ids)
    sellers = len(snapshot.seller_ids)
    source = buyers + sellers
    sink = source + 1
    rows, cols, values = [], [], []
    for buyer, quantity in enumerate(snapshot.buyer_quantity):
        rows.append(source)
        cols.append(buyer)
        values.append(int(round(quantity * scale)))
    for edge, (buyer, seller) in enumerate(
        zip(snapshot.pair_buyer, snapshot.pair_seller)
    ):
        rows.append(int(buyer))
        cols.append(buyers + int(seller))
        values.append(int(round(
            min(snapshot.buyer_quantity[buyer], snapshot.seller_quantity[seller])
            * scale
        )))
    for seller, quantity in enumerate(snapshot.seller_quantity):
        rows.append(buyers + seller)
        cols.append(sink)
        values.append(int(round(quantity * scale)))
    graph = csr_matrix((np.asarray(values, dtype=np.int64), (rows, cols)),
                       shape=(sink + 1, sink + 1))
    started = perf_counter_ns()
    result = maximum_flow(graph, source, sink)
    elapsed_ms = (perf_counter_ns() - started) / 1e6
    edge_flow = np.asarray([
        result.flow[int(buyer), buyers + int(seller)] / scale
        for buyer, seller in zip(snapshot.pair_buyer, snapshot.pair_seller)
    ])
    spending = np.bincount(
        snapshot.pair_buyer,
        weights=edge_flow * snapshot.pair_delivered_price,
        minlength=buyers,
    )
    return {
        "volume": float(result.flow[source].sum() / scale),
        "flow": edge_flow,
        "time_ms": elapsed_ms,
        "budget_violations": int(np.count_nonzero(
            spending > snapshot.buyer_budget + 1e-7
        )),
    }


def old_repair(snapshot):
    buyer, seller = snapshot.pair_buyer, snapshot.pair_seller
    price = snapshot.pair_delivered_price
    demand = snapshot.buyer_quantity.copy()
    supply = snapshot.seller_quantity.copy()
    budget = snapshot.buyer_budget.copy()
    rank_order = np.lexsort((snapshot.buyer_ids, -snapshot.buyer_age,
                             -snapshot.buyer_limit))
    rank = np.empty(len(rank_order), dtype=np.int64)
    rank[rank_order] = np.arange(len(rank_order))
    flow = np.zeros(len(buyer))
    ordered = np.lexsort((seller, price, buyer))
    for _ in range(64):
        cap = np.minimum(
            np.minimum(demand[buyer], supply[seller]),
            np.divide(budget[buyer], price, out=np.full_like(price, np.inf),
                      where=price > 0),
        )
        available = ordered[cap[ordered] > 1e-8]
        if not len(available):
            break
        chosen = available[np.unique(buyer[available], return_index=True)[1]]
        seller_rank = np.full(len(snapshot.seller_ids), len(rank))
        np.minimum.at(seller_rank, seller[chosen], rank[buyer[chosen]])
        chosen = chosen[rank[buyer[chosen]] == seller_rank[seller[chosen]]]
        quantity = cap[chosen]
        flow[chosen] += quantity
        demand[buyer[chosen]] -= quantity
        supply[seller[chosen]] -= quantity
        budget[buyer[chosen]] -= quantity * price[chosen]
    return flow


def timed(function, repeats=5):
    function()
    values = []
    for _ in range(repeats):
        started = perf_counter_ns()
        result = function()
        values.append((perf_counter_ns() - started) / 1e6)
    return result, float(np.median(values)), values


def main():
    with tempfile.TemporaryDirectory() as directory:
        db, service = build_world(73129, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            buys, sells, _ = prepare_market(engine)
            snapshot, _ = build_v4_snapshot(
                engine, buys, (order for group in sells.values() for order in group)
            )
            csr = csr_allowed(snapshot)
            print("snapshot ready", flush=True)
            exact_started = perf_counter_ns()
            exact = allocate_market_exact(snapshot)
            exact_ms = (perf_counter_ns() - exact_started) / 1e6
            print("exact ready", flush=True)
            old_flow, old_ms, old_runs = timed(lambda: old_repair(snapshot))
            print("old ready", flush=True)
            residual_flow, residual_ms, residual_runs = timed(
                lambda: allocate_residual(snapshot)
            )
            print("residual ready", flush=True)
            maxflow = max_flow_capacity(snapshot)
            print("maxflow ready", flush=True)
            result = {
                "seed": 73129,
                "snapshot": {
                    "buyers": len(snapshot.buyer_ids),
                    "sellers": len(snapshot.seller_ids),
                    "allowed_edges": len(snapshot.pair_buyer),
                    "csr_shape": list(csr.shape),
                    "csr_nnz": int(csr.nnz),
                    "csr_data_bytes": int(csr.data.nbytes),
                },
                "algorithms": {
                    "old_repair": {
                        "median_ms": old_ms,
                        "runs_ms": old_runs,
                        "volume": float(old_flow.sum()),
                        "loss": float(exact.objective_volume - old_flow.sum()),
                    },
                    "bfs_repair": {
                        "median_ms": residual_ms,
                        "runs_ms": residual_runs,
                        "volume": float(residual_flow.sum()),
                        "loss": float(exact.objective_volume - residual_flow.sum()),
                    },
                    "csr_max_flow": {
                        "time_ms": maxflow["time_ms"],
                        "volume": maxflow["volume"],
                        "loss": float(exact.objective_volume - maxflow["volume"]),
                        "budget_violations": maxflow["budget_violations"],
                    },
                    "exact_lp": {
                        "time_ms": exact_ms,
                        "volume": exact.objective_volume,
                    },
                },
                "csr_scope": {
                    "guaranteed": "max volume when budgets are nonbinding, or when one constant price per buyer converts budget to source capacity",
                    "blocked": "buyer budgets combined with different delivered prices",
                },
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    main()
