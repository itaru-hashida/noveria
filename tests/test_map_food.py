import copy
import shutil
import subprocess

import pytest

from scripts.check_map_food import CHAIN, solve
from test_economy_v3 import engine

solver_available = subprocess.run([shutil.which('python'), '-c', 'import scipy'], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not solver_available, reason='Нужен SciPy: requirements-research.txt в Python решателя')


def simple(engine):
    for f in engine.enterprises.values():
        f.update(capacity=20, output=0, available_from=0)
        f['suspended'] = f['good'] not in CHAIN
    engine.cfg['basic_needs']['bread'] = 20/100000
    return engine


def test_complete_chain_respects_shared_resource_and_recipes(engine):
    simple(engine)
    before = copy.deepcopy((engine.accounts, engine.enterprises, engine.meta, engine.rng.getstate()))
    result = solve(engine, False)
    # 6 зерна -> 5 муки -> 6.25 хлеба. Древесины и труда достаточно.
    assert result['success']
    assert result['bread'] == pytest.approx(6.25)
    assert result['max_constraint_error'] < 1e-6
    assert (engine.accounts, engine.enterprises, engine.meta, engine.rng.getstate()) == before


def test_competing_flax_reserves_same_grain_resource(engine):
    simple(engine)
    flax = next(f for f in engine.enterprises.values() if f['good'] == 'flax')
    flax['output'] = 3
    assert solve(engine, True)['bread'] == pytest.approx(3.125)


def test_unreachable_households_cannot_be_fed(engine):
    simple(engine)
    engine.graph.add_node('isolated')
    engine.accounts['h']['tile_id'] = 'isolated'
    assert solve(engine, False)['bread'] == 0


def test_impossible_external_demand_is_not_silently_dropped(engine):
    simple(engine)
    engine.graph.add_node('isolated')
    other = next(f for f in engine.enterprises.values() if f['good'] == 'planks')
    other['output'] = 1
    engine.accounts[other['id']]['tile_id'] = 'isolated'
    assert not solve(engine, True)['success']


def test_labor_limits_whole_chain_instead_of_each_firm_separately(engine):
    simple(engine)
    engine.cfg['labor_share'] = 1.37/100000
    assert solve(engine, False)['bread'] == pytest.approx(1)


def test_owner_bread_uses_same_production_instead_of_disappearing(engine):
    simple(engine)
    assert solve(engine, True, owner_bread={'t': 1})['bread'] == pytest.approx(5.25)
