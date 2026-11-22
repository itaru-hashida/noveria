"""Проверка greedy V4 matcher против exact LP и live-прогон seed 73129."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from modules.economy.v4.market import MarketSnapshot, allocate_market_exact  # noqa: E402
from scripts.experiment_v4_live_market_pass import prepare_market  # noqa: E402
from shadow.v4_greedy_matcher import allocate_market_greedy, match_live_market  # noqa: E402


def small_counterexample() -> dict[str, object]:
    snapshot = MarketSnapshot.from_rows(
        [
            {"id": "high", "good": "x", "quantity": 1, "limit": 10,
             "budget": 10, "unmet_age": 1},
            {"id": "low", "good": "x", "quantity": 10, "limit": 5,
             "budget": 50, "unmet_age": 0},
        ],
        [
            {"id": "cheap", "good": "x", "quantity": 10, "price": 5},
            {"id": "dear", "good": "x", "quantity": 1, "price": 10},
        ],
    )
    exact = allocate_market_exact(snapshot)
    greedy = allocate_market_greedy(snapshot)
    return {
        "exact_volume": exact.objective_volume,
        "greedy_volume": greedy.objective_volume,
        "exact_buy_execution": {o.order_id: o.executed for o in exact.outcomes if o.side == "buy"},
        "greedy_buy_execution": {o.order_id: o.executed for o in greedy.outcomes if o.side == "buy"},
        "greedy_allocations": [a.__dict__ for a in greedy.allocations],
    }


def live(seed: int = 73129) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db, service = scenario.build_world(seed, directory, report_detail="none")
        try:
            engine = service.economy_state().engine
            buys, sells_by_good, _ = prepare_market(engine)
            started = time.perf_counter_ns()
            result = match_live_market(engine, buys, sells_by_good)
            total_ns = time.perf_counter_ns() - started
            timings = dict(result["timings_ns"])
            return {
                "buyers": len(buys),
                "sellers": sum(len(rows) for rows in sells_by_good.values()),
                "trades": len(result["applied"]),
                "volume": sum(float(row["quantity"]) for row in result["applied"]),
                "timings_ms": {key: value / 1e6 for key, value in timings.items()},
                "total_ms": total_ns / 1e6,
                "errors": [
                    f"negative balance {a['id']}" for a in engine.accounts.values()
                    if a["balance"] < -1e-7
                ],
            }
        finally:
            db.close()


def main() -> None:
    print(json.dumps({"counterexample": small_counterexample(), "live": live()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
