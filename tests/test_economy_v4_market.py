import numpy as np

from modules.economy.v4.market import MarketSnapshot, allocate_market


def market(buyers, sellers, routes=()):
    return allocate_market(MarketSnapshot.from_rows(buyers, sellers, routes))


def test_control_example_maximizes_ten_units():
    result = market(
        [{"id": "B1", "good": "x", "quantity": 8, "limit": 16, "budget": 200},
         {"id": "B2", "good": "x", "quantity": 8, "limit": 12, "budget": 200}],
        [{"id": "S1", "good": "x", "quantity": 6, "price": 9},
         {"id": "S2", "good": "x", "quantity": 4, "price": 15}],
    )
    assert result.objective_volume == 10
    assert sum(row.quantity for row in result.allocations) == 10


def test_freight_budget_and_unreachable_route_are_hard_constraints():
    result = market(
        [{"id": "b", "good": "x", "quantity": 10, "limit": 10, "budget": 25}],
        [{"id": "near", "good": "x", "quantity": 10, "price": 8},
         {"id": "far", "good": "x", "quantity": 10, "price": 1}],
        [{"buyer_id": "b", "seller_id": "near", "good": "x", "freight": 1},
         {"buyer_id": "b", "seller_id": "far", "good": "x", "reachable": False}],
    )
    assert result.objective_volume == 25 / 9
    assert {row.seller_id for row in result.allocations} == {"near"}


def test_equal_price_uses_age_then_id_without_rng():
    rows = [
        {"id": "b-old", "good": "x", "quantity": 3, "limit": 5, "budget": 15, "unmet_age": 2},
        {"id": "b-new", "good": "x", "quantity": 3, "limit": 5, "budget": 15, "unmet_age": 0},
    ]
    first = market(rows, [{"id": "s", "good": "x", "quantity": 3, "price": 5}])
    second = market(list(reversed(rows)), [{"id": "s", "good": "x", "quantity": 3, "price": 5}])
    assert first.allocations[0].buyer_id == "b-old"
    assert [(x.buyer_id, x.quantity) for x in first.allocations] == [(x.buyer_id, x.quantity) for x in second.allocations]


def test_equal_price_and_age_use_strict_id_priority():
    result = market(
        [{"id": "b-1", "good": "x", "quantity": 5, "limit": 5, "budget": 25},
         {"id": "b-2", "good": "x", "quantity": 1, "limit": 5, "budget": 5}],
        [{"id": "s", "good": "x", "quantity": 1, "price": 5}],
    )
    assert result.buyer_filled.tolist() == [1.0, 0.0]
    assert result.linprog_calls == 3


def test_permutation_invariance_and_multiple_goods():
    buyers = [
        {"id": "b2", "good": "y", "quantity": 2, "limit": 4, "budget": 8},
        {"id": "b1", "good": "x", "quantity": 2, "limit": 4, "budget": 8},
    ]
    sellers = [
        {"id": "s2", "good": "y", "quantity": 2, "price": 2},
        {"id": "s1", "good": "x", "quantity": 2, "price": 2},
    ]
    a = market(buyers, sellers)
    b = market(reversed(buyers), reversed(sellers))
    assert a.objective_volume == b.objective_volume == 4
    assert [(x.buyer_id, x.seller_id, x.quantity) for x in a.allocations] == [(x.buyer_id, x.seller_id, x.quantity) for x in b.allocations]


def test_unmet_age_updates_and_every_order_has_terminal_outcome():
    result = MarketSnapshot.from_rows(
        [{"id": "b", "good": "x", "quantity": 2, "limit": 1, "budget": 2, "unmet_age": 4}],
        [{"id": "s", "good": "x", "quantity": 2, "price": 2}],
    )
    allocation = allocate_market(result)
    outcome = next(row for row in allocation.outcomes if row.order_id == "b")
    assert outcome.status == "unfilled"
    assert outcome.unmet_age == 5
    assert outcome.remaining == outcome.unmet_quantity == 2
    assert all(row.status in {"fulfilled", "partial", "unfilled", "rejected"} for row in allocation.outcomes)
