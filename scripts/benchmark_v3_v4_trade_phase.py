"""Контрольный benchmark торговой фазы V3 и shadow V4.

Мир и заявки создаются вне таймеров. V3/V4 получают независимые свежие копии
одного seed и выполняют свои реальные торговые правила без подмены экономики.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from modules.economy.native import match_boundary as match  # noqa: E402
from modules.economy.native import trade_boundary as trade  # noqa: E402
from shadow import v4_market_adapter as v4_adapter  # noqa: E402
from scripts.experiment_v4_live_market_pass import prepare_market  # noqa: E402


SEEDS = (17, 73129, 7, 4231)
REPEATS = 5
EPS = 1e-9


def _warm_routes(engine, buys, sells):
    for buy in buys:
        for sell in sells:
            if buy["good"] == sell["good"] and buy["participant"] != sell["participant"]:
                engine.landed(sell["tile_id"], buy["tile_id"], buy["good"], sell["price"])


def _cold_routes(engine):
    engine.__dict__.pop("_landed_cache", None)
    engine.invalidate_market_books()


def _install_landed_timer(engine):
    original = engine.landed
    data = {"ns": 0, "calls": 0}

    def timed(*args, **kwargs):
        started = time.perf_counter_ns()
        result = original(*args, **kwargs)
        data["ns"] += time.perf_counter_ns() - started
        data["calls"] += 1
        return result

    engine.landed = timed
    return original, data


def run_v3(engine, buys, sells, *, warm: bool) -> dict[str, int]:
    flat_sells = [row for rows in sells.values() for row in rows]
    if warm:
        _warm_routes(engine, buys, flat_sells)
    else:
        _cold_routes(engine)
    original_landed, landed = _install_landed_timer(engine)
    original_offers = engine.offers
    original_quote = engine.quoted_cost
    original_run_match = match.run_match
    original_atomic = trade.commit_trade_atomic
    original_commit = trade.commit_trade
    counters = {"offers": 0, "quote": 0, "run_match": 0, "apply": 0}

    def timed_offers(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return original_offers(*args, **kwargs)
        finally:
            counters["offers"] += time.perf_counter_ns() - started

    def timed_quote(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return original_quote(*args, **kwargs)
        finally:
            counters["quote"] += time.perf_counter_ns() - started

    def timed_run_match(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return original_run_match(*args, **kwargs)
        finally:
            counters["run_match"] += time.perf_counter_ns() - started

    def timed_atomic(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return original_atomic(*args, **kwargs)
        finally:
            counters["apply"] += time.perf_counter_ns() - started

    def timed_commit(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return original_commit(*args, **kwargs)
        finally:
            counters["apply"] += time.perf_counter_ns() - started

    engine.offers = timed_offers
    engine.quoted_cost = timed_quote
    match.run_match = timed_run_match
    trade.commit_trade_atomic = timed_atomic
    trade.commit_trade = timed_commit
    started = time.perf_counter_ns()
    try:
        basic = [buy for buy in buys if buy["purpose"] == "Базовые нужды"]
        for buy in engine.shuffled(basic):
            engine.match(buy, sells, local_only=True)
        match.run_buyers(engine, engine.shuffled(list(buys)), sells, EPS,
                         trade.commit_trade, match.run_match)
    finally:
        engine.landed = original_landed
        engine.offers = original_offers
        engine.quoted_cost = original_quote
        match.run_match = original_run_match
        trade.commit_trade_atomic = original_atomic
        trade.commit_trade = original_commit
    total = time.perf_counter_ns() - started
    search = max(0, counters["offers"] - landed["ns"] + counters["quote"])
    distribution = max(0, counters["run_match"] - counters["apply"])
    finalization = max(0, total - landed["ns"] - search - distribution - counters["apply"])
    return {
        "total": total,
        "routes": landed["ns"],
        "search": search,
        "distribution": distribution,
        "apply": counters["apply"],
        "finalization": finalization,
        "snapshot": 0,
        "landed_calls": landed["calls"],
    }


def run_v4(engine, buys, sells, *, warm: bool) -> dict[str, int]:
    if warm:
        _warm_routes(engine, buys, sells)
    else:
        _cold_routes(engine)
    original_landed, landed = _install_landed_timer(engine)
    original_atomic = v4_adapter.commit_trade_atomic
    apply_ns = 0

    def timed_atomic(*args, **kwargs):
        nonlocal apply_ns
        started = time.perf_counter_ns()
        try:
            return original_atomic(*args, **kwargs)
        finally:
            apply_ns += time.perf_counter_ns() - started

    v4_adapter.commit_trade_atomic = timed_atomic
    started = time.perf_counter_ns()
    try:
        run = v4_adapter.clear_and_apply_v4(engine, buys, sells)
    finally:
        engine.landed = original_landed
        v4_adapter.commit_trade_atomic = original_atomic
    total = time.perf_counter_ns() - started
    routes = run.timings_ns["routes"]
    snapshot = run.timings_ns["snapshot"]
    distribution = run.timings_ns["two_lp"]
    apply_total = run.timings_ns["apply"]
    finalization = max(0, apply_total - apply_ns)
    search = max(0, routes - landed["ns"])
    return {
        "total": total,
        "routes": landed["ns"],
        "search": search,
        "distribution": distribution,
        "apply": apply_ns,
        "finalization": finalization,
        "snapshot": snapshot,
        "landed_calls": landed["calls"],
    }


def main():
    output = ROOT / "docs" / "v3_v4_trade_phase_timings.csv"
    fields = ["seed", "repeat", "temperature", "position", "variant", "stage", "ns"]
    rows: list[dict[str, object]] = []
    for seed in SEEDS:
        for repeat in range(REPEATS + 1):
            order = ("v3", "v4") if (seed + repeat) % 2 == 0 else ("v4", "v3")
            for position, variant in enumerate(order):
                with tempfile.TemporaryDirectory() as directory:
                    db, service = scenario.build_world(seed, directory, report_detail="none")
                    try:
                        engine = service.economy_state().engine
                        buys, sells_by_good, _ = prepare_market(engine)
                        sells = [row for group in sells_by_good.values() for row in group]
                        timings = (run_v3 if variant == "v3" else run_v4)(
                            engine, buys, sells_by_good if variant == "v3" else sells,
                            warm=repeat > 0)
                        for stage, ns in timings.items():
                            if stage == "landed_calls":
                                continue
                            rows.append({"seed": seed, "repeat": repeat,
                                         "temperature": "warm" if repeat else "cold",
                                         "position": position, "variant": variant,
                                         "stage": stage, "ns": ns})
                    finally:
                        db.close()
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    warm = [row for row in rows if row["temperature"] == "warm"]
    print("seed,variant,stage,median_ms,ratio_v3_over_v4")
    for seed in SEEDS:
        for stage in ("routes", "search", "snapshot", "distribution", "apply", "finalization", "total"):
            values = {}
            for variant in ("v3", "v4"):
                values[variant] = float(np.median([row["ns"] for row in warm
                    if row["seed"] == seed and row["variant"] == variant and row["stage"] == stage])) / 1e6
            print(f"{seed},{stage},{values['v3']:.3f},{values['v4']:.3f},{values['v3']/values['v4']:.3f}")
    print(f"raw_csv={output}")


if __name__ == "__main__":
    main()
