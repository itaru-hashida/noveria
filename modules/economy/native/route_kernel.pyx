# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Ядро маршрутов: деревья кратчайших путей от одной площадки.

Заменяет ``networkx.single_source_dijkstra(graph, start, weight='weight')``
буквально: те же дистанции, те же пути и тот же порядок обхода.

Почему это hot path, а не «ещё один числовой расчёт»: ``path`` вызывается на
каждый ``landed`` промахом кэша и один раз строит дерево на всю карту
(``_dijkstra_multisource`` + лямбда веса — 96% времени профиля хода).
Заменяется только сам обход: ``nx``-вызов остаётся эталоном и fallback.

Контракт networkx 3.7 воспроизведён посимвольно:

  * обход идёт по ``G._adj`` — словарю ``{узел: {сосед: атрибуты}}``;
  * вес берётся как ``data.get('weight', 1)`` (ветка ``Graph``/``DiGraph``);
  * очередь — куча троек ``(distance, счётчик, узел)``: счётчик разрешает
    сравнение несравнимых узлов и задаёт порядок при равных дистанциях;
  * узел фиксируется при первом извлечении из кучи, поэтому порядок
    релаксаций — а значит и выбранный предок при равных путях — прежний;
  * пути восстанавливаются по первому предку и включают сам исток.

Ядро не владеет состоянием: ни графа, ни кэша маршрутов оно не хранит.
Дерево складывает движок в ``self.paths`` и ``ROUTE_TREE_CACHE``, как раньше.
"""

from heapq import heappush, heappop


def single_source(dict adj, object source):
    """Вернуть ``(dist, paths)`` ровно как ``single_source_dijkstra``.

    ``adj`` — живой ``graph._adj``: ядро его только читает.
    """
    cdef dict dist = {}
    cdef dict seen = {}
    cdef dict pred = {}
    cdef dict succ
    cdef dict paths
    cdef list fringe = []
    cdef Py_ssize_t tape = 0
    cdef object v = None
    cdef object u
    cdef object data
    cdef object dist_v
    cdef object vu_dist
    cdef list path

    seen[source] = 0
    heappush(fringe, (0, tape, source))
    while fringe:
        dist_v, _, v = heappop(fringe)
        if v in dist:
            continue
        dist[v] = dist_v
        succ = adj[v]
        for u, data in succ.items():
            vu_dist = dist_v+data.get('weight', 1)
            if u in dist:
                # networkx поднимает ValueError на противоречивых путях.
                # Отрицательных весов в мире нет, ветка сохранена буквально.
                if vu_dist < dist[u]:
                    raise ValueError(
                        'Contradictory paths found: negative weights?')
            elif u not in seen or vu_dist < seen[u]:
                seen[u] = vu_dist
                tape += 1
                heappush(fringe, (vu_dist, tape, u))
                pred[u] = v

    # Порядок восстановления — порядок фиксации узлов, как у networkx:
    # предок всегда зафиксирован раньше потомка, поэтому его путь уже готов.
    paths = {source: [source]}
    for v in dist:
        if v is source:
            continue
        path = paths[pred[v]]
        paths[v] = path+[v]
    return dist, paths
