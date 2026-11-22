"""Экспериментальный клиринг на массивах рёбер без LP."""

import numpy as np

from .market import MarketSnapshot, _result_from_x


def _join(keys, right_keys):
    """Индексы пар с одинаковым ключом без декартова произведения."""
    order = np.argsort(right_keys, kind="stable")
    sorted_keys = right_keys[order]
    start = np.searchsorted(sorted_keys, keys)
    count = np.searchsorted(sorted_keys, keys, side="right") - start
    left = np.repeat(np.arange(len(keys)), count)
    offset = np.arange(len(left)) - np.repeat(np.cumsum(count) - count, count)
    return left, order[start[left] + offset]


def allocate_residual(snapshot: MarketSnapshot, *, repair_steps=64, tolerance=1e-8):
    """Вернуть объёмы по рёбрам, сохраняя исполнение вытесняемых покупателей."""
    buyer, seller = snapshot.pair_buyer, snapshot.pair_seller
    price = snapshot.pair_delivered_price
    demand = snapshot.buyer_quantity.copy()
    supply = snapshot.seller_quantity.copy()
    budget = snapshot.buyer_budget.copy()
    rank = np.empty(len(demand), dtype=np.intp)
    rank[np.lexsort((snapshot.buyer_ids, -snapshot.buyer_age,
                     -snapshot.buyer_limit))] = np.arange(len(demand))
    flow = np.zeros(len(buyer))
    # Рёбра покупателя идут подряд, внутри — по цене и ID продавца.
    ordered = np.lexsort((seller, price, buyer))

    def affordable(money, cost):
        return np.divide(np.maximum(money, 0), cost,
                         out=np.full_like(cost, np.inf), where=cost > 0)

    def direct():
        while True:
            cap = np.minimum(np.minimum(demand[buyer], supply[seller]),
                             affordable(budget[buyer], price))
            edges = ordered[cap[ordered] > tolerance]
            if not len(edges):
                return
            edges = edges[np.unique(buyer[edges], return_index=True)[1]]
            # Продавец ждёт старшую заявку даже когда она пока берёт другой лот.
            eligible = np.where(cap > tolerance, rank[buyer], len(rank))
            first = np.full(len(supply), len(rank))
            np.minimum.at(first, seller, eligible)
            edges = edges[rank[buyer[edges]] == first[seller[edges]]]
            b, s, q = buyer[edges], seller[edges], cap[edges]
            flow[edges] += q
            demand[b] -= q
            supply[s] -= q
            budget[b] -= q * price[edges]

    direct()
    for _ in range(repair_steps):
        incoming = np.flatnonzero((demand[buyer] > tolerance)
                                  & (affordable(budget[buyer], price) > tolerance))
        occupied = np.flatnonzero(flow > tolerance)
        left, right = _join(seller[incoming], seller[occupied])
        new, old = incoming[left], occupied[right]
        free = np.flatnonzero(supply[seller] > tolerance)
        left, right = _join(buyer[old], buyer[free])
        new, old, alt = new[left], old[left], free[right]
        cost = np.maximum(price[alt] - price[old], 0)
        q = np.minimum.reduce((demand[buyer[new]], flow[old], supply[seller[alt]],
                               affordable(budget[buyer[new]], price[new]),
                               affordable(budget[buyer[old]], cost)))
        valid = np.flatnonzero((q > tolerance) & (buyer[new] != buyer[old])
                               & (seller[new] != seller[alt]))
        if not len(valid):
            break
        i = valid[np.lexsort((alt[valid], old[valid], new[valid], -q[valid],
                              rank[buyer[new[valid]]]))[0]]
        n, o, a, amount = new[i], old[i], alt[i], q[i]
        flow[n] += amount
        flow[o] -= amount
        flow[a] += amount
        demand[buyer[n]] -= amount
        supply[seller[a]] -= amount
        budget[buyer[n]] -= amount * price[n]
        budget[buyer[o]] -= amount * (price[a] - price[o])
        direct()
    return flow


def allocate_market_residual(snapshot: MarketSnapshot, *, repair_steps=64, tolerance=1e-8):
    """Подготовить результат для существующей границы применения."""
    flow = allocate_residual(snapshot, repair_steps=repair_steps, tolerance=tolerance)
    return _result_from_x(snapshot, flow, 0, tolerance=tolerance)
