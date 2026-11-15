# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Строительные buy orders: цена-потолок и выставление закупки на недостающий материал.

Ядро — ровно тот Python-участок, который до сих пор жил в хуке
``material_boundary.place_order_hook``: расчёт цены закупки по котировке и
создание buy order через существующий hook ``V3.order``. Прежнее тело было
двумя строками:

    _, price, _ = owner.quoted_cost(account, good, missing, sells)
    owner.order(account['id'], good, missing, 'buy', price, PURPOSE)

Оно и перенесено: ``quoted_cost`` даёт ``value``, ``ceiling``, ``exact``;
потолок (``ceiling``) — это и есть цена закупки, а ``value`` и ``exact`` прежний
код не использовал. Создание заявки идёт через тот же ``owner.order``.

Границы среза:

  * недостающий объём (``missing``) и количество закупки считает нативный
    ``material_kernel.reserve_materials``: он обходит смету, закрепляет склад
    за проектом и зовёт хук на каждый материал, включая ``missing == 0``.
    Здесь это значение не пересчитывается — иначе появился бы второй источник
    истины для количества;
  * ``quoted_cost`` и ``order`` — хуки движка: книга, кэш котировок, резерв
    материала, ``OrderStore.append``, ``CashStore.add_reserved_cash``, ревизия
    спроса и отчёт остаются у движка. Ядро задаёт только их порядок и
    аргументы;
  * денежное урезание закупки (``min(quantity, free_cash/price)``), расчёт
    ``order['reserved']`` и его запись в ``CashStore`` живут в ``V3.order``:
    второй копии денежной политики ядро не заводит;
  * ``project['materials']`` и ``stock_reserved`` уже обновлены нативным
    ``material_kernel`` до вызова; dirty goods старый участок не помечал
    (buy-ветка ``V3.order`` двигает ревизию спроса, а не котировки), поэтому и
    ядро их не помечает;
  * rollback не добавляется: выставленная закупка остаётся выставленной.

Арифметика намеренно ``object``: ``missing`` бывает и ``int``, и ``float``
(``int``-остаток, например целый ноль, обязан остаться ``int`` и в аргументе
заявки), а ``price`` — ``float`` из ``quoted_cost``. Значения передаются как
есть, без арифметики, поэтому тип и последний разряд сохраняются побитово.
Распаковка котировки — та же тройная, что и в прежнем теле: кортеж другой
длины должен давать ту же ошибку.
"""


def place_order(object owner, object account, object good, object missing,
                object sells, object purpose, object quote, object order):
    """Выставить строительную закупку на недостающий материал.

    ``quote`` — хук движка ``quote(owner, account, good, missing, sells)``,
    возвращающий тройку ``quoted_cost``. ``order`` — хук движка
    ``order(owner, account_id, good, quantity, side, price, purpose)``: это
    существующий ``V3.order``, который сам урезает количество по свободным
    деньгам, считает ``reserved`` и пишет его в ``CashStore``.

    Порядок прежний и обязательный: сначала котировка (одна, с распаковкой
    тройки), затем заявка. Заявка выставляется на каждый материал, включая
    ``missing == 0``: пустую заявку отбрасывает сам ``V3.order``.
    """
    cdef object price
    _, price, _ = quote(owner, account, good, missing, sells)
    order(owner, account['id'], good, missing, 'buy', price, purpose)
    return None
