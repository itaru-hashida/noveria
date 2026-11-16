"""Граница маршрутов: одна привязка ядра на импорт, без диспетчера.

Модуль не хранит состояния. Граф принадлежит движку (``engine.graph``), кэш
маршрутов — тоже (``engine.paths`` и ``ROUTE_TREE_CACHE``); ядро получает
живой ``graph._adj`` и возвращает те же два словаря, что и networkx.

Ядро заменяет только ``networkx.single_source_dijkstra``: остальной контракт
``EconomyServices.path`` (проверка формы графа раз в месяц, ключ кэша,
общий ``ROUTE_TREE_CACHE``, кэш ``self.paths``) остаётся в ``support.py``.
Поэтому второго кэша и второй карты нет: дерево кладётся туда же, куда его
клал networkx.

Если расширение не собрано, используется ``networkx`` напрямую — это и
эталон, и fallback. Для multigraph эталон остаётся обязательным: у него
другое правило веса (минимум по параллельным рёбрам), и подменять его
нельзя молча.
"""
import importlib
import networkx as nx


def single_source_python(graph, source):
    """Эталон: буквальный вызов networkx, которым жил ``path``."""
    return nx.single_source_dijkstra(graph, source, weight='weight')


def load_route_kernel():
    """Вернуть native-модуль route_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.route_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_ROUTE = False

_kernel = load_route_kernel()
if _kernel is not None:
    NATIVE_ROUTE = True


def single_source(graph, source):
    """Дерево кратчайших путей от ``source``: ``(distances, paths)``.

    Multigraph и граф без ``_adj`` (подкласс с другим хранилищем) остаются на
    networkx: ядро воспроизводит ровно словарную ветку ``Graph``/``DiGraph``.
    """
    if not NATIVE_ROUTE or graph.is_multigraph():
        return nx.single_source_dijkstra(graph, source, weight='weight')
    return _kernel.single_source(graph._adj, source)
