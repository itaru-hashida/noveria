"""Сопоставимый измерительный benchmark V3 и shadow V4 CSR.

Скрипт не меняет экономическую логику: V3 и V4 запускаются на свежих мирах
одного seed после одинаковой подготовки живых заявок. V4 остаётся shadow-путём.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_flow

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from modules.economy.native import match_boundary as match  # noqa: E402
from modules.economy.native import trade_boundary as trade  # noqa: E402
from modules.economy.native.enterprise_reset_boundary import reset_enterprises  # noqa: E402
from modules.economy.v4 import (  # noqa: E402
    MarketSnapshot,
    build_enterprise_matrices,
    calculate_enterprise_orders,
    load_recipe_graph,
)
from modules.economy.v4.market import _result_from_x  # noqa: E402
from scripts.experiment_v4_live_market_pass import prepare_market, state_errors  # noqa: E402
from shadow import v4_market_adapter as v4_adapter  # noqa: E402


SEEDS = (17, 73129, 7, 4231, 43, 101, 2024, 31415, 5150, 9001)
REPEATS = 10
EPS = 1e-9
SCALE = 1_000_000


def ns() -> int:
    return time.perf_counter_ns()


def input_fingerprint(buys, sells_by_good) -> str:
    sells = [row for rows in sells_by_good.values() for row in rows]
    data = [(row.get("id"), row.get("side"), row.get("good"), row.get("quantity"),
             row.get("remaining"), row.get("price"), row.get("reserved"), row.get("status"))
            for row in list(buys) + sells]
    return hashlib.sha256(json.dumps(sorted(data), sort_keys=True, default=str).encode()).hexdigest()


def aggregate_state(engine):
    stock = sum(float(quantity) for account in engine.accounts.values()
                for quantity in account.get("stock", {}).values())
    return float(engine.gold()), stock


def production_rows(engine):
    rows = []
    for enterprise in engine.enterprises.values():
        account = engine.accounts[enterprise["id"]]
        rows.append({
            "id": enterprise["id"],
            "output_good": enterprise["good"],
            "production_plan": enterprise.get("capacity", 0.0),
            "capacity": enterprise.get("capacity", 0.0),
            "cash": account.get("balance", 0.0),
            "cash_reserved": account.get("reserved", 0.0),
            "stock": dict(account.get("stock", {})),
            "active": enterprise.get("status", "active") == "active",
        })
    return rows


def time_v4_production(engine):
    graph_started = ns()
    graph = load_recipe_graph()
    graph_ns = ns() - graph_started
    rows = production_rows(engine)
    started = ns()
    matrices = build_enterprise_matrices(rows, graph)
    matrix_ns = ns() - started
    started = ns()
    result = calculate_enterprise_orders(matrices, graph, detailed=False)
    calculation_ns = ns() - started
    return graph_ns, matrix_ns, calculation_ns, result


def timed_prepare(engine):
    original = engine.plan_enterprises
    data = {"plan_ns": 0}

    def timed_plan(*args, **kwargs):
        started = ns()
        try:
            return original(*args, **kwargs)
        finally:
            data["plan_ns"] += ns() - started

    engine.plan_enterprises = timed_plan
    started = ns()
    try:
        buys, sells, info = prepare_market(engine)
    finally:
        engine.plan_enterprises = original
    info = dict(info)
    info["shared_preparation_ns"] = ns() - started
    info["v3_plan_ns"] = data["plan_ns"]
    return buys, sells, info


def run_v3_trade(engine, buys, sells_by_good):
    flat_sells = [row for rows in sells_by_good.values() for row in rows]
    original_landed = engine.landed
    original_offers = engine.offers
    original_quote = engine.quoted_cost
    original_run_match = match.run_match
    original_atomic = trade.commit_trade_atomic
    original_commit = trade.commit_trade
    landed_ns = offers_ns = quote_ns = run_match_ns = apply_ns = 0
    landed_calls = 0

    def timed_landed(*args, **kwargs):
        nonlocal landed_ns, landed_calls
        started = ns()
        try:
            return original_landed(*args, **kwargs)
        finally:
            landed_ns += ns() - started; landed_calls += 1

    def timed_offers(*args, **kwargs):
        nonlocal offers_ns
        started = ns()
        try: return original_offers(*args, **kwargs)
        finally: offers_ns += ns() - started

    def timed_quote(*args, **kwargs):
        nonlocal quote_ns
        started = ns()
        try: return original_quote(*args, **kwargs)
        finally: quote_ns += ns() - started

    def timed_match(*args, **kwargs):
        nonlocal run_match_ns
        started = ns()
        try: return original_run_match(*args, **kwargs)
        finally: run_match_ns += ns() - started

    def timed_atomic(*args, **kwargs):
        nonlocal apply_ns
        started = ns()
        try: return original_atomic(*args, **kwargs)
        finally: apply_ns += ns() - started

    def timed_commit(*args, **kwargs):
        nonlocal apply_ns
        started = ns()
        try: return original_commit(*args, **kwargs)
        finally: apply_ns += ns() - started

    engine.landed = timed_landed
    engine.offers = timed_offers
    engine.quoted_cost = timed_quote
    match.run_match = timed_match
    trade.commit_trade_atomic = timed_atomic
    trade.commit_trade = timed_commit
    started = ns()
    try:
        basic = [buy for buy in buys if buy["purpose"] == "Базовые нужды"]
        for buy in engine.shuffled(basic):
            engine.match(buy, sells_by_good, local_only=True)
        match.run_buyers(engine, engine.shuffled(list(buys)), sells_by_good, EPS,
                         trade.commit_trade, match.run_match)
    finally:
        engine.landed = original_landed; engine.offers = original_offers
        engine.quoted_cost = original_quote; match.run_match = original_run_match
        trade.commit_trade_atomic = original_atomic; trade.commit_trade = original_commit
    total = ns() - started
    search_ns = max(0, offers_ns - landed_ns + quote_ns)
    distribution_ns = max(0, run_match_ns - apply_ns)
    finalization_ns = max(0, total - landed_ns - search_ns - distribution_ns - apply_ns)
    gold_after, stock_after = aggregate_state(engine)
    return {
        "trade_total_ns": total, "routes_landed_ns": landed_ns,
        "route_search_ns": search_ns, "snapshot_ns": 0, "csr_build_ns": 0,
        "distribution_ns": distribution_ns, "apply_ns": apply_ns,
        "finalization_ns": finalization_ns, "landed_calls": landed_calls,
        "volume": sum(float(row["quantity"]) for row in engine.report["trades"]),
        "trades": len(engine.report["trades"]), "pairs": 0,
        "errors": len(state_errors(engine)), "apply_error": "",
        "gold_after": gold_after, "stock_after": stock_after,
        "produced_total": sum(float(row.get("output", 0.0)) for row in engine.enterprises.values()),
    }


def build_csr_graph(snapshot):
    buyers, sellers = len(snapshot.buyer_ids), len(snapshot.seller_ids)
    source, sink = buyers + sellers, buyers + sellers + 1
    rows, cols, values = [], [], []
    for buyer, quantity in enumerate(snapshot.buyer_quantity):
        rows.append(source); cols.append(buyer); values.append(int(round(quantity * SCALE)))
    for buyer, seller in zip(snapshot.pair_buyer, snapshot.pair_seller):
        rows.append(int(buyer)); cols.append(buyers + int(seller))
        values.append(int(round(min(snapshot.buyer_quantity[buyer], snapshot.seller_quantity[seller]) * SCALE)))
    for seller, quantity in enumerate(snapshot.seller_quantity):
        rows.append(buyers + seller); cols.append(sink); values.append(int(round(quantity * SCALE)))
    return csr_matrix((np.asarray(values, dtype=np.int64), (rows, cols)), shape=(sink + 1, sink + 1)), source, sink


def run_v4_csr(engine, buys, sells_by_good):
    flat_sells = [row for rows in sells_by_good.values() for row in rows]
    original_landed = engine.landed
    original_atomic = v4_adapter.commit_trade_atomic
    landed_ns = landed_calls = apply_atomic_ns = 0

    def timed_landed(*args, **kwargs):
        nonlocal landed_ns, landed_calls
        started = ns()
        try: return original_landed(*args, **kwargs)
        finally: landed_ns += ns() - started; landed_calls += 1

    def timed_atomic(*args, **kwargs):
        nonlocal apply_atomic_ns
        started = ns()
        try: return original_atomic(*args, **kwargs)
        finally: apply_atomic_ns += ns() - started

    engine.landed = timed_landed
    v4_adapter.commit_trade_atomic = timed_atomic
    started = ns()
    try:
        route_timings = {}
        snapshot, routes = v4_adapter.build_v4_snapshot(
            engine, buys, flat_sells, timings_ns=route_timings)
        snapshot_ns = route_timings["snapshot"]
        route_python_ns = max(0, route_timings["routes"] - landed_ns)
        graph_started = ns()
        graph, source, sink = build_csr_graph(snapshot)
        csr_build_ns = ns() - graph_started
        solve_started = ns()
        flow_result = maximum_flow(graph, source, sink)
        solve_ns = ns() - solve_started
        edge_flow = np.asarray([
            flow_result.flow[int(b), len(snapshot.buyer_ids) + int(s)] / SCALE
            for b, s in zip(snapshot.pair_buyer, snapshot.pair_seller)
        ])
        result = _result_from_x(snapshot, edge_flow, 0, tolerance=1e-8)
        apply_started = ns()
        applied = []
        apply_error = ""
        try:
            applied = v4_adapter.apply_v4_result(engine, result, routes)
        except Exception as exc:  # expose invalid CSR output instead of hiding it
            apply_error = f"{type(exc).__name__}: {exc}"
        apply_total_ns = ns() - apply_started
    finally:
        engine.landed = original_landed
        v4_adapter.commit_trade_atomic = original_atomic
    total = ns() - started
    finalization_ns = max(0, apply_total_ns - apply_atomic_ns)
    return {
        "trade_total_ns": total, "routes_landed_ns": landed_ns,
        "route_search_ns": route_python_ns, "snapshot_ns": snapshot_ns,
        "csr_build_ns": csr_build_ns, "distribution_ns": csr_build_ns + solve_ns,
        "apply_ns": apply_atomic_ns, "finalization_ns": finalization_ns,
        "landed_calls": landed_calls, "volume": float(result.objective_volume),
        "trades": len(applied), "pairs": len(snapshot.pair_buyer),
        "buyers": len(snapshot.buyer_ids), "sellers": len(snapshot.seller_ids),
        "errors": len(state_errors(engine)) + bool(apply_error),
        "apply_error": apply_error,
        "gold_after": aggregate_state(engine)[0], "stock_after": aggregate_state(engine)[1],
    }


def run_one(seed, repeat, position, variant):
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(seed, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            buys, sells, prep = timed_prepare(engine)
            fingerprint = input_fingerprint(buys, sells)
            production = {}
            if variant == "v4":
                graph_ns, matrix_ns, calculation_ns, production_result = time_v4_production(engine)
                production = {"v4_graph_ns": graph_ns, "v4_matrix_ns": matrix_ns,
                               "v4_production_ns": calculation_ns,
                               "v4_produced_total": float(production_result.available_output.sum())}
            else:
                production = {"v3_plan_ns": prep["v3_plan_ns"]}
            trade_result = (run_v3_trade if variant == "v3" else run_v4_csr)(engine, buys, sells)
            production.setdefault("v3_produced_total", sum(
                float(row.get("output", 0.0)) for row in engine.enterprises.values()))
            production.setdefault("v3_produced_total", 0.0)
            production.setdefault("v4_produced_total", 0.0)
            production["produced_total"] = production[
                "v4_produced_total" if variant == "v4" else "v3_produced_total"
            ]
            return {"seed": seed, "repeat": repeat, "position": position,
                    "variant": variant, "input_fingerprint": fingerprint,
                    "buy_orders": len(buys),
                    "sell_orders": sum(len(rows) for rows in sells.values()),
                    "shared_preparation_ns": prep["shared_preparation_ns"],
                    **production, **trade_result}
        finally:
            db.close()


def percentile(values, p):
    return float(np.percentile(np.asarray(values, dtype=np.float64) / 1e6, p))


def write_report(csv_path, report_path):
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    warm = [row for row in rows if int(row["repeat"]) > 0]
    stages = ("v3_plan_ns", "v4_matrix_ns", "shared_preparation_ns", "routes_landed_ns",
              "route_search_ns", "snapshot_ns", "csr_build_ns", "distribution_ns",
              "apply_ns", "finalization_ns", "trade_total_ns")
    lines = ["# Benchmark V3 vs V4 CSR", "",
             "Измерены свежие копии одного seed: 10 seeds, 1 прогрев и 10 тёплых повторов, порядок V3/V4 чередуется. Создание мира вне таймеров. Полный pytest не запускался.", ""]
    lines += ["## Сопоставимость", "",
             "V3 выполняет реальный `basic needs local_only` и общий matcher с применением сделок. V4 выполняет shadow snapshot, route enumeration, CSR max-flow и попытку того же `commit_trade_atomic`. На контрольном seed применение CSR завершилось ValueError из-за остатка товара; поэтому V4 trade total ниже — стоимость shadow-пути до/во время попытки применения, не завершённая торговая фаза. V4 не является полным месячным ходом: production plans, adaptive top-up и V3 observations не подключены к V4 runtime.", ""]
    lines += ["## Медиана по seed, warm repeats", "", "| Seed | V3 trade ms | V4 CSR trade ms | V3/V4 |", "|---:|---:|---:|---:|"]
    ratios = []
    for seed in sorted({int(r["seed"]) for r in warm}):
        vals = {}
        for variant in ("v3", "v4"):
            vals[variant] = float(np.median([int(r["trade_total_ns"]) for r in warm if int(r["seed"]) == seed and r["variant"] == variant])) / 1e6
        ratio = vals["v3"] / vals["v4"]; ratios.append(ratio)
        lines.append(f"| {seed} | {vals['v3']:.3f} | {vals['v4']:.3f} | {ratio:.3f}x |")
    lines += ["", f"Общая медиана парных seed ratios V3/V4: **{np.median(ratios):.3f}x**; разброс min–max `{min(ratios):.3f}…{max(ratios):.3f}`.", ""]
    failed = sum(bool(r["apply_error"]) for r in warm if r["variant"] == "v4")
    lines += ["## Стадии", "", "| Этап | V3 median ms | V4 CSR median ms | V3/V4 | V3 p95 ms | V4 p95 ms |", "|---|---:|---:|---:|---:|---:|"]
    for stage in ("shared_preparation_ns", "routes_landed_ns", "route_search_ns", "snapshot_ns", "csr_build_ns", "distribution_ns", "apply_ns", "finalization_ns", "trade_total_ns"):
        med = {}
        for variant in ("v3", "v4"):
            values = [int(r[stage]) for r in warm if r["variant"] == variant]
            med[variant] = float(np.median(values)) / 1e6
        ratio = "n/a" if med["v4"] == 0 else f"{med['v3']/med['v4']:.3f}x"
        p95_v3 = percentile([int(r[stage]) for r in warm if r["variant"] == "v3"], 95)
        p95_v4 = percentile([int(r[stage]) for r in warm if r["variant"] == "v4"], 95)
        lines.append(f"| {stage} | {med['v3']:.3f} | {med['v4']:.3f} | {ratio} | {p95_v3:.3f} | {p95_v4:.3f} |")
    v3_plan = float(np.median([int(r["v3_plan_ns"]) for r in warm if r["variant"] == "v3"])) / 1e6
    v4_matrix = float(np.median([int(r["v4_matrix_ns"]) for r in warm if r["variant"] == "v4"])) / 1e6
    lines += ["", "| Производство (неэквивалентные политики) | V3 plan_enterprises | V4 matrix calculation |", "|---|---:|---:|", f"| median ms | {v3_plan:.3f} | {v4_matrix:.3f} |", "", "Production is not assigned a speedup: V3 `plan_enterprises` and V4 matrix calculation do not implement the same economic policy. Their raw medians are retained in CSV (`v3_plan_ns` vs `v4_matrix_ns`) for reference only.", ""]
    lines += ["## Workload and correctness checks", "", "The benchmark records buy/sell counts, CSR pairs, volume, trade count and state error count per run. The input fingerprint must match between V3 and V4 for each seed/repeat; mismatches abort the run. Different trade volume or trade count is expected because V4 lacks V3 local-only/adaptive behavior and is not an equivalent full-month implementation.", ""]
    v3_produced = float(np.median([float(r["produced_total"]) for r in warm if r["variant"] == "v3"]))
    v4_produced = float(np.median([float(r["produced_total"]) for r in warm if r["variant"] == "v4"]))
    paired = {}
    for row in warm:
        paired.setdefault((row["seed"], row["repeat"]), {})[row["variant"]] = row
    gold_mismatch = sum(abs(float(pair["v3"]["gold_after"]) - float(pair["v4"]["gold_after"])) > 1e-7 for pair in paired.values())
    stock_mismatch = sum(abs(float(pair["v3"]["stock_after"]) - float(pair["v4"]["stock_after"])) > 1e-7 for pair in paired.values())
    lines += [f"Median produced total: V3 `{v3_produced:.3f}`, V4 matrix `{v4_produced:.3f}`; this is a policy/output mismatch, not a speed comparison.", f"Paired post-phase aggregate balance mismatches: gold `{gold_mismatch}`, stock `{stock_mismatch}` at tolerance `1e-7`.", ""]
    lines += ["## Conclusion", "", f"CSR application failed in {failed} of {len([r for r in warm if r['variant'] == 'v4'])} warm runs; only {len([r for r in warm if r['variant'] == 'v4']) - failed} completed. A valid full trade-phase or whole-economy speedup cannot be reported: CSR does not reliably apply its allocation, and V4 production/runtime phases are not integrated. The tables retain measured shadow-path and solver costs, but no V4/V3 acceleration coefficient is claimed.", ""]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=ROOT / "docs" / "v3_v4_csr_benchmark_raw.csv")
    parser.add_argument("--report", type=Path, default=ROOT / "docs" / "MATRIX_ECONOMY_V3_V4_CSR_BENCHMARK.md")
    args = parser.parse_args()
    fields = ["seed", "repeat", "position", "variant", "input_fingerprint", "buy_orders", "sell_orders",
              "shared_preparation_ns", "v3_plan_ns", "v4_graph_ns", "v4_matrix_ns", "v4_production_ns",
              "trade_total_ns", "routes_landed_ns", "route_search_ns", "snapshot_ns", "csr_build_ns",
              "distribution_ns", "apply_ns", "finalization_ns", "landed_calls", "pairs", "buyers", "sellers",
              "volume", "trades", "errors", "apply_error", "gold_after", "stock_after",
              "produced_total", "v3_produced_total", "v4_produced_total"]
    rows = []
    fingerprints = {}
    for seed_index, seed in enumerate(SEEDS):
        for repeat in range(REPEATS + 1):
            order = ("v3", "v4") if (seed_index + repeat) % 2 == 0 else ("v4", "v3")
            for position, variant in enumerate(order, 1):
                row = run_one(seed, repeat, position, variant)
                key = (seed, repeat)
                previous = fingerprints.setdefault(key, row["input_fingerprint"])
                if previous != row["input_fingerprint"]:
                    raise AssertionError(f"input mismatch: seed={seed}, repeat={repeat}")
                rows.append(row)
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    write_report(args.csv, args.report)
    print(f"raw_csv={args.csv}")
    print(f"report={args.report}")


if __name__ == "__main__":
    main()
