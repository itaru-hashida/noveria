"""Маршруты в Cython: дерево кратчайших путей обязано совпасть с networkx.

Тест держит три вещи, которые легко потерять при замене
``networkx.single_source_dijkstra``:

  * значения — те же дистанции и те же пути, включая сам исток;
  * порядок — ключи ``dist``/``paths`` идут в порядке фиксации узлов, а при
    равных дистанциях предка выбирает счётчик кучи, а не сравнение узлов;
  * кэш — дерево кладётся в прежние ``engine.paths`` и ``ROUTE_TREE_CACHE``,
    поэтому второго кэша маршрутов не появляется.

Отдельно проверяется fallback: эталон ``route_boundary.single_source_python``
— буквальный вызов networkx, и он обязан выдавать то же самое, что ядро.
"""
import json
import random
import sys
from pathlib import Path

import networkx as nx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import route_boundary  # noqa: E402


# ---------------------------------------------------------------------------
# Синтетический движок: карта из одинаковых клеток, как в тестах книги.
# ---------------------------------------------------------------------------
def tile(identity, x, y, passable=True, province='p'):
    return dict(id=identity, x=x, y=y, passable=passable, cost=1, road=True,
                province_id=province, resources=['grain'], terrain='plain')


def make_engine(tiles):
    cfg = json.loads((ROOT / 'tests/fixtures/economy.json').read_text(
        encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S',
                     properties={'capital_id': 'c1'}),
                dict(id='p', kind='Province', name='P',
                     properties={'capital_id': 'c1'}),
                dict(id='c1', kind='City', name='C1',
                     properties={'province_id': 'p', 'tile_id': 't1'}),
                dict(id='h', kind='SocialGroup', name='H',
                     properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c1')]
    engine = Economy(dict(entities=entities, links=links), tiles,
                     meta=dict(id='game', version=1, month=0, config=cfg))
    engine.seed = 17
    engine.rng.seed(17)
    engine.bootstrap()
    return engine


def reference(graph, source):
    """Эталон: буквальный networkx-вызов, которым жил ``path``."""
    return nx.single_source_dijkstra(graph, source, weight='weight')


def assert_same(graph, source):
    left = reference(graph, source)
    right = route_boundary.single_source(graph, source)
    assert list(left[0].items()) == list(right[0].items())
    assert list(left[1].items()) == list(right[1].items())
    return left


# --- 1. Значения и порядок ------------------------------------------------
def test_linear_route_matches_networkx_paths():
    """На реальной карте вес ребра задан стоимостью клетки, а не единицей."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0), tile('t3', 2, 0)])
    distances, paths = route_boundary.single_source(engine.graph, 't1')
    step = engine.graph['t1']['t2']['weight']
    assert step > 0
    assert distances == {'t1': 0, 't2': step, 't3': step*2}
    assert paths == {'t1': ['t1'], 't2': ['t1', 't2'], 't3': ['t1', 't2', 't3']}
    assert distances == reference(engine.graph, 't1')[0]


def test_source_path_contains_source_itself():
    """networkx кладёт в paths и сам исток: ``['a']``, а не пустой список."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    _, paths = route_boundary.single_source(engine.graph, 't1')
    assert paths['t1'] == ['t1']


def test_isolated_and_blocked_tiles_are_absent():
    """Недостижимая клетка не попадает ни в dist, ни в paths."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0),
                          tile('t3', 5, 5, passable=False)])
    distances, paths = route_boundary.single_source(engine.graph, 't1')
    assert 't3' not in distances
    assert 't3' not in paths


def test_engine_path_returns_networkx_pair():
    """``engine.path`` отдаёт ровно ту же пару, что и networkx."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0), tile('t3', 2, 0)])
    expected = reference(engine.graph, 't1')
    assert engine.path('t1', 't3') == (expected[0]['t3'], expected[1]['t3'])
    assert engine.path('t1', 't1') == (0, ['t1'])


def test_unreachable_pair_returns_none_and_empty_path():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0, passable=False)])
    assert engine.path('t1', 't2') == (None, [])


# --- 2. Узкие места контракта --------------------------------------------
def test_equal_distances_pick_same_predecessor():
    """Два равных пути: предка выбирает порядок фиксации, как в networkx."""
    graph = nx.Graph()
    graph.add_edge('a', 'b', weight=1)
    graph.add_edge('b', 'c', weight=1)
    graph.add_edge('a', 'd', weight=1)
    graph.add_edge('d', 'c', weight=1)
    assert_same(graph, 'a')


def test_zero_weight_edges_do_not_break_order():
    graph = nx.Graph()
    graph.add_edge('a', 'b', weight=0)
    graph.add_edge('b', 'c', weight=2)
    graph.add_edge('a', 'c', weight=2)
    assert_same(graph, 'a')


