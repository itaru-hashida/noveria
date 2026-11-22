"""Сравнение одного живого торгового прохода V3 и shadow V4.

Это не месячный ход: выполняются только одинаковые подготовительные фазы до
рынка, затем либо V3 matching, либо V4 snapshot/allocation/apply.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

import scenario_runner as scenario  # noqa: E402
from modules.economy.support import EPS  # noqa: E402
from modules.economy.native import match_boundary as match  # noqa: E402
from modules.economy.native import trade_boundary as trade  # noqa: E402
from modules.economy.native.enterprise_reset_boundary import reset_enterprises  # noqa: E402
from shadow.v4_market_adapter import clear_and_apply_v4  # noqa: E402


def prepare_market(engine: object) -> tuple[list[dict], Mapping[str, list[dict]], dict[str, int]]:
    """Воспроизвести подготовку monthly_step_python до первого match."""
    started = time.perf_counter_ns()
    engine.invalidate_stock_sales_cache()
    engine.activate_rules()
    engine.accounts = dict(sorted(engine.accounts.items()))
    engine.enterprises = dict(sorted(engine.enterprises.items()))
    engine.meta["month"] += 1
    engine.reset_report()
    engine.report.update(rules_version=3, investment_decisions=[])
    engine.sync_locations()
    engine.hooks = getattr(engine, "hooks", None)
    existing = list(engine.enterprises.values())
    reset_enterprises(existing)
    engine.update_prices()
    engine.prepare_recovery_quotes()
    sells_by_good = engine.sell_orders()
    households = engine.shuffled(
        a for a in engine.accounts.values() if a["kind"] == "household"
    )
    engine.household_requests(households, engine.cfg["basic_needs"], "Базовые нужды", sells_by_good)
    engine.household_requests(households, engine.cfg["extra_needs"], "Дополнительные нужды", sells_by_good)
    engine.owner_requests(sells_by_good)
    engine.recover_enterprises(sells_by_good)
    engine.public_project_requests(sells_by_good)
    engine.project_requests(sells_by_good)
    engine.plan_enterprises(existing, sells_by_good)
    buys = [order for order in engine.report["orders"] if order["side"] == "buy"]
    return buys, sells_by_good, {"preparation": time.perf_counter_ns() - started}


def totals(engine: object) -> tuple[object, float]:
    return engine.total_stock(), float(engine.gold())


def state_errors(engine: object) -> list[str]:
    errors: list[str] = []
    for account in engine.accounts.values():
        if account["balance"] < -1e-7:
            errors.append(f"negative balance: {account['id']}={account['balance']}")
        for good, quantity in account.get("stock", {}).items():
            if quantity < -1e-7:
                errors.append(f"negative stock: {account['id']}:{good}={quantity}")
    for order in engine.report["orders"]:
        for field in ("remaining", "reserved"):
            if order.get(field, 0.0) < -1e-7:
                errors.append(f"negative {field}: {order['id']}={order[field]}")
        if order["status"] == "filled" and order["remaining"] > 1e-7:
            errors.append(f"filled with remaining: {order['id']}")
    return errors


def state_summary(engine: object) -> dict[str, object]:
    orders = engine.report["orders"]
    status_counts: dict[str, int] = {}
    reserved_by_side: dict[str, float] = {}
    for order in orders:
        status_counts[order["status"]] = status_counts.get(order["status"], 0) + 1
        reserved_by_side[order["side"]] = reserved_by_side.get(order["side"], 0.0) + float(order.get("reserved", 0.0))
    return {"status_counts": status_counts, "reserved_by_side": reserved_by_side}


def run_v3(engine: object, buys: Iterable[dict], sells: Mapping[str, list[dict]], preparation_ns: int) -> dict:
    before = totals(engine)
    started = time.perf_counter_ns()
    basic = [buy for buy in buys if buy["purpose"] == "Базовые нужды"]
    for buy in engine.shuffled(basic):
        engine.match(buy, sells, local_only=True)
    match.run_buyers(engine, engine.shuffled(list(buys)), sells, EPS,
                     trade.commit_trade, match.run_match)
    elapsed = time.perf_counter_ns() - started
    after = totals(engine)
    return {
        "time_ns": elapsed,
        "preparation_ns": preparation_ns,
        "volume": sum(float(row["quantity"]) for row in engine.report["trades"]),
        "trades": len(engine.report["trades"]),
        "orders": len(engine.report["orders"]),
        "before_stock": before[0], "after_stock": after[0],
        "before_gold": before[1], "after_gold": after[1],
        "errors": state_errors(engine),
        "state": state_summary(engine),
    }


def run_v4(engine: object, buys: list[dict], sells: Iterable[dict], preparation_ns: int) -> dict:
    before = totals(engine)
    run = clear_and_apply_v4(engine, buys, sells)
    after = totals(engine)
    return {
        "timings_ns": run.timings_ns,
        "preparation_ns": preparation_ns,
        "volume": run.result.objective_volume,
        "trades": len(run.applied),
        "orders": len(engine.report["orders"]),
        "buyers": len(run.snapshot.buyer_ids),
        "sellers": len(run.snapshot.seller_ids),
        "pairs": len(run.snapshot.pair_buyer),
        "lp_calls": run.result.linprog_calls,
        "before_stock": before[0], "after_stock": after[0],
        "before_gold": before[1], "after_gold": after[1],
        "errors": state_errors(engine),
        "state": state_summary(engine),
    }


def main() -> None:
    seed = 73129
    with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
        db_v3, service_v3 = scenario.build_world(seed, left, report_detail="none")
        db_v4, service_v4 = scenario.build_world(seed, right, report_detail="none")
        try:
            v3_engine = service_v3.economy_state().engine
            v4_engine = service_v4.economy_state().engine
            buys_v3, sells_v3, prep_v3 = prepare_market(v3_engine)
            buys_v4, sells_v4, prep_v4 = prepare_market(v4_engine)
            flat_sells_v3 = [sell for rows in sells_v3.values() for sell in rows]
            flat_sells_v4 = [sell for rows in sells_v4.values() for sell in rows]
            same_input = (len(buys_v3), len(flat_sells_v3)) == (len(buys_v4), len(flat_sells_v4))
            result = {
                "seed": seed,
                "same_input_counts": same_input,
                "input": {"buys": len(buys_v3), "sells": len(flat_sells_v3)},
                "v3": run_v3(v3_engine, buys_v3, sells_v3, prep_v3["preparation"]),
                "v4": run_v4(v4_engine, buys_v4, flat_sells_v4, prep_v4["preparation"]),
                "comparison_note": "V3 includes its basic-needs local_only pass; V4 clears all reachable pairs globally.",
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        finally:
            db_v3.close()
            db_v4.close()


if __name__ == "__main__":
    main()
