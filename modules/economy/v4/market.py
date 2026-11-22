"""Детерминированное разреженное распределение рынка V4.

Расчёт отделён от состояния V3: ``MarketSnapshot`` содержит только числовые
массивы, а ``allocate_market`` возвращает результат без побочных эффектов.
Сначала максимизируется общий физический объём, затем среди таких решений
применяется приоритет лимита цены, возраста дефицита и ID покупателя.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
from scipy.optimize import linprog


@dataclass(frozen=True)
class MarketSnapshot:
    buyer_ids: tuple[str, ...]
    seller_ids: tuple[str, ...]
    good_ids: tuple[str, ...]
    buyer_good: np.ndarray
    buyer_quantity: np.ndarray
    buyer_limit: np.ndarray
    buyer_age: np.ndarray
    buyer_budget: np.ndarray
    seller_good: np.ndarray
    seller_quantity: np.ndarray
    seller_price: np.ndarray
    pair_buyer: np.ndarray
    pair_seller: np.ndarray
    pair_freight: np.ndarray
    pair_delivered_price: np.ndarray

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, np.ndarray):
                value.setflags(write=False)

    @classmethod
    def from_rows(
        cls,
        buyers: Iterable[Mapping[str, Any]],
        sellers: Iterable[Mapping[str, Any]],
        routes: Iterable[Mapping[str, Any]] = (),
    ) -> "MarketSnapshot":
        b = sorted((_normalise_order(row, "buyer") for row in buyers), key=lambda r: r["id"])
        s = sorted((_normalise_order(row, "seller") for row in sellers), key=lambda r: r["id"])
        if len({row["id"] for row in b}) != len(b):
            raise ValueError("Повторный buyer id")
        if len({row["id"] for row in s}) != len(s):
            raise ValueError("Повторный seller id")
        buyer_ids = tuple(row["id"] for row in b)
        seller_ids = tuple(row["id"] for row in s)
        goods = tuple(sorted({row["good"] for row in b + s}))
        good_index = {good: i for i, good in enumerate(goods)}
        buyer_good = np.array([good_index[row["good"]] for row in b], dtype=np.int64)
        buyer_quantity = np.array([row["quantity"] for row in b], dtype=np.float64)
        buyer_limit = np.array([row["limit"] for row in b], dtype=np.float64)
        buyer_age = np.array([row["unmet_age"] for row in b], dtype=np.float64)
        buyer_budget = np.array([row["budget"] for row in b], dtype=np.float64)
        seller_good = np.array([good_index[row["good"]] for row in s], dtype=np.int64)
        seller_quantity = np.array([row["quantity"] for row in s], dtype=np.float64)
        seller_price = np.array([row["price"] for row in s], dtype=np.float64)

        route_map: dict[tuple[str, str, str], float | None] = {}
        for raw in routes:
            buyer = str(raw.get("buyer_id", raw.get("buyer")))
            seller = str(raw.get("seller_id", raw.get("seller")))
            good = str(raw.get("good_id", raw.get("good")))
            reachable = raw.get("reachable", True)
            route_map[(buyer, seller, good)] = (
                float(raw.get("freight", raw.get("transport_cost", 0.0)))
                if reachable else None
            )
        pairs: list[tuple[int, int, float, float]] = []
        for bi, brow in enumerate(b):
            for si, srow in enumerate(s):
                if brow["good"] != srow["good"]:
                    continue
                freight = route_map.get((brow["id"], srow["id"], brow["good"]), 0.0)
                if freight is None:
                    continue
                delivered = srow["price"] + freight
                if delivered <= brow["limit"] + 1e-12:
                    pairs.append((bi, si, freight, delivered))
        pairs.sort(key=lambda row: (row[0], row[1]))
        pair = np.asarray(pairs, dtype=np.float64) if pairs else np.empty((0, 4))
        return cls(
            buyer_ids, seller_ids, goods, buyer_good, buyer_quantity, buyer_limit,
            buyer_age, buyer_budget, seller_good, seller_quantity, seller_price,
            pair[:, 0].astype(np.int64), pair[:, 1].astype(np.int64), pair[:, 2], pair[:, 3]
        )


@dataclass(frozen=True)
class Allocation:
    buyer_id: str
    seller_id: str
    good_id: str
    quantity: float
    seller_price: float
    freight: float
    delivered_price: float


@dataclass(frozen=True)
class OrderOutcome:
    order_id: str
    side: str
    status: str
    initial: float
    executed: float
    remaining: float
    reason: str
    unmet_age: float
    unmet_quantity: float


@dataclass(frozen=True)
class MarketResult:
    allocations: tuple[Allocation, ...]
    buyer_filled: np.ndarray
    seller_filled: np.ndarray
    outcomes: tuple[OrderOutcome, ...]
    objective_volume: float
    linprog_calls: int = 0

    def __post_init__(self) -> None:
        self.buyer_filled.setflags(write=False)
        self.seller_filled.setflags(write=False)


def _number(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = float(row.get(key, default))
    if not np.isfinite(value) or value < 0:
        raise ValueError(f"{key} должен быть конечным и неотрицательным")
    return value


def _normalise_order(row: Mapping[str, Any], side: str) -> dict[str, Any]:
    identity = str(row.get("id", row.get(f"{side}_id", "")))
    good = str(row.get("good_id", row.get("good", "")))
    if not identity or not good:
        raise ValueError(f"{side}: требуются id и good")
    result = {"id": identity, "good": good, "quantity": _number(row, "quantity")}
    if side == "buyer":
        result.update(limit=_number(row, "limit", row.get("price", 0.0)),
                      unmet_age=_number(row, "unmet_age"), budget=_number(row, "budget"))
    else:
        result["price"] = _number(row, "price")
    return result


def _solve_lexicographic(
    buyer_quantity: np.ndarray,
    buyer_limit: np.ndarray,
    buyer_age: np.ndarray,
    buyer_budget: np.ndarray,
    buyer_ids: tuple[str, ...],
    seller_quantity: np.ndarray,
    pair_buyer: np.ndarray,
    pair_seller: np.ndarray,
    pair_delivered_price: np.ndarray,
    *,
    tolerance: float,
) -> tuple[np.ndarray, int]:
    """Решить одну независимую компоненту точной лексикографией."""

    n = len(pair_buyer)
    nb, ns = len(buyer_quantity), len(seller_quantity)
    a_rows: list[np.ndarray] = []
    b_rows: list[float] = []
    for bi in range(nb):
        row = np.zeros(n)
        row[pair_buyer == bi] = 1.0
        a_rows.append(row); b_rows.append(buyer_quantity[bi])
    for si in range(ns):
        row = np.zeros(n)
        row[pair_seller == si] = 1.0
        a_rows.append(row); b_rows.append(seller_quantity[si])
    for bi in range(nb):
        row = np.zeros(n)
        row[pair_buyer == bi] = pair_delivered_price[pair_buyer == bi]
        a_rows.append(row); b_rows.append(buyer_budget[bi])
    bounds = [(0.0, None)] * n
    a_ub = np.asarray(a_rows)
    b_ub = np.asarray(b_rows)
    first = linprog(-np.ones(n), A_ub=a_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if not first.success:
        raise ValueError(f"allocation solver failed: {first.message}")
    max_volume = float(-first.fun)
    linprog_calls = 1

    # После фиксации общего объёма последовательно фиксируем максимум каждого
    # покупателя. Это настоящая лексикографическая оптимизация, а не сумма
    # дробных весов, которая может нарушить порядок приоритетов.
    ranks = np.lexsort((np.array(buyer_ids), -buyer_age, -buyer_limit))
    equality_rows = [np.ones(n)]
    equality_values = [max_volume]
    x = first.x
    for bi in ranks:
        buyer_edges = pair_buyer == bi
        if not np.any(buyer_edges):
            continue
        objective = np.zeros(n)
        objective[buyer_edges] = -1.0
        result = linprog(
            objective,
            A_ub=a_ub,
            b_ub=b_ub,
            A_eq=np.asarray(equality_rows),
            b_eq=np.asarray(equality_values),
            bounds=bounds,
            method="highs",
        )
        linprog_calls += 1
        if not result.success:
            raise ValueError(f"lexicographic allocation solver failed: {result.message}")
        x = result.x
        equality_rows.append(buyer_edges.astype(np.float64))
        equality_values.append(float(np.dot(buyer_edges, x)))
    x[np.abs(x) <= tolerance] = 0.0
    return x, linprog_calls


def _result_from_x(
    snapshot: MarketSnapshot,
    x: np.ndarray,
    linprog_calls: int,
    *,
    tolerance: float,
) -> MarketResult:
    """Собрать единый наблюдаемый результат из распределения по рёбрам."""
    x = x.copy()
    x[np.abs(x) <= tolerance] = 0.0
    nb, ns = len(snapshot.buyer_ids), len(snapshot.seller_ids)
    buyer_filled = np.bincount(snapshot.pair_buyer, weights=x, minlength=nb)
    seller_filled = np.bincount(snapshot.pair_seller, weights=x, minlength=ns)
    allocations = tuple(
        Allocation(snapshot.buyer_ids[bi], snapshot.seller_ids[si], snapshot.good_ids[int(snapshot.buyer_good[bi])],
                   float(q), float(snapshot.seller_price[si]), float(freight), float(delivered))
        for bi, si, freight, delivered, q in zip(snapshot.pair_buyer, snapshot.pair_seller,
                                                  snapshot.pair_freight, snapshot.pair_delivered_price, x)
        if q > tolerance
    )
    outcomes: list[OrderOutcome] = []
    for bi, identity in enumerate(snapshot.buyer_ids):
        initial, executed = float(snapshot.buyer_quantity[bi]), float(buyer_filled[bi])
        remaining = max(0.0, initial - executed)
        status = "fulfilled" if remaining <= tolerance else "partial" if executed > tolerance else "unfilled"
        if status == "fulfilled":
            reason = "filled"
        elif executed <= tolerance:
            compatible = np.flatnonzero(snapshot.pair_buyer == bi)
            reason = "insufficient_budget" if compatible.size and snapshot.buyer_budget[bi] <= tolerance else "no_compatible_supply"
        else:
            reason = "limited_supply_or_budget"
        age = 0.0 if status == "fulfilled" or initial <= tolerance else float(snapshot.buyer_age[bi] + 1.0)
        outcomes.append(OrderOutcome(identity, "buy", status, initial, executed, remaining, reason, age, remaining))
    for si, identity in enumerate(snapshot.seller_ids):
        initial, executed = float(snapshot.seller_quantity[si]), float(seller_filled[si])
        remaining = max(0.0, initial - executed)
        status = "fulfilled" if remaining <= tolerance else "partial" if executed > tolerance else "unfilled"
        outcomes.append(OrderOutcome(identity, "sell", status, initial, executed, remaining,
                                     "sold" if executed > tolerance else "no_compatible_demand", 0.0, 0.0))
    return MarketResult(
        allocations, buyer_filled, seller_filled, tuple(outcomes), float(np.sum(x)), linprog_calls
    )


def allocate_market_exact(snapshot: MarketSnapshot, *, tolerance: float = 1e-8) -> MarketResult:
    """Эталонный точный решатель с последовательной лексикографией."""

    if not len(snapshot.pair_buyer):
        return _empty_result(snapshot)
    x, calls = _solve_lexicographic(
        snapshot.buyer_quantity,
        snapshot.buyer_limit,
        snapshot.buyer_age,
        snapshot.buyer_budget,
        snapshot.buyer_ids,
        snapshot.seller_quantity,
        snapshot.pair_buyer,
        snapshot.pair_seller,
        snapshot.pair_delivered_price,
        tolerance=tolerance,
    )
    return _result_from_x(snapshot, x, calls, tolerance=tolerance)


def _components(snapshot: MarketSnapshot) -> list[np.ndarray]:
    """Найти компоненты графа buyer-seller по допустимым рёбрам."""

    nb, ns = len(snapshot.buyer_ids), len(snapshot.seller_ids)
    parent = np.arange(nb + ns)

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = int(parent[node])
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for buyer, seller in zip(snapshot.pair_buyer, snapshot.pair_seller):
        union(int(buyer), nb + int(seller))
    groups: dict[int, list[int]] = {}
    for edge, (buyer, seller) in enumerate(zip(snapshot.pair_buyer, snapshot.pair_seller)):
        groups.setdefault(find(int(buyer)), []).append(edge)
    return [np.asarray(edges, dtype=np.int64) for edges in groups.values()]


def _fast_noncompetitive(snapshot: MarketSnapshot, edge_indices: np.ndarray) -> np.ndarray:
    """Быстро распределить компоненту, где продавцы не конкурируют."""

    x = np.zeros(len(edge_indices), dtype=np.float64)
    local_sellers = snapshot.pair_seller[edge_indices]
    seller_degrees = {int(seller): int(np.count_nonzero(local_sellers == seller))
                      for seller in np.unique(local_sellers)}
    if any(degree > 1 for degree in seller_degrees.values()):
        raise ValueError("fast path called for a competitive component")
    for buyer in np.unique(snapshot.pair_buyer[edge_indices]):
        positions = np.flatnonzero(snapshot.pair_buyer[edge_indices] == buyer)
        positions = positions[np.argsort(snapshot.pair_delivered_price[edge_indices[positions]], kind="stable")]
        remaining = float(snapshot.buyer_quantity[buyer])
        budget = float(snapshot.buyer_budget[buyer])
        for position in positions:
            edge = edge_indices[position]
            quantity = min(
                remaining,
                float(snapshot.seller_quantity[snapshot.pair_seller[edge]]),
                budget / float(snapshot.pair_delivered_price[edge])
                if snapshot.pair_delivered_price[edge] > 0 else remaining,
            )
            if quantity <= 0:
                continue
            x[position] = quantity
            remaining -= quantity
            budget -= quantity * float(snapshot.pair_delivered_price[edge])
    return x


def allocate_market(snapshot: MarketSnapshot, *, tolerance: float = 1e-8) -> MarketResult:
    """Решить рынки по компонентам с быстрым путём для независимых покупателей."""

    if not len(snapshot.pair_buyer):
        return _empty_result(snapshot)
    x = np.zeros(len(snapshot.pair_buyer), dtype=np.float64)
    calls = 0
    for edge_indices in _components(snapshot):
        local_sellers = snapshot.pair_seller[edge_indices]
        if all(np.count_nonzero(local_sellers == seller) <= 1 for seller in np.unique(local_sellers)):
            x[edge_indices] = _fast_noncompetitive(snapshot, edge_indices)
            continue
        buyers = np.unique(snapshot.pair_buyer[edge_indices])
        sellers = np.unique(snapshot.pair_seller[edge_indices])
        buyer_map = {int(value): index for index, value in enumerate(buyers)}
        seller_map = {int(value): index for index, value in enumerate(sellers)}
        local_pair_buyer = np.asarray([buyer_map[int(value)] for value in snapshot.pair_buyer[edge_indices]])
        local_pair_seller = np.asarray([seller_map[int(value)] for value in snapshot.pair_seller[edge_indices]])
        local_x, local_calls = _solve_lexicographic(
            snapshot.buyer_quantity[buyers],
            snapshot.buyer_limit[buyers],
            snapshot.buyer_age[buyers],
            snapshot.buyer_budget[buyers],
            tuple(snapshot.buyer_ids[index] for index in buyers),
            snapshot.seller_quantity[sellers],
            local_pair_buyer,
            local_pair_seller,
            snapshot.pair_delivered_price[edge_indices],
            tolerance=tolerance,
        )
        x[edge_indices] = local_x
        calls += local_calls
    return _result_from_x(snapshot, x, calls, tolerance=tolerance)


def _empty_result(snapshot: MarketSnapshot) -> MarketResult:
    outcomes = [OrderOutcome(identity, "buy", "unfilled", float(snapshot.buyer_quantity[i]), 0.0,
                             float(snapshot.buyer_quantity[i]), "no_compatible_supply",
                             float(snapshot.buyer_age[i] + 1 if snapshot.buyer_quantity[i] else 0),
                             float(snapshot.buyer_quantity[i]))
                for i, identity in enumerate(snapshot.buyer_ids)]
    outcomes.extend(OrderOutcome(identity, "sell", "unfilled", float(snapshot.seller_quantity[i]), 0.0,
                                 float(snapshot.seller_quantity[i]), "no_compatible_demand", 0.0, 0.0)
                    for i, identity in enumerate(snapshot.seller_ids))
    return MarketResult((), np.zeros(len(snapshot.buyer_ids)), np.zeros(len(snapshot.seller_ids)),
                        tuple(outcomes), 0.0, 0)


__all__ = [
    "Allocation", "MarketResult", "MarketSnapshot", "OrderOutcome",
    "allocate_market", "allocate_market_exact",
]
