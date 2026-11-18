"""Граница release sell orders: одна привязка реализации на импорт.

Модуль не хранит состояние движка и не создаёт второго order state: ``offer`` —
живой dict заявки движка, поэтому ``quantity``/``remaining``/``backing``,
которые меняет ядро, — те же поля, что читает остальной движок, а ``store`` —
существующая граница мутаций (``order_boundary.OrderStore``), переданная
вызовом, а не пересозданная здесь.

Что перенесено в ядро (``release_kernel``):

  * снятие закреплённой части собственного sell-оффера
    (``detach_offer``): ``quantity``, ``remaining`` через границу заявок,
    ``backing`` — в прежнем порядке полей;
  * окончание release (``settle_release``): пометка котировок грязными и
    отмена опустевшего оффера с прежней причиной.

Что осталось в Python и почему:

  * выбор проекта, порядок инвесторов, отмена по простою и по марже/спросу,
    доставка и защита денег — в ``v3`` и ``delivery_boundary`` до этого вызова;
  * обход ``sells[good]``, проверки ``participant``/``status``,
    ``take = min(release, remaining)``, ``stock_reserved``,
    ``project['materials']`` и строительные закупки — ``material_kernel``:
    release получает уже выбранный оффер, поэтому второго цикла и второго
    material state нет;
  * ``mark_market_quotes_dirty`` и ``cancel_order`` — хуки движка: денежная
    политика, ревизии спроса, ``cash_store`` и отчёт остаются у движка, ядро
    задаёт только момент вызова и аргументы;
  * rollback не добавляется: частично снятый оффер остаётся снятым.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Реализации принимают одни и те же аргументы, поэтому тест гоняет обе
на одинаковом состоянии и сравнивает значения, типы и порядок вызовов хуков.

Имена привязываются один раз при импорте, чтобы не платить за диспетчер на
каждом оффере. Модуль не импортирует ``modules.economy.support``: EPS приходит
аргументом, поэтому циклического импорта нет.
"""

import importlib

# Причина отмены опустевшего sell-оффера: прежний литерал v3. У ядра и у
# эталона константа одна, поэтому подменить текст причины нельзя незаметно.
RELEASE_REASON = 'Материалы закреплены за собственной стройкой'


def _store_metadata(store, order, key, value):
    """Keep lightweight test doubles compatible with the owner boundary."""
    setter = getattr(store, 'set_metadata', None)
    if setter is None:
        order[key] = value
    else:
        setter(order, key, value)


def _store_quantity(store, order, value):
    setter = getattr(store, 'set_quantity', None)
    if setter is None:
        order['quantity'] = value
    else:
        setter(order, value)


def detach_offer_python(owner, store, offer, take):
    """Эталон снятия закреплённой части: те же поля в том же порядке."""
    _store_quantity(store, offer, offer['quantity']-take)
    store.set_remaining(offer, offer['remaining']-take)
    _store_metadata(store, offer, 'backing', offer['backing']-take)
    return None


def settle_release_python(owner, offer, good, eps):
    """Эталон окончания release: те же два side effect в том же порядке."""
    owner.mark_market_quotes_dirty(good)
    if offer['remaining'] <= eps:
        owner.cancel_order(offer, RELEASE_REASON)
    return None


def load_release_kernel():
    """Вернуть native-модуль release_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталонную Python-ветку.
    """
    try:
        return importlib.import_module('.release_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_RELEASE_KERNEL = False

_kernel = load_release_kernel()
if _kernel is not None:
    detach_offer = _kernel.detach_offer
    settle_release = _kernel.settle_release
    NATIVE_RELEASE_KERNEL = True
else:
    detach_offer = detach_offer_python
    settle_release = settle_release_python
