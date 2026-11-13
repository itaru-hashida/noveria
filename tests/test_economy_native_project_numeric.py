"""Числовые ledger: порядок обхода, rollback trial и контракт кэша."""
import copy
import math
import random

import numpy as np
import pytest

from modules.economy import Economy

native = pytest.importorskip('modules.economy.native.serial_step')


class Reference:
    sustainable_flow_headroom = Economy.sustainable_flow_headroom
    sustainable_candidate_capacity = Economy.sustainable_candidate_capacity
    recipe_graph = Economy.recipe_graph
    recipe_requirements = Economy.recipe_requirements
    investment_expansion_intents = Economy.investment_expansion_intents

    def __init__(self, recipes, capacities, committed=None):
        self.goods = {good: dict(inputs=inputs) for good, inputs in recipes.items()}
        self.rows = {(state, good): dict(capacity_supply=capacity,
            committed=(committed or {}).get((state, good), 0.0),
            rampable_free=max(0, capacity-(committed or {}).get((state, good), 0.0)))
            for (state, good), capacity in capacities.items()}
        self.cache = {}
        self.calls = []

    def investment_flow_balance(self, state, good):
        key = state, good
        if key not in self.cache:
            self.calls.append(key)
            self.cache[key] = dict(self.rows[key])
        return self.cache[key]


def packed(recipes, states=('s',)):
    goods = list(recipes)
    names = dict(good_names=goods, state_names=list(states),
        good_index={g: i for i, g in enumerate(goods)},
        state_index={s: i for i, s in enumerate(states)})
    offset, edge_goods, values = [0], [], []
    for recipe in recipes.values():
        for good, ratio in recipe.items():
            edge_goods.append(names['good_index'][good])
            values.append(ratio)
        offset.append(len(values))
    inv = dict(recipe_input_offset=np.asarray(offset, dtype=np.int32),
        recipe_input_good=np.asarray(edge_goods, dtype=np.int32),
        recipe_input_value=np.asarray(values, dtype=np.float64))
    return names, inv


@pytest.mark.parametrize('desired', [0.0, -1.0, float('nan'), 0.0000001,
                                    math.nextafter(7.0, 0), 80.0, 1e12])
def test_flow_exact_shared_ancestor_partial_trial_and_lazy_balances(desired):
    # Порядок ID и порядок вставки входов специально различаются.
    recipes = {'root': {'z': 0.7, 'a': 1.3}, 'z': {'shared': 1.1},
               'a': {'shared': 0.9}, 'shared': {}, 'unused': {}}
    names, inv = packed(recipes)
    capacities = {('s', g): q for g, q in zip(recipes, [100.0, 10.0, 11.0, 18.0, 0.0])}
    expected = Reference(recipes, capacities, {('s', 'shared'): 0.1})
    owner = copy.deepcopy(expected)
    kernel = native._ProjectFlowKernel(owner, names, inv)
    ledger = native._FlowLedger(names)
    reserved = {('s', 'unused'): 0.0, ('s', 'shared'): 0.2}
    for key, value in reserved.items():
        ledger[key] = value
    before = list(ledger.items())
    candidate = dict(state_id='s', good='root', quantity=desired)
    capacity, trial = expected.sustainable_candidate_capacity(candidate, reserved, expected.cache)
    actual, numeric = kernel.solve(0, 0, desired, ledger)
    assert actual == capacity
    assert list(numeric.items()) == list(trial.items())
    assert list(ledger.items()) == before
    assert owner.calls == expected.calls
    assert list(owner.cache) == list(expected.cache)
    assert ledger.data.dtype == np.float64 and ledger.present.dtype == np.uint8

    # Выход best следующего solve может быть его же входом. Предыдущие
    # принятые резервы обязаны пережить многократный неуспешный trial.
    for good in ('root', 'a', 'z', 'unused', 'root'):
        candidate.update(good=good, quantity=20.0)
        capacity, trial = expected.sustainable_candidate_capacity(candidate, trial, expected.cache)
        actual, numeric = kernel.solve(0, names['good_index'][good], 20.0, numeric)
        assert actual == capacity
        assert list(numeric.items()) == list(trial.items())
        assert owner.calls == expected.calls


def test_flow_cycle_reports_same_path_and_does_not_reject_unused_cycle():
    recipes = {'root': {'a': 1.0}, 'a': {'root': 1.0}, 'leaf': {}}
    names, inv = packed(recipes)
    capacities = {('s', g): 100.0 for g in recipes}
    reference = Reference(recipes, capacities)
    owner = copy.deepcopy(reference)
    kernel = native._ProjectFlowKernel(owner, names, inv)
    ledger = native._FlowLedger(names)
    with pytest.raises(ValueError) as expected:
        reference.sustainable_candidate_capacity(dict(state_id='s', good='root', quantity=5), {}, reference.cache)
    with pytest.raises(ValueError) as actual:
        kernel.solve(0, 0, 5, ledger)
    assert str(actual.value) == str(expected.value)
    assert not dict(ledger)
    assert owner.calls == reference.calls
    assert kernel.solve(0, 2, 5, ledger)[0] > 0


