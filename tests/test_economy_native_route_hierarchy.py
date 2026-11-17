"""Иерархические маршруты HPA*: точность, fallback, кэш и инвалидация.

Тест держит контракт шага: иерархия обязана возвращать не «примерно тот же»
путь, а ровно тот, что вернул бы прежний полный Dijkstra, включая выбор
предка при равных дистанциях, и обязана молча уступать ему там, где
гарантий нет.

Проверяется:

  1. значения и порядок — стоимость и путь совпадают с networkx-эталоном на
     реальной карте, сетках и синтетических случайных графах;
  2. узкие места — равные расстояния, нулевые веса, отсутствующий weight,
     целые веса и типы;
  3. fallback — multigraph, oriented-графы, нестандартный и невалидный вес,
     граф меньше порога, отсутствие расширения;
  4. кэш — иерархия не заводит второго кэша маршрутов, деревья по-прежнему
     лежат в ``engine.paths`` и ``ROUTE_TREE_CACHE``;
  5. инвалидация — правка карты сбрасывает только затронутые регионы и не
     оставляет устаревших ответов;
  6. RNG — маршруты не двигают состояние генератора.
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
from modules.economy.native import route_hierarchy as hierarchy  # noqa: E402
from modules.economy.native import route_hierarchy_boundary as boundary  # noqa: E402

pytestmark = pytest.mark.skipif(not boundary.NATIVE_HIERARCHY,
                                reason='Cython-расширение route_hierarchy не собрано')


# ---------------------------------------------------------------------------
# Синтетический движок: та же карта, что в focused route-тестах.
# ---------------------------------------------------------------------------
def tile(identity, x, y, passable=True, province='p', cost=1, road=True):
    return dict(id=identity, x=x, y=y, passable=passable, cost=cost, road=road,
                province_id=province, resources=['grain'], terrain='plain')


def make_engine(tiles):
    cfg = json.loads((ROOT / 'tests/fixtures/economy.json').read_text(
        encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S',
                     properties={'capital_id': 'c1'}),
                dict(id='p', kind='Province', name='P',
                     properties={'capital_id': 'c1'}),
                dict(id='c1', kind='City', name='C1',
                     properties={'province_id': 'p', 'tile_id': tiles[0]['id']}),
                dict(id='h', kind='SocialGroup', name='H',
                     properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c1')]
    engine = Economy(dict(entities=entities, links=links), tiles,
                     meta=dict(id='game', version=1, month=0, config=cfg))
    engine.seed = 17
    engine.rng.seed(17)
    engine.bootstrap()
    return engine


def grid_engine(side, seed=17, zero=False):
    """Квадратная карта со стоимостью клетки, как у настоящего мира."""
    rng = random.Random(seed)
    tiles = []
    for x in range(side):
        for y in range(side):
            cost = rng.choice([0.0, 1.0, 2.0, 5.0] if zero else [0.5, 1.0, 2.0, 5.0])
            tiles.append(tile('tile-%d-%d' % (x, y), x, y, cost=cost))
    engine = make_engine(tiles)
    # Порог включается явно: тест проверяет иерархию, а не порог.
    engine._route_min_nodes = 4
    engine._route_chunk = 4
    engine.invalidate_routes()
    return engine


def build_grid(side, seed=17, weights=(1.0,), missing=False, block=0.0):
    """Сетка NetworkX с заданными весами: эталон для сравнения путей."""
    rng = random.Random(seed)
    graph = nx.Graph()
    coords = {}
    for x in range(side):
        for y in range(side):
            if rng.random() < block:
                continue
            node = 'n%d_%d' % (x, y)
            coords[node] = (x, y)
            graph.add_node(node)
    for node, (x, y) in list(coords.items()):
        for dx, dy in ((1, 0), (0, 1)):
            other = 'n%d_%d' % (x + dx, y + dy)
            if other not in coords:
                continue
            if missing and rng.random() < 0.4:
                graph.add_edge(node, other)
            else:
                graph.add_edge(node, other, weight=rng.choice(weights))
    return graph, coords


def build_region_graph(graph, coords, chunk=4):
    nodes = list(coords)
    xs = [coords[node][0] for node in nodes]
    ys = [coords[node][1] for node in nodes]
    return hierarchy.build(graph._adj, nodes, xs, ys, chunk)


def assert_matches_reference(graph, region_graph, start, goal):
    """Стоимость и путь совпадают с полным Dijkstra до последнего бита."""
    distances, paths = nx.single_source_dijkstra(graph, start, weight='weight')
    expected = (distances.get(goal), paths.get(goal, []))
    assert hierarchy.search(region_graph, start, goal) == expected


# ---------------------------------------------------------------------------
# 1. Значения и порядок.
# ---------------------------------------------------------------------------
def test_real_map_city_pair_matches_networkx():
    """Реальная карта: иерархия возвращает путь прежнего Dijkstra."""
    engine = grid_engine(24, seed=5)
    nodes = list(engine.graph.nodes())
    for start, goal in ((nodes[0], nodes[-1]), (nodes[7], nodes[40]),
                        (nodes[0], nodes[0])):
        assert_matches_reference(engine.graph, build_region_graph_native(engine),
                                 start, goal)


def build_region_graph_native(engine):
    region_graph = boundary.build(engine)
    assert region_graph is not None, boundary.last_reason()
    return region_graph


def test_engine_path_answers_point_to_point():
    """``engine.path`` отдаёт ровно ту же пару, что и полный Dijkstra."""
    engine = grid_engine(24, seed=5)
    nodes = list(engine.graph.nodes())
    start, goal = nodes[0], nodes[-1]
    distances, paths = nx.single_source_dijkstra(engine.graph, start,
                                                 weight='weight')
    assert engine.path(start, goal) == (distances[goal], paths[goal])


def test_source_path_contains_source_itself():
    engine = grid_engine(12)
    node = list(engine.graph.nodes())[0]
    assert engine.path(node, node) == (0, [node])


def test_unreachable_pair_returns_none_and_empty_path():
    """Недостижимая пара остаётся парой ``(None, [])``, как раньше."""
    tiles = [tile('t1', 0, 0), tile('t2', 6, 6)]
    engine = make_engine(tiles)
    engine._route_min_nodes = 1
    engine.invalidate_routes()
    assert engine.path('t1', 't2') == (None, [])


def test_hierarchical_bound_is_a_valid_upper_bound():
    """Иерархическая ступень даёт путь карты не дороже истинного."""
    graph, coords = build_grid(12, seed=3)
    region_graph = build_region_graph(graph, coords)
    nodes = list(coords)
    for k in range(20):
        start, goal = nodes[k*7 % len(nodes)], nodes[(k*13+5) % len(nodes)]
        bound = hierarchy.bound(region_graph, start, goal)
        if bound is None:
            continue
        cost, path = bound
        assert path[0] == start and path[-1] == goal
        recomputed = 0.0
        for a, b in zip(path, path[1:]):
            assert graph.has_edge(a, b), (a, b)
            recomputed += graph[a][b].get('weight', 1)
        assert recomputed == cost
        true = nx.single_source_dijkstra_path_length(graph, start, weight='weight')
        if goal in true:
            assert cost >= true[goal]


# ---------------------------------------------------------------------------
# 2. Узкие места контракта.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('name,weights', [
    ('equal', (1.0,)),
    ('zeros', (0.0, 1.0)),
    ('ints', (1.0, 2.0, 3.0)),
    ('fractions', (0.1, 0.35, 0.5)),
])
def test_ties_and_zero_weights_match_networkx(name, weights):
    """Равные дистанции и нулевые веса: предка выбирает тот же протокол."""
    for trial in range(8):
        graph, coords = build_grid(9, seed=trial, weights=weights)
        region_graph = build_region_graph(graph, coords)
        nodes = list(coords)
        for k in range(6):
            start = nodes[k*5 % len(nodes)]
            goal = nodes[(k*11+3) % len(nodes)]
            assert_matches_reference(graph, region_graph, start, goal)


def test_missing_weight_defaults_to_one():
    """Ребро без ``weight`` считается единицей — и в иерархии тоже."""
    graph, coords = build_grid(9, seed=4, weights=(1.0, 2.0))
    for a, b in list(graph.edges())[:10]:
        del graph[a][b]['weight']
    region_graph = build_region_graph(graph, coords)
    nodes = list(coords)
    assert_matches_reference(graph, region_graph, nodes[0], nodes[-1])


def test_integer_weights_keep_integer_cost():
    """Тип стоимости не превращается в float без нужды."""
    graph = nx.Graph()
    graph.add_edge('a', 'b', weight=2)
    graph.add_edge('b', 'c', weight=3)
    region_graph = hierarchy.build(graph._adj, ['a', 'b', 'c'], [0, 1, 2],
                                   [0, 0, 0], 2)
    cost, path = hierarchy.search(region_graph, 'a', 'c')
    assert path == ['a', 'b', 'c']
    assert cost == 5


def test_random_synthetic_graphs_match_networkx():
    """Случайные графы: иерархия либо совпадает, либо уступает Dijkstra."""
    rng = random.Random(23)
    for _ in range(30):
        graph, coords = build_grid(rng.randint(4, 14), seed=rng.randint(0, 999),
                                   weights=(0.5, 1.0, 2.0, 0.0),
                                   block=rng.choice([0.0, 0.1]))
        if len(coords) < 2:
            continue
        region_graph = build_region_graph(graph, coords, chunk=rng.choice([3, 4, 8]))
        nodes = list(coords)
        for k in range(4):
            start = nodes[k*3 % len(nodes)]
            goal = nodes[(k*7+1) % len(nodes)]
            assert_matches_reference(graph, region_graph, start, goal)


# ---------------------------------------------------------------------------
# 3. Fallback.
# ---------------------------------------------------------------------------
def test_multigraph_stays_on_networkx():
    """У multigraph другое правило веса: иерархия его не подменяет."""
    graph = nx.MultiGraph()
    for k in range(80):
        graph.add_edge('a%d' % k, 'b%d' % k, weight=1)
    assert boundary.supports(graph, {}, min_nodes=1) == 'multigraph'


def test_oriented_graph_is_rejected():
    graph = nx.Graph()
    graph.add_edge('a', 'b')
    oriented = graph.to_directed()
    assert boundary.supports(oriented, {'a': {'x': 0, 'y': 0},
                                        'b': {'x': 1, 'y': 0}},
                             min_nodes=1) == 'directed'


def test_missing_weight_attribute_falls_back():
    """Отсутствующий weight — не повод угадывать: работает прежний Dijkstra."""
    graph = nx.Graph()
    graph.add_edge('a', 'b')
    tiles = {'a': {'x': 0, 'y': 0}, 'b': {'x': 1, 'y': 0}}
    assert boundary.supports(graph, tiles, min_nodes=1) == 'no_weight'


@pytest.mark.parametrize('weight,reason', [
    (-1.0, 'bad_weight'),
    (float('nan'), 'bad_weight'),
    (float('inf'), 'bad_weight'),
    ('1.0', 'bad_weight'),
    (True, 'bad_weight'),
])
def test_invalid_weight_falls_back(weight, reason):
    graph = nx.Graph()
    graph.add_edge('a', 'b', weight=weight)
    tiles = {'a': {'x': 0, 'y': 0}, 'b': {'x': 1, 'y': 0}}
    assert boundary.supports(graph, tiles, min_nodes=1) == reason


def test_small_graph_uses_threshold():
    """Маленький граф остаётся на прежнем Dijkstra по порогу, а не по ошибке."""
    graph, coords = build_grid(2, seed=1)
    tiles = {node: {'x': coords[node][0], 'y': coords[node][1]}
             for node in coords}
    assert graph.number_of_nodes() == 4
    assert boundary.supports(graph, tiles, min_nodes=5) == 'small'
    assert boundary.supports(graph, tiles, min_nodes=1) is None


def test_nodes_without_coordinates_fall_back():
    graph = nx.Graph()
    graph.add_edge('a', 'b', weight=1)
    assert boundary.supports(graph, {'a': {'x': 0, 'y': 0}}, min_nodes=1) == 'no_tile'


def test_hierarchy_threshold_keeps_small_maps_on_dijkstra():
    """Порог по умолчанию не пускает иерархию на действующую малую карту."""
    assert boundary.MIN_NODES >= 1024


def test_engine_path_stays_on_dijkstra_below_threshold():
    """Малая карта отвечает прежним путём и не строит иерархию."""
    engine = make_engine([tile('t1', 0, 0), tile('t2', 1, 0), tile('t3', 2, 0)])
    engine._route_min_nodes = boundary.MIN_NODES
    engine.invalidate_routes()
    distance, path = engine.path('t1', 't3')
    assert path == ['t1', 't2', 't3']
    assert getattr(engine, '_route_hierarchy', None) is None


# ---------------------------------------------------------------------------
# 4. Кэш остаётся кэшем движка.
# ---------------------------------------------------------------------------
def test_hierarchy_does_not_create_a_second_route_cache():
    """Иерархия не заводит глобального кэша: деревья лежат там же, где раньше."""
    engine = grid_engine(16)
    nodes = list(engine.graph.nodes())
    engine.invalidate_routes()
    engine.path(nodes[0], nodes[-1])
    # Единственный кэш деревьев по-прежнему живёт в движке и в общем
    # ROUTE_TREE_CACHE; у границы иерархии своего хранилища нет.
    assert not hasattr(boundary, '_cache')
    assert not hasattr(boundary, 'CACHE')
    assert set(engine.paths) <= {nodes[0]}
    assert engine._route_cache_key in support.ROUTE_TREE_CACHE or not engine.paths


def test_tree_still_lands_in_engine_paths_on_full_query():
    """Полный запрос (start == end) по-прежнему строит дерево в ``engine.paths``."""
    engine = grid_engine(16)
    node = list(engine.graph.nodes())[0]
    engine.invalidate_routes()
    assert engine.paths == {}
    engine.path(node, node)
    assert node in engine.paths
    assert engine.paths[node][0][node] == 0


def test_point_to_point_does_not_grow_paths():
    """Точечный запрос не обязан строить дерево на всю карту."""
    engine = grid_engine(16)
    nodes = list(engine.graph.nodes())
    engine.invalidate_routes()
    engine.path(nodes[0], nodes[0])
    before = dict(engine.paths)
    engine.path(nodes[1], nodes[-1])
    assert len(engine.paths) <= len(before)+1


def test_invalidate_routes_drops_hierarchy():
    """Правка карты сбрасывает иерархию: устаревших ответов не остаётся."""
    engine = grid_engine(16)
    nodes = list(engine.graph.nodes())
    engine.path(nodes[0], nodes[-1])
    assert boundary.stats(engine)['enabled'] is True
    engine.invalidate_routes()
    assert getattr(engine, '_route_hierarchy', None) is None


def test_invalidation_marks_only_affected_regions():
    """Точечная инвалидация помечает только регионы затронутых клеток."""
    engine = grid_engine(24, seed=5)
    region_graph = build_region_graph_native(engine)
    nodes = list(engine.graph.nodes())
    # Прогрев: таблицы нескольких регионов посчитаны.
    hierarchy.search(region_graph, nodes[0], nodes[-1])
    ready_before = set(hierarchy.ready_regions(region_graph))
    assert ready_before
    target = nodes[0]
    region = hierarchy.region_of_node(region_graph, target)
    hierarchy.invalidate(region_graph, [target])
    assert hierarchy.dirty_regions(region_graph) == [region]
    # Чистые регионы сохранили таблицы, грязный ещё не пересчитан.
    assert set(hierarchy.ready_regions(region_graph)) == ready_before


def test_invalidation_picks_up_new_road():
    """Новая дорога видна после правки карты: устаревший путь не отдаётся."""
    engine = grid_engine(16, zero=True)
    nodes = list(engine.graph.nodes())
    start, goal = nodes[0], nodes[-1]
    first = engine.path(start, goal)
    # Правим вес существующего ребра и сбрасываем маршруты: так же поступает
    # редактор карты и постройка дороги.
    neighbour = next(iter(engine.graph[start]))
    engine.graph[start][neighbour]['weight'] = 0.001
    engine.invalidate_routes()
    second = engine.path(start, goal)
    assert second[0] is not None
    # После инвалидации ответ согласован с текущим графом.
    distances, paths = nx.single_source_dijkstra(engine.graph, start,
                                                 weight='weight')
    assert second == (distances.get(goal), paths.get(goal, []))
    assert second[0] <= first[0]


# ---------------------------------------------------------------------------
# 5. RNG.
# ---------------------------------------------------------------------------
def test_rng_is_not_touched():
    """Маршруты — чистая функция от графа: RNG-состояние не меняется."""
    engine = grid_engine(16)
    nodes = list(engine.graph.nodes())
    before = engine.rng.getstate()
    engine.path(nodes[0], nodes[-1])
    assert engine.rng.getstate() == before
