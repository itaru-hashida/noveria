"""Focused-тесты изолированного recipe/production-среза V4."""

from pathlib import Path

import numpy as np
import pytest

from modules.economy.v4 import (
    RecipeCycleError,
    build_enterprise_matrices,
    build_recipe_graph,
    calculate_production_orders,
    load_recipe_graph,
)


ROOT = Path(__file__).resolve().parents[1]


def graph_from(mapping):
    return build_recipe_graph({good: {"inputs": inputs} for good, inputs in mapping.items()})


def test_real_economy_loader_has_twelve_goods_and_recipes():
    graph = load_recipe_graph(ROOT / "content" / "economy.json")

    assert len(graph.good_ids) == 12
    assert set(graph.good_ids) == {
        "grain",
        "flour",
        "bread",
        "wood",
        "planks",
        "ore",
        "iron",
        "tools",
        "stone",
        "flax",
        "cloth",
        "fish",
    }
    assert graph.A[graph.good_index["grain"], graph.good_index["flour"]] == pytest.approx(1.2)
    assert graph.A[graph.good_index["flour"], graph.good_index["bread"]] == pytest.approx(0.8)
    assert graph.topological_order == tuple(sorted(graph.topological_order, key=lambda good: graph.topological_order.index(good)))


def test_multilevel_theoretical_demand_sums_common_inputs():
    graph = graph_from(
        {
            "grain": {},
            "flour": {"grain": 1.2},
            "bread": {"flour": 0.8, "wood": 0.1},
            "wood": {},
            "meal": {"grain": 0.5},
        }
    )

    demand = graph.theoretical_demand({"bread": 10, "meal": 4})
    assert demand[graph.good_index["bread"]] == pytest.approx(10)
    assert demand[graph.good_index["flour"]] == pytest.approx(8)
    assert demand[graph.good_index["grain"]] == pytest.approx(11.6)
    assert demand[graph.good_index["wood"]] == pytest.approx(1)


def test_cycle_is_rejected_with_path():
    with pytest.raises(RecipeCycleError, match=r"a.*b.*a"):
        graph_from({"a": {"b": 1}, "b": {"a": 2}})


def test_own_stock_reservations_and_strategic_stock():
    graph = graph_from({"ore": {}, "iron": {"ore": 2}})
    enterprises = build_enterprise_matrices(
        [
            {
                "id": "smelter",
                "good": "iron",
                "plan": 5,
                "stock": {"ore": 10},
                "production_reserved": {"ore": 4},
                "other_purpose_reservation": {"ore": 2},
                "strategic_stock": {"ore": 3},
            }
        ],
        graph,
    )
    result = calculate_production_orders(graph, enterprises)
    ore = graph.good_index["ore"]

    # 10 - 2 (other purpose) - 3 (strategic) = 5 usable; plan needs 10.
    assert result.usable_stock[0, ore] == pytest.approx(5)
    assert result.input_orders[0, ore] == pytest.approx(5)
    assert result.available_output[0, graph.good_index["iron"]] == pytest.approx(2.5)
    assert result.input_reason_codes[0, ore] == "order_required"
    assert all(value >= 0 for value in result.input_orders.ravel())


def test_due_delivery_covers_need_but_late_delivery_does_not():
    graph = graph_from({"ore": {}, "iron": {"ore": 2}})
    rows = [
        {
            "id": "due",
            "good": "iron",
            "plan": 5,
            "required_turn": 3,
            "in_transit": [{"good": "ore", "quantity": 10, "arrival_turn": 3}],
        },
        {
            "id": "late",
            "good": "iron",
            "plan": 5,
            "required_turn": 3,
            "in_transit": [{"good": "ore", "quantity": 10, "arrival_turn": 4}],
        },
    ]
    enterprises = build_enterprise_matrices(rows, graph)
    result = calculate_production_orders(graph, enterprises)
    ore = graph.good_index["ore"]

    assert result.due_supply[0, ore] == pytest.approx(10)
    assert result.input_orders[0, ore] == pytest.approx(0)
    assert result.input_reason_codes[0, ore] == "covered_by_due_delivery"
    assert result.late_supply[1, ore] == pytest.approx(10)
    assert result.input_orders[1, ore] == pytest.approx(10)
    assert result.input_reason_codes[1, ore] == "order_required_late_delivery_ignored"


def test_zero_demand_has_zero_orders_and_explicit_no_plan_reason():
    graph = graph_from({"ore": {}, "iron": {"ore": 2}})
    enterprises = build_enterprise_matrices(
        [{"id": "empty", "good": "iron", "plan": 0, "stock": {"ore": 100}}],
        graph,
    )
    result = calculate_production_orders(graph, enterprises)

    assert np.array_equal(result.input_orders, np.zeros((1, 2)))
    assert result.enterprise_reason_codes == ("no_plan",)
    assert result.input_diagnostics == ()


def test_enterprise_row_permutation_is_invariant_and_ids_are_sorted():
    graph = graph_from({"ore": {}, "iron": {"ore": 2}})
    rows = [
        {"id": "zeta", "good": "iron", "plan": 4, "stock": {"ore": 1}},
        {"id": "alpha", "good": "iron", "plan": 3, "stock": {"ore": 9}},
    ]
    first = calculate_production_orders(graph, build_enterprise_matrices(rows, graph))
    second = calculate_production_orders(graph, build_enterprise_matrices(reversed(rows), graph))

    assert first.enterprise_ids == second.enterprise_ids == ("alpha", "zeta")
    np.testing.assert_array_equal(first.input_orders, second.input_orders)
    np.testing.assert_array_equal(first.available_output, second.available_output)
    assert first.input_reason_codes.tolist() == second.input_reason_codes.tolist()


def test_mapping_matrices_are_read_only_and_no_negative_order_is_possible():
    graph = graph_from({"ore": {}, "iron": {"ore": 2}})
    enterprises = build_enterprise_matrices(
        [{"id": "smelter", "good": "iron", "plan": 1, "stock": {"ore": 100}}],
        graph,
    )
    result = calculate_production_orders(graph, enterprises)

    with pytest.raises(ValueError):
        graph.A[0, 1] = 99
    assert np.all(result.input_orders >= 0)
    assert np.all(result.available_output >= 0)