def test_flow_and_recipe_requirements_exact_for_random_dags_and_states():
    rng = random.Random(73129)
    for _ in range(30):
        goods = ['g%d' % i for i in range(8)]
        recipes = {g: {upstream: rng.choice([0.1, 0.7, 1.3, 2.1])
                       for upstream in goods[:i] if rng.random() < 0.4}
                   for i, g in enumerate(goods)}
        names, inv = packed(recipes, ('second', 'first'))
        capacities = {(s, g): rng.uniform(0, 200) for s in names['state_names'] for g in goods}
        committed = {key: rng.uniform(0, 20) for key in capacities}
        reference = Reference(recipes, capacities, committed)
        owner = copy.deepcopy(reference)
        kernel = native._ProjectFlowKernel(owner, names, inv)
        requirements = native._ProjectRecipes(names, inv)
        ledger = native._FlowLedger(names)
        reserved = {}
        for good in reversed(goods):
            state = rng.choice(names['state_names'])
            desired = rng.uniform(0, 50)
            candidate = dict(state_id=state, good=good, quantity=desired)
            capacity, reserved = reference.sustainable_candidate_capacity(candidate, reserved, reference.cache)
            actual, ledger = kernel.solve(names['state_index'][state], names['good_index'][good], desired, ledger)
            assert actual == capacity
            assert list(ledger.items()) == list(reserved.items())
            expected = reference.recipe_requirements(good, desired)
            assert list(requirements.requirements(names['good_index'][good], desired).items()) == list(expected.items())
        assert owner.calls == reference.calls


def test_recipe_workspace_deep_chain_and_cycle():
    recipes = {'g%d' % i: {'g%d' % (i+1): 1.1} for i in range(64)}
    recipes['g64'] = {}
    names, inv = packed(recipes)
    reference = Reference(recipes, {})
    actual = native._ProjectRecipes(names, inv)
    assert actual.requirements(0, 7.0) == reference.recipe_requirements('g0', 7.0)
    recipes['g64']['g32'] = 0.7
    names, inv = packed(recipes)
    reference = Reference(recipes, {})
    with pytest.raises(ValueError) as expected:
        reference.recipe_requirements('g0', 7.0)
    with pytest.raises(ValueError) as actual:
        native._ProjectRecipes(names, inv).requirements(0, 7.0)
    assert str(actual.value) == str(expected.value)


def test_expansion_accumulation_exact_with_shared_inputs_and_duplicate_parents():
    recipes = {'root': {'z': 0.7, 'a': 1.3}, 'z': {'shared': 1.1},
               'a': {'shared': 0.9}, 'shared': {}}
    names, inv = packed(recipes, ('z-state', 'a-state'))
    capacities = {(s, g): 3.1 for s in names['state_names'] for g in recipes}
    reference = Reference(recipes, capacities)
    owner = copy.deepcopy(reference)
    kernel = native._ProjectFlowKernel(owner, names, inv)
    accumulator = native._ExpansionFlows(kernel, names)
    demands = [dict(state_id=s, good='root', quantity=q, decision_id='same')
               for s in names['state_names'] for q in [4.0, 3.7]]
    expected = reference.investment_expansion_intents(demands, reference.cache)
    for demand in sorted(demands, key=lambda r: (r['state_id'], r['good'], r['decision_id'])):
        accumulator.add(names['state_index'][demand['state_id']], 0,
                        demand['quantity'], demand['decision_id'])
    assert accumulator.rows() == expected
    assert owner.calls == reference.calls


def test_upstream_numeric_cache_negative_hits_rounding_dimensions_and_payload_identity():
    cache = native._UpstreamCache(8)
    names = dict(account_names=['owner'], good_names=['a', 'b'], state_names=['s', 'other'],
                 province_names=['p', 'other-p'])
    account = {'id': 'owner'}
    project = {'capacity': 10, 'parent_project_id': 'first-parent'}
    diagnostics = [{'stage': 'capacity', 'capacity': 0.0}]
    rounded = round(0.123456781, 8)
    assert cache.lookup(0, 0, rounded, 0) == -1
    cache.store(0, 0, rounded, 0, -1, None, diagnostics)
    assert cache.lookup(0, 0, round(0.123456784, 8), 0) == 0
    assert cache.result(0, {'owner': account}, names) is None
    assert cache.last_diagnostics() is diagnostics
    for key in [(1, 0, rounded, 0), (0, 1, rounded, 0),
                (0, 0, rounded, 1), (0, 0, round(0.123456789, 8), 0)]:
        assert cache.lookup(*key) == -1
    cache.store(1, 0, 1.0, 0, 0, project, [])
    account['project'] = project
    owner, cached = cache.result(cache.lookup(1, 0, 1.0, 0), {'owner': account}, names)
    assert owner is account and cached is project
    old = {('b', 'other', 3.0, 'other-p'): []}
    result = cache.materialize_diagnostics(names, old)
    assert result[('a', 's', rounded, 'p')] is diagnostics
    assert result[('b', 's', 1.0, 'p')] == []
    cloned = copy.deepcopy(cache)
    assert cloned.lookup(1, 0, 1.0, 0) == 1
    assert cloned.result(1, {'owner': account}, names)[1] == project
    assert cloned.result(1, {'owner': account}, names)[1] is not project
