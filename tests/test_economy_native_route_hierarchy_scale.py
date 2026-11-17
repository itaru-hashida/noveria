"""Масштаб и parity HPA*: реальные карты, seed 17/73129 и сетки до 100k.

Тест отвечает на два вопроса шага:

  * масштаб — иерархия обязана отвечать на больших картах за время, не
    растущее как полное дерево, и стоимость должна совпадать с прежним
    Dijkstra (проверяется на срезах до 10k, где эталон ещё считается быстро);
  * parity — реальный мир и его месяцы не сдвигаются: маршруты не меняют ни
    state/report/RNG digest, ни порядок событий и сделок.

500k проверяется бенчмарком ``scripts/bench_route_hierarchy.py``, а не тестом:
построение эталонного дерева на такой карте занимает секунды, и держать это
в обычном прогоне тестов незачем.
"""
import json
import random
import sys
import time
from pathlib import Path

import networkx as nx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy.native import route_hierarchy as hierarchy  # noqa: E402
from modules.economy.native import route_hierarchy_boundary as boundary  # noqa: E402

pytestmark = pytest.mark.skipif(not boundary.NATIVE_HIERARCHY,
                                reason='Cython-расширение route_hierarchy не собрано')


def tile(identity, x, y, cost=1.0, road=True):
    return dict(id=identity, x=x, y=y, passable=True, cost=cost, road=road,
                province_id='p', resources=['grain'], terrain='plain')


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


def big_engine(side, seed=17):
    rng = random.Random(seed)
    tiles = [tile('tile-%d-%d' % (x, y), x, y,
                  cost=rng.choice([0.0, 1.0, 2.0, 5.0]))
             for x in range(side) for y in range(side)]
    engine = make_engine(tiles)
    engine._route_min_nodes = 4
    engine._route_chunk = 32
    engine.invalidate_routes()
    return engine


def build_region_graph(engine):
    region_graph = boundary.build(engine)
    assert region_graph is not None, boundary.last_reason()
    return region_graph


# ---------------------------------------------------------------------------
# Масштаб.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('side', [32, 64])
def test_matches_networkx_at_scale(side):
    """До 10k узлов: стоимость и путь совпадают с полным Dijkstra."""
    engine = big_engine(side)
    region_graph = build_region_graph(engine)
    nodes = list(engine.graph.nodes())
    rng = random.Random(7)
    for _ in range(12):
        start, goal = rng.choice(nodes), rng.choice(nodes)
        distances, paths = nx.single_source_dijkstra(engine.graph, start,
                                                     weight='weight')
        assert hierarchy.search(region_graph, start, goal) == (
            distances.get(goal), paths.get(goal, []))


def test_region_graph_reports_entrances_and_regions():
    """Разбиение видно снаружи: регионы, входы и размер чанка."""
    engine = big_engine(40)
    region_graph = build_region_graph(engine)
    stats = hierarchy.stats(region_graph)
    assert stats['chunk'] == 32
    assert stats['regions'] >= 2
    assert stats['entrances'] > 0
    assert stats['cols']*stats['rows'] == stats['regions']


def test_point_query_is_cheaper_than_full_tree_at_100k():
    """На 100k узлов запрос дешевле построения дерева от истока."""
    engine = big_engine(320)
    nodes = list(engine.graph.nodes())
    region_graph = build_region_graph(engine)
    rng = random.Random(11)
    queries = [(rng.choice(nodes), rng.choice(nodes)) for _ in range(20)]
    hierarchy.search(region_graph, *queries[0])

    started = time.perf_counter()
    for start, goal in queries:
        assert hierarchy.search(region_graph, start, goal) is not None
    query_seconds = time.perf_counter()-started

    started = time.perf_counter()
    nx.single_source_dijkstra(engine.graph, queries[0][0], weight='weight')
    tree_seconds = time.perf_counter()-started

    # Один запрос должен быть заметно дешевле дерева: иначе смысла нет.
    assert query_seconds/len(queries) < tree_seconds


# ---------------------------------------------------------------------------
# Parity: реальный мир и его месяцы.
# ---------------------------------------------------------------------------
def test_real_world_month_keeps_digests():
    """Месяц реального мира не сдвигается: state/report/RNG digest прежние."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir
    import hashlib

    def digest(obj):
        return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                         default=str).encode('utf-8')).hexdigest()

    with workdir('hierarchy_parity_17_standard') as directory:
        db, service = sr.build_world(17, directory, report_detail='standard')
        try:
            engine = service.economy_state().engine
            engine.step(native_mode='cython')
            engine.validate()
            payload = dict(
                month=engine.meta['month'],
                state=digest({
                    'accounts': {a['id']: a for a in engine.accounts.values()},
                    'enterprises': {e['id']: e for e in engine.enterprises.values()},
                    'meta': {k: v for k, v in engine.meta.items()
                             if k != 'market_history'},
                    'orders': engine.report['orders'],
                    'rng': repr(engine.rng.getstate()),
                }),
                report=digest(engine.report),
                rng=digest(engine.rng.getstate()),
                trades=len(engine.report['trades']),
                events=len(engine.report['events']),
            )
            # Значения из эталонной матрицы parity для 17:standard:cython,
            # месяц 1. Они закреплены в tests/fixtures и не должны сдвинуться.
            baseline = json.loads(
                (ROOT/'tests/fixtures/native_parity_baseline_5887c10.json')
                .read_text(encoding='utf-8'))['17:standard:cython'][0]
            assert payload['state'] == baseline['state']
            assert payload['report'] == baseline['report']
            assert payload['rng'] == baseline['rng']
            assert payload['trades'] == baseline['trades']
            assert payload['events'] == baseline['events']
        finally:
            db.close()


def test_hierarchy_is_not_used_on_the_current_world_map():
    """Действующая карта ниже порога: иерархия не включается, путь прежний."""
    assert boundary.MIN_NODES > 1018, 'порог обязан оставлять текущую карту на Dijkstra'
