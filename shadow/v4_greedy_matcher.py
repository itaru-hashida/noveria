"""Экспериментальный V4 matcher без LP.

Live-вариант повторно использует V3 ``offers``: индексы route components,
кэш landed и сортировку предложений. Модуль не подключается к V3 monthly path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import time
from typing import Any

import numpy as np

from modules.economy.native.trade_boundary import commit_trade_atomic
from modules.economy.v4.market import MarketResult, MarketSnapshot, _result_from_x


EPS = 1e-9


def _buyer_priority(row: Mapping[str, Any], age: Mapping[str, float] | None = None) -> tuple[float, float, str]:
    order_id = str(row.get("id", ""))
    unmet_age = float((age or {}).get(order_id, row.get("unmet_age", 0.0)))
    return (-float(row.get("limit", row.get("price", 0.0))), -unmet_age, order_id)


def allocate_market_greedy(
    snapshot: MarketSnapshot,
    *,
    tolerance: float = 1e-8,
) -> MarketResult:
    """Greedy allocation by buyer priority, then cheapest delivered offer.

    This is deliberately not claimed to maximize global volume. It uses only
    snapshot edge arrays and seller/buyer capacities; no dense matrix or LP.
    """
    n = len(snapshot.pair_buyer)
    x = np.zeros(n, dtype=np.float64)
    buyer_order = np.lexsort((np.asarray(snapshot.buyer_ids), -snapshot.buyer_age,
                              -snapshot.buyer_limit))
    for buyer in buyer_order:
        edges = np.flatnonzero(snapshot.pair_buyer == buyer)
        edges = np.asarray(sorted(
            edges.tolist(),
            key=lambda edge: (
                float(snapshot.pair_delivered_price[edge]),
                snapshot.seller_ids[int(snapshot.pair_seller[edge])],
            ),
        ), dtype=np.int64) if len(edges) else edges
        remaining = float(snapshot.buyer_quantity[buyer])
        budget = float(snapshot.buyer_budget[buyer])
        for edge in edges:
            if remaining <= tolerance:
                break
            price = float(snapshot.pair_delivered_price[edge])
            seller = int(snapshot.pair_seller[edge])
            seller_remaining = float(snapshot.seller_quantity[seller] - np.sum(
                x[snapshot.pair_seller == seller]
            ))
            quantity = min(remaining, seller_remaining,
                           budget / price if price > tolerance else remaining)
            if quantity <= tolerance:
                continue
            x[edge] = quantity
            remaining -= quantity
            budget -= quantity * price
    return _result_from_x(snapshot, x, 0, tolerance=tolerance)


def match_live_market(
    engine: Any,
    buys: Iterable[Mapping[str, Any]],
    sells: Mapping[str, list[Mapping[str, Any]]],
    *,
    unmet_age: Mapping[str, float] | None = None,
    local_only: bool = False,
) -> dict[str, Any]:
    """Match live V3 orders through indexed ``engine.offers`` and apply trades."""
    age = unmet_age or {}
    active = [
        buy for buy in buys
        if buy.get("side") == "buy"
        and buy.get("status") not in {"cancelled", "expired", "filled"}
        and float(buy.get("remaining", 0.0)) > EPS
    ]
    active.sort(key=lambda row: _buyer_priority(row, age))
    applied: list[dict[str, Any]] = []
    timings = {"offers": 0, "distribution": 0, "apply": 0, "finalization": 0}
    for buy in active:
        buyer = engine.accounts[buy["participant"]]
        offers_started = time.perf_counter_ns()
        options = engine.offers(buyer, buy["good"], sells, local_only=local_only)
        timings["offers"] += time.perf_counter_ns() - offers_started
        distribution_started = time.perf_counter_ns()
        local_apply = 0
        for delivered_price, sell, quote in options:
            if buy["participant"] == sell["participant"]:
                continue
            if delivered_price > buy["price"] + EPS:
                continue
            quantity = min(
                float(buy["remaining"]),
                float(sell["remaining"]),
                float(buy["reserved"]) / float(delivered_price),
                float(engine.purchase_quantity(buy, sells)),
            )
            if quantity <= EPS:
                continue
            apply_started = time.perf_counter_ns()
            applied.append(commit_trade_atomic(
                engine, buy, sell, quote, quantity, float(sell["price"]), EPS,
                engine.cash_store, engine.order_store,
            ))
            elapsed_apply = time.perf_counter_ns() - apply_started
            timings["apply"] += elapsed_apply
            local_apply += elapsed_apply
            if buy["remaining"] <= EPS:
                break
        timings["distribution"] += max(
            0, time.perf_counter_ns() - distribution_started - local_apply
        )
        final_started = time.perf_counter_ns()
        status = "filled" if buy["remaining"] <= EPS else (
            "partial" if buy["remaining"] < buy["quantity"] else "unfilled"
        )
        engine.order_store.set_status(buy, status)
        timings["finalization"] += time.perf_counter_ns() - final_started
    return {"applied": tuple(applied), "timings_ns": timings}
