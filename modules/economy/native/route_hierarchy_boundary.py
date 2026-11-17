"""Граница иерархических маршрутов: HPA* на native-ядре с честным fallback.

Модуль не хранит состояния. Иерархия строится по живому ``engine.graph`` и
живёт на движке (``engine._route_hierarchy``) до ближайшей инвалидации, поэтому
второго глобального кэша маршрутов не появляется: деревья по-прежнему лежат в
``engine.paths`` и ``ROUTE_TREE_CACHE``.

Зачем иерархия вообще нужна. ``single_source`` строит дерево от истока на всю
карту. Это выгодно, когда с одного истока спрашивают много целей, но цена
построения растёт линейно от размера карты. На больших картах пара запросов
«точка-точка» дешевле, чем дерево на всю карту: HPA* читает регионы, а не карту.

Почему малые карты остаются на прежнем пути. Пока карта меньше ``MIN_NODES``,
построение дерева дешевле поточечного поиска, а дерево ещё и переиспользуется
кэшем движка. Порог намеренно высокий: действующая карта (около 1200 клеток)
обязана остаться на прежнем Dijkstra, иначе parity-базлайн сдвинулся бы не от
маршрутов, а от порядка вызовов.

Когда включается fallback. Иерархия молча уступает прежнему Dijkstra, если:
  * расширение не собрано;
  * граф — multigraph (у него другое правило веса) или oriented;
  * у узлов нет целых координат клетки или есть узел вне карты;
  * вес ребра отсутствует, не число, отрицателен или не конечен;
  * граф меньше порога ``min_nodes``;
  * иерархия вернула ``None`` — пути нет либо он не единственен, и тогда
    предка при равных дистанциях обязан выбрать Dijkstra.

Причина отказа пишется в ``last_reason`` для диагностики; на результат она не
влияет.
"""
import importlib
import math

# Порог, ниже которого иерархия не включается: на малых картах дерево от
# истока дешевле и переиспользуется кэшем движка.
MIN_NODES = 4096
# Размер региона по умолчанию: 32x32 клетки.
DEFAULT_CHUNK = 32

_last_reason = None


def load_route_hierarchy():
    """Вернуть native-модуль route_hierarchy или None, если он не собран."""
    try:
        return importlib.import_module('.route_hierarchy', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_HIERARCHY = False

_kernel = load_route_hierarchy()
if _kernel is not None:
    NATIVE_HIERARCHY = True


def last_reason():
    """Причина последнего отказа иерархии (или None, если она ответила)."""
    return _last_reason


def _set_reason(reason):
    global _last_reason
    _last_reason = reason


def threshold(engine):
    """Порог включения: атрибут движка перекрывает значение по умолчанию."""
    value = getattr(engine, '_route_min_nodes', None)
    return MIN_NODES if value is None else int(value)


def supports(graph, tiles, min_nodes=MIN_NODES):
    """Поддерживается ли граф иерархией; причина отказа или None."""
    if not NATIVE_HIERARCHY:
        return 'no_native'
    if graph is None or not hasattr(graph, '_adj'):
        return 'no_adj'
    if graph.is_multigraph():
        return 'multigraph'
    if graph.is_directed():
        return 'directed'
    if graph.number_of_nodes() < min_nodes:
        return 'small'
    for node, neighbours in graph._adj.items():
        tile = tiles.get(node)
        if tile is None:
            return 'no_tile'
        x = tile.get('x')
        y = tile.get('y')
        if not isinstance(x, int) or not isinstance(y, int):
            return 'no_coords'
        for data in neighbours.values():
            weight = data.get('weight')
            if weight is None:
                return 'no_weight'
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                return 'bad_weight'
            if not math.isfinite(weight) or weight < 0:
                return 'bad_weight'
    return None


def build(engine):
    """Собрать RegionGraph по живому графу движка (или None при отказе)."""
    reason = supports(engine.graph, engine.tiles, threshold(engine))
    if reason is not None:
        _set_reason(reason)
        return None
    nodes = list(engine.graph.nodes())
    # Порядок узлов — порядок графа (он же порядок карты): от него зависит
    # нумерация слотов, а значит и детерминизм при равных дистанциях.
    xs = [engine.tiles[node]['x'] for node in nodes]
    ys = [engine.tiles[node]['y'] for node in nodes]
    chunk = int(getattr(engine, '_route_chunk', DEFAULT_CHUNK) or DEFAULT_CHUNK)
    try:
        graph = _kernel.build(engine.graph._adj, nodes, xs, ys, chunk)
    except ValueError:
        _set_reason('build_error')
        return None
    _set_reason(None)
    return graph


def route(engine, start, end):
    """Точечный маршрут: ``(distance, path)`` или None, если иерархия уступила.

    ``None`` означает «спроси прежний Dijkstra»: либо пара не поддержана,
    либо кратчайший путь не единственен, либо пути нет.
    """
    hierarchy = getattr(engine, '_route_hierarchy', None)
    if hierarchy is None:
        reason = supports(engine.graph, engine.tiles, threshold(engine))
        if reason is not None:
            _set_reason(reason)
            return None
        hierarchy = build(engine)
        if hierarchy is None:
            return None
        engine._route_hierarchy = hierarchy
    if hierarchy.node_index(start) < 0 or hierarchy.node_index(end) < 0:
        _set_reason('unknown_node')
        return None
    result = _kernel.search(hierarchy, start, end)
    if result is None:
        _set_reason('ambiguous' if hierarchy.ambiguity() else 'no_path')
        return None
    _set_reason(None)
    return result


def invalidate(engine, nodes=None):
    """Инвалидировать иерархию: узлы — только их регионы, None — всю карту."""
    hierarchy = getattr(engine, '_route_hierarchy', None)
    if hierarchy is None:
        return
    if nodes is None:
        engine._route_hierarchy = None
        return
    _kernel.invalidate(hierarchy, list(nodes))


def stats(engine):
    """Диагностика: разбиение, входы, кэш регионов и причина отказа."""
    hierarchy = getattr(engine, '_route_hierarchy', None)
    if hierarchy is None:
        return dict(enabled=False,
                    reason=supports(engine.graph, engine.tiles, threshold(engine)))
    return dict(enabled=True, reason=None, **hierarchy.stats())
