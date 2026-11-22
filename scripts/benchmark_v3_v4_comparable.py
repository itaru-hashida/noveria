"""Честный benchmark доступных, но не end-to-end сопоставимых V3/V4 стадий."""

from __future__ import annotations

import json
import math
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
from scipy.stats import t as student_t

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from modules.economy.v4 import (  # noqa: E402
    MarketSnapshot,
    build_enterprise_matrices,
    calculate_enterprise_orders,
    load_recipe_graph,
)
from scripts.experiment_v4_priority_lp import allocate_market_two_lp  # noqa: E402
import scripts.experiment_v4_priority_lp as v4_experiment  # noqa: E402


SEEDS = (7, 17, 31, 43, 101, 211, 307, 401, 509, 613, 719, 823,
         929, 1031, 1171, 1283, 1409, 1531, 1663, 1789, 1901, 2027,
         4231, 73129)
REPEATS = 3


def _v4_rows(engine: object) -> list[dict[str, object]]:
    return [
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


def _run_v3(seed: int) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(seed, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            existing = list(engine.enterprises.values())
            inner = {"input_requests": 0.0, "calls": 0}
            original = engine.input_requests

            def timed_input(self, enterprise, sells):
                started = time.perf_counter()
                try:
                    return original(enterprise, sells)
                finally:
                    inner["input_requests"] += time.perf_counter() - started
                    inner["calls"] += 1

            engine.input_requests = types.MethodType(timed_input, engine)
            started = time.perf_counter()
            sells = engine.sell_orders()
            sell_seconds = time.perf_counter() - started
            started = time.perf_counter()
            engine.plan_enterprises(existing, sells)
            planning_seconds = time.perf_counter() - started
            return {
                "total": sell_seconds + planning_seconds,
                "sell_prepare": sell_seconds,
                "planning": planning_seconds,
                "input_requests": inner["input_requests"],
                "planning_other": max(0.0, planning_seconds - inner["input_requests"]),
                "input_calls": float(inner["calls"]),
            }
        finally:
            db.close()


def _run_v4(seed: int) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(seed, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            graph = load_recipe_graph()
            started = time.perf_counter()
            rows = _v4_rows(engine)
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
            prepare_seconds = time.perf_counter() - started

            started = time.perf_counter()
            snapshot = MarketSnapshot.from_rows(buyers, sellers)
            snapshot_seconds = time.perf_counter() - started
            lp_seconds = 0.0
            lp_calls = 0

            original_lp = v4_experiment.linprog

            def timed_lp(*args, **kwargs):
                nonlocal lp_seconds, lp_calls
                lp_started = time.perf_counter()
                try:
                    return original_lp(*args, **kwargs)
                finally:
                    lp_seconds += time.perf_counter() - lp_started
                    lp_calls += 1

            v4_experiment.linprog = timed_lp
            try:
                started = time.perf_counter()
                allocation = allocate_market_two_lp(snapshot)
                market_seconds = time.perf_counter() - started
            finally:
                v4_experiment.linprog = original_lp
            return {
                "total": prepare_seconds + snapshot_seconds + market_seconds,
                "prepare": prepare_seconds,
                "snapshot": snapshot_seconds,
                "market": market_seconds,
                "lp": lp_seconds,
                "market_other": max(0.0, market_seconds - lp_seconds),
                "lp_calls": float(lp_calls),
                "buyers": float(len(buyers)),
                "sellers": float(len(sellers)),
                "pairs": float(len(snapshot.pair_buyer)),
                "volume": allocation.objective_volume,
            }
        finally:
            db.close()


def _ci(values: list[float]) -> tuple[float, float]:
    mean = float(np.mean(values))
    if len(values) < 2:
        return mean, mean
    half = float(student_t.ppf(0.975, len(values) - 1) * np.std(values, ddof=1) /
                 math.sqrt(len(values)))
    return mean - half, mean + half


def main() -> None:
    measurements: list[dict[str, object]] = []
    for seed_index, seed in enumerate(SEEDS):
        # Прогрев не входит в статистику; порядок прогрева тоже чередуем.
        warm_order = ("v3", "v4") if seed_index % 2 == 0 else ("v4", "v3")
        for variant in warm_order:
            (_run_v3 if variant == "v3" else _run_v4)(seed)
        for repeat in range(REPEATS):
            order = ("v3", "v4") if (seed_index + repeat) % 2 == 0 else ("v4", "v3")
            values: dict[str, dict[str, float]] = {}
            for variant in order:
                values[variant] = (_run_v3 if variant == "v3" else _run_v4)(seed)
            measurements.append({"seed": seed, "repeat": repeat + 1, **values})

    by_seed = []
    for seed in SEEDS:
        rows = [row for row in measurements if row["seed"] == seed]
        v3 = [row["v3"]["total"] for row in rows]
        v4 = [row["v4"]["total"] for row in rows]
        ratios = [a / b for a, b in zip(v3, v4)]
        by_seed.append({"seed": seed, "v3_mean": float(np.mean(v3)),
                        "v4_mean": float(np.mean(v4)),
                        "speedup": float(np.mean(ratios)),
                        "v3_v4_delta": float(np.mean(v3) - np.mean(v4))})

    speedups = [row["speedup"] for row in by_seed]
    log_speedups = np.log(speedups)
    log_ci = _ci(log_speedups.tolist())
    paired_deltas = [row["v3_v4_delta"] for row in by_seed]
    v4_stage = {key: float(np.mean([row["v4"][key] for row in measurements]))
                for key in ("prepare", "snapshot", "lp", "market_other")}
    v4_total = sum(v4_stage.values())
    output = {
        "seeds": list(SEEDS), "repeats": REPEATS,
        "warmup": "one run per variant per seed, excluded",
        "alternation": "V3/V4 order alternated by seed and repeat",
        "paired_by_seed": by_seed,
        "speedup_mean": float(np.mean(speedups)),
        "speedup_median": float(np.median(speedups)),
        "speedup_paired_95ci": [math.exp(log_ci[0]), math.exp(log_ci[1])],
        "paired_delta_seconds_95ci": list(_ci(paired_deltas)),
        "v4_stage_mean_seconds": v4_stage,
        "v4_stage_percent": {key: 100.0 * value / v4_total for key, value in v4_stage.items()},
        "notes": [
            "V4 is an isolated snapshot; its preparation consumes synthetic rows derived from the initial V3 engine.",
            "V3/V4 are not full-turn equivalent and this result is not an end-to-end speedup.",
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
