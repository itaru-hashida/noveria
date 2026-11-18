# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Release sell orders: снятие закреплённой части и отмена опустевшего оффера.

Ядро — последний Python-участок release внутри ``project_material_requests``.
Условия, обход и порядок уже живут в ``material_kernel.reserve_materials``:
оттуда приходят живой оффер, уже выбранный ``take`` и ``release``. Здесь
остались ровно те две мутации, которые до сих пор выполнялись Python-хуками
``material_boundary``:

  * ``detach_offer`` — снятие закреплённой части собственного sell-оффера:
    ``quantity``, ``remaining`` через существующую границу заявок и ``backing``.
    Порядок полей прежний и обязательный;
  * ``settle_release`` — пометка котировок грязными
    (``mark_market_quotes_dirty``) и отмена опустевшего оффера
    (``cancel_order``) с прежней причиной.

Что осталось у Python и почему:

  * выбор проекта, порядок инвесторов, отмена по простою и по марже/спросу,
    доставка и защита денег — в ``v3`` и ``delivery_boundary`` до этого вызова;
  * обход ``sells[good]``, проверки ``participant``/``status``,
    ``take = min(release, remaining)`` и пропуск ``take <= eps`` — в
    ``material_kernel``: это тот же нативный цикл резерва, и второй его копии
    здесь нет;
  * ``stock_reserved``, ``project['materials']`` и строительные закупки —
    ``material_kernel``;
  * ``mark_market_quotes_dirty`` и ``cancel_order`` — хуки движка: они меняют
    кэши котировок, ревизии спроса и ``cash_store``, поэтому ядро задаёт только
    момент вызова и аргументы, а тела остаются у движка;
  * второго order/material state нет: ``offer`` — живой dict заявки, ``store`` —
    существующая граница мутаций (``order_boundary``), переданная вызовом;
  * rollback не добавляется: частично снятый оффер остаётся снятым, как и
    прежде.

Арифметика намеренно ``object``: ``quantity``/``remaining``/``backing`` бывают
и ``int``, и ``float``, а тип виден дальше в отчёте. Операции записаны теми же
``-=``/``-``, что и в прежнем теле, поэтому тип и порядок вычислений совпадают
побитово.
"""

# Причина отмены опустевшего sell-оффера: прежний литерал v3.
RELEASE_REASON = 'Материалы закреплены за собственной стройкой'


def detach_offer(object owner, object store, object offer, object take):
    """Снять закреплённую часть собственного sell-оффера с рынка.

    Порядок прежний: ``quantity``, затем ``remaining`` через границу заявок,
    затем ``backing``. ``remaining`` читается заново после записи ``quantity``,
    потому что прежнее тело читало поле из того же словаря, а не из локальной
    переменной. ``store.set_remaining`` пишет и колонку, и dict: второго
    источника остатка не появляется.

    Все три записи идут через владельца заявки: ``quantity`` и ``backing`` —
    его metadata-поля, ``remaining`` — колонка. Прямой записи в dict оффера
    здесь нет, иначе ядро обходило бы ``OrderStore``.
    """
    store.set_quantity(offer, offer['quantity']-take)
    store.set_remaining(offer, offer['remaining']-take)
    store.set_metadata(offer, 'backing', offer['backing']-take)
    return None


def settle_release(object owner, object offer, object good, object eps):
    """Пометить котировки грязными и отменить опустевший оффер.

    Порядок прежний: сначала ``mark_market_quotes_dirty`` — ревизия котировок
    растёт на каждое снятие, даже если оффер не опустел, — и только потом
    проверка ``remaining <= eps`` и ``cancel_order``. Причина — прежний
    литерал ``RELEASE_REASON``.
    """
    owner.mark_market_quotes_dirty(good)
    if offer['remaining'] <= eps:
        owner.cancel_order(offer, RELEASE_REASON)
    return None
