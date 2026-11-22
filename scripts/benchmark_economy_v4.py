"""Сравнение времени полного хода V3 и изолированного ядра V4 на реальном мире."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from modules.economy.v4 import (  # noqa: E402
    MarketSnapshot,
    allocate_market,
    build_enterprise_matrices,
    calculate_enterprise_orders,
    load_recipe_graph,
)


def benchmark_seed(seed: int) -> dict[str, object]:
    """Измерить один seed на том же initial_world.json, что и аудит V3."""

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

            started = time.perf_counter()
            report = service.advance_turn(
                command_id=f"benchmark-v4-{seed}", source="benchmark"
            )
            v3_seconds = time.perf_counter() - started

            started = time.perf_counter()
            matrices = build_enterprise_matrices(rows, graph)
            production = calculate_enterprise_orders(matrices, graph, detailed=False)
            v4_production_seconds = time.perf_counter() - started

            buyers: list[dict[str, object]] = []
            sellers: list[dict[str, object]] = []
            for enterprise_index, enterprise_id in enumerate(matrices.enterprise_ids):
                for good_index, good in enumerate(matrices.good_ids):
                    required = float(production.input_orders[enterprise_index, good_index])
                    if required > 1e-9:
                        buyers.append(
                            {
                                "id": f"b:{enterprise_id}:{good}",
                                "good": good,
                                "quantity": required,
                                "limit": 1e9,
                                "budget": 1e12,
                                "unmet_age": 0,
                            }
                        )
                    available = float(
                        production.available_output[enterprise_index, good_index]
                    )
                    if available > 1e-9:
                        sellers.append(
                            {
                                "id": f"s:{enterprise_id}:{good}",
                                "good": good,
                                "quantity": available,
                                "price": 1,
                            }
                        )

            started = time.perf_counter()
            snapshot = MarketSnapshot.from_rows(buyers, sellers)
            allocation = allocate_market(snapshot)
            v4_market_seconds = time.perf_counter() - started

            return {
                "seed": seed,
                "tiles": len(engine.tiles),
                "enterprises": len(rows),
                "goods": len(graph.good_ids),
                "buyers": len(buyers),
                "sellers": len(sellers),
                "pairs": len(snapshot.pair_buyer),
                "v3_turn_seconds": v3_seconds,
                "v4_production_seconds": v4_production_seconds,
                "v4_market_seconds": v4_market_seconds,
                "v4_total_seconds": v4_production_seconds + v4_market_seconds,
                "v4_allocated": allocation.objective_volume,
                "v3_month": report["month"],
            }
        finally:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default="17,73129,7,4231")
    args = parser.parse_args()
    results = [benchmark_seed(int(seed)) for seed in args.seeds.split(",") if seed]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
