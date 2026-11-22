"""Честный benchmark доступных, но не end-to-end сопоставимых V3/V4 стадий."""

from __future__ import annotations

import argparse
import csv
import hashlib
import pickle
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


SEEDS = (17, 73129, 7, 4231)
REPEATS = 3
now = time.perf_counter_ns


def _signature(engine):
    state = (engine.tiles, engine.enterprises, engine.accounts, engine.goods, engine.meta)
    return hashlib.sha256(pickle.dumps(state)).hexdigest()


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


def _run_v3(seed: int):
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(seed, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            signature = _signature(engine)
            started = now()
            # Арифметика V3 на общих входах: заданный план и нулевой запас.
            missing = {(e["id"], good): max(0, e["capacity"] * ratio)
                       for e in engine.enterprises.values()
                       for good, ratio in engine.goods[e["good"]]["inputs"].items()
                       if ratio > 0}
            common = now() - started
            existing = list(engine.enterprises.values())
            input_ns = 0
            original = engine.input_requests

            def timed_input(self, enterprise, sells):
                nonlocal input_ns
                started = now()
                try:
                    return original(enterprise, sells)
                finally:
                    input_ns += now() - started

            engine.input_requests = types.MethodType(timed_input, engine)
            total_started = started = now()
            sells = engine.sell_orders()
            sell_seconds = now() - started
            started = now()
            engine.plan_enterprises(existing, sells)
            ended = now()
            planning_seconds = ended - started
            return signature, missing, {
                "total": ended - total_started,
                "sell_prepare": sell_seconds,
                "input_requests": input_ns,
                "planning_other": planning_seconds - input_ns,
                "timer_gaps": started - total_started - sell_seconds,
                "scalar_missing": common,
            }
        finally:
            db.close()


def _run_v4(seed: int):
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(seed, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            signature = _signature(engine)
            started = now()
            graph = load_recipe_graph()
            recipe_setup = now() - started
            total_started = started = now()
            rows = _v4_rows(engine)
            matrices = build_enterprise_matrices(rows, graph)
            matrix_end = now()
            production = calculate_enterprise_orders(matrices, graph, detailed=False)
            production_end = now()
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
            rows_end = now()
            snapshot = MarketSnapshot.from_rows(buyers, sellers)
            snapshot_end = now()
            lp_seconds = 0
            lp_calls = 0

            original_lp = v4_experiment.linprog

            def timed_lp(*args, **kwargs):
                nonlocal lp_seconds, lp_calls
                lp_started = now()
                try:
                    return original_lp(*args, **kwargs)
                finally:
                    lp_seconds += now() - lp_started
                    lp_calls += 1

            v4_experiment.linprog = timed_lp
            try:
                started = now()
                allocate_market_two_lp(snapshot)
                ended = now()
            finally:
                v4_experiment.linprog = original_lp
            assert lp_calls == 2
            # Отдельный микрозамер общей арифметики на готовых массивах.
            started_common = now()
            deficits = np.maximum(matrices.production_plan @ graph.A.T - matrices.stock, 0)
            common = now() - started_common
            np.testing.assert_allclose(deficits, production.input_orders, atol=1e-9)
            missing = {(enterprise, good): float(deficits[i, j])
                       for i, enterprise in enumerate(matrices.enterprise_ids)
                       for j, good in enumerate(matrices.good_ids) if deficits[i, j] > 0}
            return signature, missing, {
                "total": ended - total_started,
                "matrix_prepare": matrix_end - total_started,
                "production_numpy_and_diagnostics": production_end - matrix_end,
                "order_rows": rows_end - production_end,
                "snapshot": snapshot_end - rows_end,
                "lp": lp_seconds,
                "market_other": ended - started - lp_seconds,
                "timer_gaps": started - snapshot_end,
                "numpy_missing": common,
                "recipe_setup": recipe_setup,
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


SEPARATE = {"scalar_missing", "numpy_missing", "recipe_setup"}
FIELDS = ("seed", "repeat", "position", "variant", "stage", "ns")


def measure(path):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        for row in measurements():
            writer.writerow(row)


def measurements():
    for seed_index, seed in enumerate(SEEDS):
        expected_signature = None
        for repeat in range(REPEATS + 1):
            order = ("v3", "v4") if (seed_index + repeat) % 2 == 0 else ("v4", "v3")
            deficits = {}
            for position, variant in enumerate(order, 1):
                signature, deficits[variant], stages = (
                    _run_v3 if variant == "v3" else _run_v4)(seed)
                expected_signature = expected_signature or signature
                if signature != expected_signature:
                    raise ValueError(f"Разное исходное состояние: {seed}, {variant}")
                if min(stages.values()) < 0 or stages["total"] != sum(
                    ns for stage, ns in stages.items() if stage not in SEPARATE | {"total"}
                ):
                    raise ValueError(f"Не сходится бюджет времени: {seed}, {variant}")
                for stage, ns in stages.items():
                    yield seed, repeat, position, variant, stage, ns
            keys = deficits["v3"].keys() | deficits["v4"].keys()
            np.testing.assert_allclose(
                [deficits["v3"].get(key, 0) for key in keys],
                [deficits["v4"].get(key, 0) for key in keys], rtol=1e-12, atol=1e-9)


def report(path):
    groups = {}
    runs = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            run = runs.setdefault((row["seed"], row["repeat"], row["variant"]), {})
            stage, ns = row["stage"], int(row["ns"])
            if stage in run or ns < 0:
                raise ValueError("Повторная стадия или отрицательное время")
            run[stage] = ns
            if int(row["repeat"]) == 0:
                continue
            key = int(row["seed"]), row["variant"], row["stage"]
            groups.setdefault(key, []).append(ns)
    for run in runs.values():
        if run["total"] != sum(ns for stage, ns in run.items()
                               if stage not in SEPARATE | {"total"}):
            raise ValueError("Не сходится бюджет времени в CSV")
    if not groups or any(len(samples) != REPEATS for samples in groups.values()):
        raise ValueError("Неполный набор измерений")
    means = {key: float(np.mean(samples)) / 1e6 for key, samples in groups.items()}
    seeds = sorted({key[0] for key in means})
    print("| Seed | Вариант | Стадия | Среднее, мс | Доля total |")
    print("|---:|---|---|---:|---:|")
    for (seed, variant, stage), ms in sorted(means.items()):
        share = "—" if stage in SEPARATE else f"{100*ms/means[seed, variant, 'total']:.2f}%"
        print(f"| {seed} | {variant} | {stage} | {ms:.6f} | {share} |")
    for title, left, right in [
        ("Отношение времени разных рабочих нагрузок", "total", "total"),
        ("Микрозамер общей формулы дефицита", "scalar_missing", "numpy_missing"),
    ]:
        ratios = [means[s, "v3", left] / means[s, "v4", right] for s in seeds]
        low, high = map(math.exp, _ci(np.log(ratios).tolist()))
        print(f"\n{title}.\n")
        print("| Seed | V3/V4 |\n|---:|---:|")
        for seed, ratio in zip(seeds, ratios):
            print(f"| {seed} | {ratio:.3f}x |")
        print(f"\nСреднее {np.mean(ratios):.3f}x, медиана {np.median(ratios):.3f}x, "
              f"парный 95% t-интервал геометрического среднего [{low:.3f}x, {high:.3f}x].")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.report_only:
        measure(args.csv)
    report(args.csv)


if __name__ == "__main__":
    main()
