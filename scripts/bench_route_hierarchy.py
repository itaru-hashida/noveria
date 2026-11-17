"""Бенчмарк HPA* против полного Dijkstra на сетках 1k, 10k, 100k и 500k узлов.

Меряются две вещи, которые решает шаг:

  * построение RegionGraph (разбиение, входы, локальные таблицы);
  * стоимость запроса «точка-точка» против построения дерева от истока.

Полный Dijkstra берётся буквальным эталоном ``networkx`` — тем, что заменяет
``route_kernel``; сравнение с native-ядром уже покрыто parity-тестами.

Запуск:

    python scripts/bench_route_hierarchy.py            — вся матрица;
    python scripts/bench_route_hierarchy.py --sizes 1000 10000
"""
import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import networkx as nx  # noqa: E402

from modules.economy.native import route_hierarchy as hierarchy  # noqa: E402

DEFAULT_SIZES = (1000, 10000, 100000, 500000)
DEFAULT_CHUNK = 32


def grid(size, seed=17):
    """Квадратная 4-связная сетка со стоимостью клетки, как у карты мира."""
    rng = random.Random(seed)
    side = int(size ** 0.5)
    graph = nx.Graph()
    coords = {}
    for x in range(side):
        for y in range(side):
            node = 'tile-%d-%d' % (x, y)
            coords[node] = (x, y)
            graph.add_node(node)
    for node, (x, y) in list(coords.items()):
        for dx, dy in ((1, 0), (0, 1)):
            other = 'tile-%d-%d' % (x + dx, y + dy)
            if other not in coords:
                continue
            graph.add_edge(node, other,
                           weight=rng.choice([0.35, 0.5, 1.0, 2.0, 5.0]))
    return graph, coords


def measure(size, pairs=40, seed=17):
    graph, coords = grid(size, seed=seed)
    nodes = list(coords)
    xs = [coords[node][0] for node in nodes]
    ys = [coords[node][1] for node in nodes]

    started = time.perf_counter()
    region_graph = hierarchy.build(graph._adj, nodes, xs, ys, DEFAULT_CHUNK)
    build_seconds = time.perf_counter()-started

    rng = random.Random(seed)
    queries = [(rng.choice(nodes), rng.choice(nodes)) for _ in range(pairs)]

    # Прогрев локальных таблиц: первый запрос в регионе их и строит.
    hierarchy.search(region_graph, queries[0][0], queries[0][1])

    started = time.perf_counter()
    answered = 0
    for start, goal in queries:
        if hierarchy.search(region_graph, start, goal) is not None:
            answered += 1
    query_seconds = time.perf_counter()-started

    started = time.perf_counter()
    for start, _ in queries[:max(1, pairs//8)]:
        nx.single_source_dijkstra(graph, start, weight='weight')
    tree_count = max(1, pairs//8)
    tree_seconds = (time.perf_counter()-started)/tree_count

    return dict(
        nodes=graph.number_of_nodes(), edges=graph.number_of_edges(),
        chunk=DEFAULT_CHUNK, regions=region_graph.stats()['regions'],
        entrances=region_graph.stats()['entrances'],
        build_ms=build_seconds*1000,
        query_us=query_seconds/max(len(queries), 1)*1e6,
        tree_ms=tree_seconds*1000,
        answered=answered, queries=len(queries),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', nargs='*', type=int, default=list(DEFAULT_SIZES))
    parser.add_argument('--pairs', type=int, default=40)
    options = parser.parse_args(argv)
    print('узлы    рёбра    регионы входы  сборка,мс  запрос,мкс  дерево,мс  '
          'ускорение  ответов')
    for size in options.sizes:
        row = measure(size, pairs=options.pairs)
        speedup = row['tree_ms']*1000/row['query_us'] if row['query_us'] else 0
        print('%-7d %-8d %-7d %-6d %-10.2f %-11.1f %-10.2f %-10.1f %d/%d' % (
            row['nodes'], row['edges'], row['regions'], row['entrances'],
            row['build_ms'], row['query_us'], row['tree_ms'], speedup,
            row['answered'], row['queries']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