def test_missing_weight_attribute_defaults_to_one():
    """networkx считает ребро без ``weight`` единицей — ядро тоже."""
    graph = nx.Graph()
    graph.add_edge('a', 'b')
    graph.add_edge('b', 'c', weight=3)
    distances, _ = route_boundary.single_source(graph, 'a')
    assert distances == {'a': 0, 'b': 1, 'c': 4}


def test_integer_weights_keep_integer_types():
    """Типы дистанций не превращаются в float: дальнейшая арифметика точна."""
    graph = nx.Graph()
    graph.add_edge('a', 'b', weight=2)
    graph.add_edge('b', 'c', weight=3)
    distances, _ = route_boundary.single_source(graph, 'a')
    assert type(distances['c']) is int
    assert distances['c'] == 5


def test_float_weights_preserve_exact_values():
    graph = nx.Graph()
    graph.add_edge('a', 'b', weight=0.1)
    graph.add_edge('b', 'c', weight=0.2)
    left = reference(graph, 'a')[0]
    right = route_boundary.single_source(graph, 'a')[0]
    assert left == right


# --- 3. Случайные графы: эталон и ядро совпадают побитово -----------------
def test_random_graphs_match_networkx_bit_for_bit():
    rng = random.Random(11)
    for _ in range(40):
        graph = nx.Graph()
        count = rng.randint(1, 14)
        graph.add_nodes_from(range(count))
        for source in range(count):
            for target in range(source+1, count):
                if rng.random() >= 0.35:
                    continue
                if rng.random() < 0.3:
                    graph.add_edge(source, target)
                else:
                    graph.add_edge(source, target,
                                   weight=rng.choice([0, 1, 2, 0.5, 1.25]))
        for source in list(graph.nodes())[:2]:
            assert_same(graph, source)


# --- 4. Кэш остаётся кэшем движка ---------------------------------------
def test_tree_lands_in_engine_cache():
    """Ядро не заводит своего кэша: дерево живёт в ``engine.paths``."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    engine.invalidate_routes()
    assert engine.paths == {}
    distance, path = engine.path('t1', 't2')
    assert path == ['t1', 't2']
    assert distance == engine.graph['t1']['t2']['weight']
    assert 't1' in engine.paths
    assert engine.paths['t1'] == reference(engine.graph, 't1')


def test_route_cache_is_shared_between_engines():
    """``ROUTE_TREE_CACHE`` остаётся общим: второй движок берёт готовое дерево."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    first = engine.path('t1', 't2')
    trees = support.ROUTE_TREE_CACHE[engine._route_cache_key]
    assert 't1' in trees
    assert trees['t1'] == reference(engine.graph, 't1')
    assert engine.path('t1', 't2') == first


def test_invalidate_routes_drops_native_trees():
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    expected = engine.path('t1', 't2')
    engine.invalidate_routes()
    assert engine.paths == {}
    assert engine.path('t1', 't2') == expected


# --- 5. Fallback без собранного .pyd ------------------------------------
def test_python_reference_matches_native_kernel():
    """Эталон границы — тот же networkx, поэтому сравнение тривиально честное."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0), tile('t3', 2, 0)])
    for source in ('t1', 't2', 't3'):
        assert (route_boundary.single_source_python(engine.graph, source)
                == route_boundary.single_source(engine.graph, source))


def test_fallback_switch_keeps_results(monkeypatch):
    """Выключенное ядро даёт те же значения: fallback не деградирует."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0), tile('t3', 2, 0)])
    native = engine.path('t1', 't3')
    # Ключ общего ROUTE_TREE_CACHE включает id(graph), а id переиспользуется,
    # поэтому в тесте общий кэш чистится явно — иначе можно взять чужое дерево.
    support.ROUTE_TREE_CACHE.clear()
    engine.invalidate_routes()
    monkeypatch.setattr(route_boundary, 'NATIVE_ROUTE', False)
    assert engine.path('t1', 't3') == native
    step = engine.graph['t1']['t2']['weight']
    assert engine.path('t1', 't3') == (step*2, ['t1', 't2', 't3'])


def test_multigraph_stays_on_networkx():
    """У multigraph другое правило веса, поэтому ядро его не подменяет."""
    graph = nx.MultiGraph()
    graph.add_edge('a', 'b', weight=5)
    graph.add_edge('a', 'b', weight=2)
    graph.add_edge('b', 'c', weight=1)
    distances, _ = route_boundary.single_source(graph, 'a')
    assert distances == {'a': 0, 'b': 2, 'c': 3}


def test_rng_is_not_touched():
    """Маршруты — чистая функция от графа: RNG-состояние не меняется."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0)])
    before = engine.rng.getstate()
    engine.path('t1', 't2')
    assert engine.rng.getstate() == before
