"""Короткий профиль live V4 market на seed 73129 без изменения solver-а."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import sparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from scripts.experiment_v4_live_market_pass import prepare_market  # noqa: E402
from scripts import experiment_v4_priority_lp as candidate  # noqa: E402
from shadow.v4_market_adapter import build_v4_snapshot  # noqa: E402


def median(values: list[int]) -> float:
    return float(np.median(np.asarray(values, dtype=np.float64)))


def profile_snapshot(engine, buys, sells, repeats=5):
    rows = []
    cold = None
    for index in range(repeats + 1):
        landed_ns = 0
        landed_calls = 0
        cache_hits = 0
        tested_pairs = 0
        original_landed = engine.landed

        def timed_landed(seller_tile, buyer_tile, good, price):
            nonlocal landed_ns, landed_calls, cache_hits
            key = (seller_tile, buyer_tile, good)
            before = engine.get_landed_cache_entry(key)
            started = time.perf_counter_ns()
            result = original_landed(seller_tile, buyer_tile, good, price)
            landed_ns += time.perf_counter_ns() - started
            landed_calls += 1
            if before is not None:
                cache_hits += 1
            return result

        engine.landed = timed_landed
        started = time.perf_counter_ns()
        for buy in buys:
            for sell in sells:
                if buy["good"] == sell["good"]:
                    tested_pairs += 1
        pair_loop_ns = time.perf_counter_ns() - started
        timings: dict[str, int] = {}
        try:
            snapshot_started = time.perf_counter_ns()
            snapshot, _ = build_v4_snapshot(
                engine, buys, sells, timings_ns=timings
            )
            total_ns = time.perf_counter_ns() - snapshot_started
        finally:
            engine.landed = original_landed
        row = dict(
            total=total_ns,
            route_total=timings["routes"],
            snapshot=timings["snapshot"],
            landed=landed_ns,
            python_route=max(0, timings["routes"] - landed_ns),
            pair_loop=pair_loop_ns,
            tested_pairs=tested_pairs,
            landed_calls=landed_calls,
            cache_hits=cache_hits,
            snapshot_obj=snapshot,
        )
        if index == 0:
            cold = row
        else:
            rows.append(row)
    return cold, rows


def profile_two_lp(snapshot, repeats=5):
    measurements = []
    matrix_info = None
    original_constraints = candidate._constraints
    original_linprog = candidate.linprog
    for index in range(repeats + 1):
        constraint_ns = 0
        lp_ns: list[int] = []
        call_shapes: list[tuple[int, int]] = []
        call_nnz: list[int] = []

        def timed_constraints(value):
            nonlocal constraint_ns, matrix_info
            started = time.perf_counter_ns()
            a_ub, b_ub = original_constraints(value)
            constraint_ns += time.perf_counter_ns() - started
            matrix_info = {
                "shape": list(a_ub.shape),
                "nnz": int(np.count_nonzero(a_ub)),
                "dense_bytes": int(a_ub.nbytes),
            }
            return a_ub, b_ub

        def timed_linprog(*args, **kwargs):
            started = time.perf_counter_ns()
            result = original_linprog(*args, **kwargs)
            lp_ns.append(time.perf_counter_ns() - started)
            a_ub = kwargs.get("A_ub")
            if a_ub is not None:
                call_shapes.append(tuple(a_ub.shape))
                call_nnz.append(int(np.count_nonzero(a_ub)))
            return result

        candidate._constraints = timed_constraints
        candidate.linprog = timed_linprog
        try:
            result = candidate.allocate_market_two_lp(snapshot)
        finally:
            candidate._constraints = original_constraints
            candidate.linprog = original_linprog
        row = {
            "constraints": constraint_ns,
            "linprog_first": lp_ns[0],
            "linprog_second": lp_ns[1],
            "total": constraint_ns + sum(lp_ns),
            "result": result,
            "call_shapes": call_shapes,
            "call_nnz": call_nnz,
        }
        if index:
            measurements.append(row)
    return matrix_info, measurements


def grouped_estimate(snapshot, a_ub):
    by_good: dict[int, list[int]] = defaultdict(list)
    for edge, buyer in enumerate(snapshot.pair_buyer):
        by_good[int(snapshot.buyer_good[buyer])].append(edge)
    grouped_dense = 0
    grouped_csr = 0
    groups = []
    for good, edges in by_good.items():
        buyers = set(int(snapshot.pair_buyer[e]) for e in edges)
        sellers = set(int(snapshot.pair_seller[e]) for e in edges)
        shape = (2 * len(buyers) + len(sellers), len(edges))
        dense = shape[0] * shape[1] * 8
        grouped_dense += dense
        grouped_csr += int(sparse.csr_matrix(a_ub[:, edges]).data.nbytes
                            + sparse.csr_matrix(a_ub[:, edges]).indices.nbytes
                            + sparse.csr_matrix(a_ub[:, edges]).indptr.nbytes)
        groups.append({"good": snapshot.good_ids[good], "shape": list(shape), "edges": len(edges)})
    csr = sparse.csr_matrix(a_ub)
    csr_bytes = int(csr.data.nbytes + csr.indices.nbytes + csr.indptr.nbytes)
    return {
        "groups": len(groups),
        "group_details": groups,
        "grouped_dense_bytes": grouped_dense,
        "dense_bytes": int(a_ub.nbytes),
        "dense_reduction": 1 - grouped_dense / a_ub.nbytes,
        "csr_bytes": csr_bytes,
        "csr_reduction": 1 - csr_bytes / a_ub.nbytes,
        "grouped_csr_bytes": grouped_csr,
    }


def main():
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(73129, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            buys, sells_by_good, _ = prepare_market(engine)
            sells = [sell for rows in sells_by_good.values() for sell in rows]
            cold, route_rows = profile_snapshot(engine, buys, sells)
            snapshot = route_rows[0]["snapshot_obj"]
            matrix_info, lp_rows = profile_two_lp(snapshot)
            a_ub, _ = candidate._constraints(snapshot)
            for row in route_rows:
                row.pop("snapshot_obj", None)
            for row in lp_rows:
                row.pop("result", None)
            result = {
                "seed": 73129,
                "orders": {"buyers": len(buys), "sellers": len(sells)},
                "cold_routes": {k: v for k, v in cold.items() if k != "snapshot_obj"},
                "warm_route_median": {key: median([row[key] for row in route_rows])
                                       for key in ("total", "route_total", "snapshot", "landed", "python_route", "pair_loop")},
                "warm_route_counts": {key: route_rows[0][key] for key in ("tested_pairs", "landed_calls", "cache_hits")},
                "matrix": matrix_info,
                "warm_two_lp_median": {key: median([row[key] for row in lp_rows])
                                        for key in ("constraints", "linprog_first", "linprog_second", "total")},
                "lp_calls": 2,
                "grouping_csr_estimate": grouped_estimate(snapshot, a_ub),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        finally:
            db.close()


if __name__ == "__main__":
    main()
