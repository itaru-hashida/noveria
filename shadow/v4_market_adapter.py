"""Минимальный мост живых заявок V3 к чистому market solver V4.

Модуль намеренно остаётся в ``shadow``: он не подменяет месячный ход и не
меняет экономическую логику V3. Его задача — дать один проверяемый путь
``живые заявки -> маршруты -> V4 allocation -> существующее применение``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import time
from typing import Any

from modules.economy.native.trade_boundary import commit_trade_atomic
from modules.economy.v4.market import MarketResult, MarketSnapshot


EPS = 1e-9


@dataclass(frozen=True)
class V4MarketRun:
    """Результат одного shadow-клиринга и его применения."""

    snapshot: MarketSnapshot
    result: MarketResult
    applied: tuple[dict[str, Any], ...]
    timings_ns: dict[str, int]


def _active(orders: Iterable[Mapping[str, Any]], side: str) -> list[Mapping[str, Any]]:
    return [
        order for order in orders
        if order.get("side") == side
        and order.get("status") not in {"cancelled", "expired", "filled"}
        and float(order.get("remaining", order.get("quantity", 0.0))) > EPS
    ]


def build_v4_snapshot(
    engine: Any,
    buys: Iterable[Mapping[str, Any]],
    sells: Iterable[Mapping[str, Any]],
    *,
    unmet_age: Mapping[str, float] | None = None,
    timings_ns: dict[str, int] | None = None,
) -> tuple[MarketSnapshot, dict[tuple[str, str], tuple[float, float, Any, Any]]]:
    """Преобразовать живые V3 orders в V4 snapshot.

    В V3 резерв заявки — фактический денежный лимит текущего match. Поэтому
    именно ``reserved`` используется как budget; добавление свободной кассы
    изменило бы семантику V3. Возраст, которого нет в V3 order schema, по
    умолчанию равен нулю и может быть передан отдельно по id заявки.
    """
    buyers = _active(buys, "buy")
    sellers = _active(sells, "sell")
    ages = unmet_age or {}
    buyer_rows = [
        dict(
            id=str(order["id"]),
            good_id=str(order["good"]),
            quantity=float(order.get("remaining", order["quantity"])),
            limit=float(order["price"]),
            unmet_age=float(ages.get(str(order["id"]), 0.0)),
            budget=float(order.get("reserved", 0.0)),
        )
        for order in buyers
    ]
    seller_rows = [
        dict(
            id=str(order["id"]),
            good_id=str(order["good"]),
            quantity=float(order.get("remaining", order["quantity"])),
            price=float(order["price"]),
        )
        for order in sellers
    ]

    routes: list[dict[str, Any]] = []
    route_by_pair: dict[tuple[str, str], tuple[float, float, Any, Any]] = {}
    route_started = time.perf_counter_ns()
    for buy in buyers:
        for sell in sellers:
            if buy["good"] != sell["good"]:
                continue
            if buy["participant"] == sell["participant"]:
                # MarketSnapshot treats an omitted route as reachable with
                # zero freight. Self-trade must therefore be explicit.
                routes.append({
                    "buyer_id": str(buy["id"]),
                    "seller_id": str(sell["id"]),
                    "good_id": str(buy["good"]),
                    "reachable": False,
                })
                continue
            quote = engine.landed(
                sell["tile_id"], buy["tile_id"], buy["good"], float(sell["price"])
            )
            if quote is None:
                routes.append({
                    "buyer_id": str(buy["id"]),
                    "seller_id": str(sell["id"]),
                    "good_id": str(buy["good"]),
                    "reachable": False,
                })
                continue
            delivered, freight, distance, path = quote
            routes.append({
                "buyer_id": str(buy["id"]),
                "seller_id": str(sell["id"]),
                "good_id": str(buy["good"]),
                "freight": float(freight),
            })
            route_by_pair[(str(buy["id"]), str(sell["id"]))] = (
                float(delivered), float(freight), distance, path
            )
    route_ns = time.perf_counter_ns() - route_started
    snapshot_started = time.perf_counter_ns()
    snapshot = MarketSnapshot.from_rows(buyer_rows, seller_rows, routes)
    snapshot_ns = time.perf_counter_ns() - snapshot_started
    if timings_ns is not None:
        timings_ns.update(routes=route_ns, snapshot=snapshot_ns)
    return snapshot, route_by_pair


def apply_v4_result(engine: Any, result: MarketResult, route_by_pair: Mapping[tuple[str, str], tuple[float, float, Any, Any]]) -> tuple[dict[str, Any], ...]:
    """Применить allocation через тот же atomic trade boundary, что использует V3."""
    orders = {str(order["id"]): order for order in engine.report["orders"]}
    applied: list[dict[str, Any]] = []
    for allocation in result.allocations:
        buy = orders[allocation.buyer_id]
        sell = orders[allocation.seller_id]
        quote = route_by_pair[(allocation.buyer_id, allocation.seller_id)]
        applied.append(commit_trade_atomic(
            engine,
            buy,
            sell,
            (quote[0], quote[1], quote[2], quote[3]),
            allocation.quantity,
            allocation.seller_price,
            EPS,
            engine.cash_store,
            engine.order_store,
        ))
    # V3's matcher finalises the buyer status in its outer loop. The shadow
    # bridge has no such loop, so mirror that small bookkeeping step for every
    # order represented in the solver outcomes.
    for outcome in result.outcomes:
        order = orders[outcome.order_id]
        status = "filled" if outcome.status == "fulfilled" else outcome.status
        engine.order_store.set_status(order, status)
    return tuple(applied)


def clear_and_apply_v4(
    engine: Any,
    buys: Iterable[Mapping[str, Any]],
    sells: Iterable[Mapping[str, Any]],
    *,
    solver: Callable[[MarketSnapshot], MarketResult] | None = None,
    unmet_age: Mapping[str, float] | None = None,
) -> V4MarketRun:
    """Выполнить shadow V4 market phase на живых V3 orders."""
    if solver is None:
        # This is intentionally the already-audited research candidate. It
        # is faster than exact lexicographic solving, but not a proof of it.
        from scripts.experiment_v4_priority_lp import allocate_market_two_lp

        solver = allocate_market_two_lp
    preparation_timings: dict[str, int] = {}
    snapshot, route_by_pair = build_v4_snapshot(
        engine, buys, sells, unmet_age=unmet_age, timings_ns=preparation_timings
    )
    solver_started = time.perf_counter_ns()
    result = solver(snapshot)
    solver_ns = time.perf_counter_ns() - solver_started
    apply_started = time.perf_counter_ns()
    applied = apply_v4_result(engine, result, route_by_pair)
    apply_ns = time.perf_counter_ns() - apply_started
    timings_ns = dict(preparation_timings, two_lp=solver_ns, apply=apply_ns)
    return V4MarketRun(snapshot, result, applied, timings_ns)
