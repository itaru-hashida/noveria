"""Разреженный residual repair для V4 без глобального LP."""

from __future__ import annotations

import numpy as np
from collections import deque

from .market import MarketSnapshot, _result_from_x


def _buyer_rank(snapshot: MarketSnapshot) -> np.ndarray:
    order = np.lexsort((snapshot.buyer_ids, -snapshot.buyer_age,
                        -snapshot.buyer_limit))
    rank = np.empty(len(order), dtype=np.int64)
    rank[order] = np.arange(len(order))
    return rank


def _greedy_flow(snapshot: MarketSnapshot, tolerance: float) -> tuple[
    np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    buyer = snapshot.pair_buyer
    seller = snapshot.pair_seller
    price = snapshot.pair_delivered_price
    flow = np.zeros(len(buyer), dtype=np.float64)
    demand = snapshot.buyer_quantity.copy()
    supply = snapshot.seller_quantity.copy()
    budget = snapshot.buyer_budget.copy()
    rank = _buyer_rank(snapshot)
    for current in np.argsort(rank, kind="stable"):
        edges = np.flatnonzero(buyer == current)
        edges = sorted(
            edges.tolist(),
            key=lambda edge: (float(price[edge]),
                              snapshot.seller_ids[int(seller[edge])]),
        )
        for edge in edges:
            if demand[current] <= tolerance:
                break
            seller_index = seller[edge]
            affordable = budget[current] / price[edge] if price[edge] > 0 else demand[current]
            quantity = min(demand[current], supply[seller_index], affordable)
            if quantity <= tolerance:
                continue
            flow[edge] += quantity
            demand[current] -= quantity
            supply[seller_index] -= quantity
            budget[current] -= quantity * price[edge]
    return flow, demand, supply, budget


def _path_delta(
    snapshot: MarketSnapshot,
    flow: np.ndarray,
    demand: np.ndarray,
    supply: np.ndarray,
    budget: np.ndarray,
    start_buyer: int,
    additions: list[int],
    removals: list[int],
    tolerance: float,
) -> float:
    delta = float(demand[start_buyer])
    for edge in removals:
        delta = min(delta, float(flow[edge]))
    end_seller = snapshot.pair_seller[additions[-1]]
    delta = min(delta, float(supply[end_seller]))
    add_cost = np.zeros(len(snapshot.buyer_ids), dtype=np.float64)
    remove_credit = np.zeros(len(snapshot.buyer_ids), dtype=np.float64)
    for edge in additions:
        add_cost[snapshot.pair_buyer[edge]] += snapshot.pair_delivered_price[edge]
    for edge in removals:
        remove_credit[snapshot.pair_buyer[edge]] += snapshot.pair_delivered_price[edge]
    for buyer in np.flatnonzero(add_cost > tolerance):
        net_cost = add_cost[buyer] - remove_credit[buyer]
        if net_cost > tolerance:
            delta = min(delta, float(budget[buyer]) / net_cost)
    return max(0.0, delta)


def _find_augmenting_path(
    snapshot: MarketSnapshot,
    flow: np.ndarray,
    demand: np.ndarray,
    supply: np.ndarray,
    budget: np.ndarray,
    start_buyer: int,
    tolerance: float,
    outgoing,
    occupied,
) -> tuple[list[int], list[int], float] | None:
    buyer = snapshot.pair_buyer
    seller = snapshot.pair_seller
    price = snapshot.pair_delivered_price
    visited_buyers = np.zeros(len(snapshot.buyer_ids), dtype=bool)
    visited_sellers = np.zeros(len(snapshot.seller_ids), dtype=bool)
    parent = [None] * len(snapshot.buyer_ids)
    visited_buyers[start_buyer] = True
    queue = deque([start_buyer])
    while queue:
        current = queue.popleft()
        for edge in outgoing[current]:
            seller_index = int(seller[edge])
            if visited_sellers[seller_index]:
                continue
            visited_sellers[seller_index] = True
            if supply[seller_index] > tolerance:
                additions = [edge]
                removals = []
                node = current
                while node != start_buyer:
                    previous, previous_edge, removed_edge = parent[node]
                    additions.append(previous_edge)
                    removals.append(removed_edge)
                    node = previous
                additions.reverse()
                removals.reverse()
                delta = _path_delta(
                    snapshot, flow, demand, supply, budget, start_buyer,
                    additions, removals, tolerance,
                )
                if delta > tolerance:
                    return additions, removals, delta
                continue
            for old_edge in occupied[seller_index]:
                if flow[old_edge] <= tolerance:
                    continue
                old_buyer = int(buyer[old_edge])
                if visited_buyers[old_buyer]:
                    continue
                visited_buyers[old_buyer] = True
                parent[old_buyer] = (current, edge, old_edge)
                queue.append(old_buyer)
    return None


def allocate_residual(snapshot: MarketSnapshot, *, tolerance=1e-8) -> np.ndarray:
    """Greedy flow plus arbitrary-length alternating augmenting paths.

    The path search preserves all buyer and seller volumes and checks the
    incremental money constraint for every buyer on the path. It maximizes
    the volume reached by this local search, without claiming global LP
    optimality or lexicographic exactness.
    """
    flow, demand, supply, budget = _greedy_flow(snapshot, tolerance)
    buyer = snapshot.pair_buyer
    seller = snapshot.pair_seller
    price = snapshot.pair_delivered_price
    outgoing = [
        sorted(
            np.flatnonzero(buyer == current).tolist(),
            key=lambda edge: (float(price[edge]),
                              snapshot.seller_ids[int(seller[edge])]),
        )
        for current in range(len(snapshot.buyer_ids))
    ]
    occupied = [
        np.flatnonzero(seller == current).tolist()
        for current in range(len(snapshot.seller_ids))
    ]
    for start_buyer in _buyer_rank(snapshot).argsort(kind="stable"):
        if supply.sum() <= tolerance:
            break
        while demand[start_buyer] > tolerance:
            path = _find_augmenting_path(
                snapshot, flow, demand, supply, budget,
                int(start_buyer), tolerance,
                outgoing, occupied,
            )
            if path is None:
                break
            additions, removals, delta = path
            for edge in removals:
                flow[edge] -= delta
                supply[snapshot.pair_seller[edge]] += delta
                budget[snapshot.pair_buyer[edge]] += (
                    delta * snapshot.pair_delivered_price[edge]
                )
            for edge in additions:
                flow[edge] += delta
                supply[snapshot.pair_seller[edge]] -= delta
                budget[snapshot.pair_buyer[edge]] -= (
                    delta * snapshot.pair_delivered_price[edge]
                )
            demand[start_buyer] -= delta
    return flow


def allocate_market_residual(snapshot: MarketSnapshot, *, tolerance=1e-8):
    flow = allocate_residual(snapshot, tolerance=tolerance)
    return _result_from_x(snapshot, flow, 0, tolerance=tolerance)
