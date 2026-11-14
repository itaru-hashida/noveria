# cython: language_level=3
"""Исполнение одной уже выбранной сделки: заявки, деньги, отчёт.

Ядро не выбирает buyer/seller и не считает ``q``: это делает Python-цикл
``match``. Сюда приходит готовое решение, и здесь оно исполняется ровно в
прежнем порядке.

Тела ``transfer``/``use_stock``/``add_stock`` намеренно НЕ копируются: они
вызываются у движка, поэтому переопределение в наследниках, сценариях и
тестах продолжает работать. Ядро убирает Python-фрейм сделки и держит
горячие мутации заявок и отчёта в компилированном коде.

Порядок операций и арифметика повторяют прежний Python-код один в один:
освобождение резервов, списание/зачисление товара, переводы (seller, tax,
carrier), доходы фирмы, материалы стройки, остатки и статусы заявок,
flows/trades и ревизии. Никакого rollback и новых транзакционных правил
здесь нет.
"""

import builtins

# Python-``max``/``min`` берутся один раз. Встроенная C-версия вернула бы
# другой тип на границе ``max(0, x)`` при ``x == 0.0`` (целый 0 вместо float),
# и последние разряды отчёта сдвинулись бы.
_PY_MAX = builtins.max
_PY_MIN = builtins.min


def commit_trade(object owner, object buy, object sell, object quote,
                 object quantity, object price, object eps,
                 object cash, object store):
    """Исполнить одну сделку. Возвращает запись ``report['trades']``."""
    cdef object buyer_id = buy['participant']
    cdef object seller_id = sell['participant']
    cdef object good = buy['good']
    cdef object accounts = owner.accounts
    cdef object report = owner.report
    # Счетец продавца читается до освобождения резерва: так было и в Python.
    cdef object seller = accounts[seller_id]
    # Связанные методы движка: вызов идёт через объект, поэтому подмена
    # ``transfer``/``use_stock``/``add_stock`` в наследнике остаётся в силе.
    cdef object free_cash = owner.free_cash
    cdef object use_stock = owner.use_stock
    cdef object add_stock = owner.add_stock
    cdef object transfer = owner.transfer
    cdef object mark_dirty = owner.mark_market_quotes_dirty
    cdef object rest = quote[1:]
    cdef object freight, distance, path, release, value, gross, tax
    cdef object trade_id, flow_start, flows, trades, firm, materials
    cdef object stock_reserved, month

    freight, distance, path = rest
    # Освобождается только резерв исполненной части, остальной остаётся защищён.
    release = quantity*buy['price']
    # Допуск цены задан на единицу. На большой партии разница может превысить
    # денежный EPS: освобождаем обеспеченную фактическую сумму, не затрагивая
    # резервы других заявок и не меняя обычные сделки.
    if quantity*price > free_cash(buyer_id)+release+eps:
        release = _PY_MIN(buy['reserved'], _PY_MAX(release, quantity*price))
    # Резерв счёта и резерв заявки — разные поля: первый освобождает
    # исполненную часть, второй уменьшается на ту же сумму.
    cash.release_reserved_cash(buyer_id, release)
    store.set_reserved(buy, _PY_MAX(0, buy['reserved']-release))
    stock_reserved = seller['stock_reserved']
    stock_reserved[good] = _PY_MAX(0, stock_reserved[good]-quantity)
    value = use_stock(seller_id, good, quantity)
    add_stock(buyer_id, good, quantity, quantity*price)
    gross = quantity*sell['price']
    if buy.get('final_consumption', False):
        tax = gross*owner.cfg['consumption_tax']
    else:
        tax = 0.0
    flows = report['flows']
    flow_start = len(flows)
    trades = report['trades']
    month = owner.meta['month']
    trade_id = f'{month}:trade:{len(trades)}'
    transfer(buyer_id, seller_id, gross-tax, 'Товар')
    transfer(buyer_id, 'treasury:'+seller['state_id'], tax,
             'Налог с конечной потребительской продажи')
    transfer(buyer_id, 'carrier:'+seller['state_id'], quantity*freight, 'Перевозка')
    for flow in flows[flow_start:]:
        flow.update(buy_order=buy['id'], sell_order=sell['id'], good=good,
                    quantity=quantity, trade_id=trade_id)
    firm = owner.enterprises.get(seller_id)
    if firm:
        firm['revenue'] = firm['revenue']+gross
        firm['tax'] = firm.get('tax', 0)+tax
        firm['cost_of_sales'] = firm.get('cost_of_sales', 0)+value
    if buy['purpose'] == 'Строительные материалы':
        materials = accounts[buyer_id]['project']['materials']
        materials[good] = materials.get(good, 0)+quantity
    store.set_remaining(buy, _PY_MAX(0, buy['remaining']-quantity))
    store.set_remaining(sell, _PY_MAX(0, sell['remaining']-quantity))
    trades.append(dict(trade_id=trade_id, good=good, quantity=quantity,
        price=sell['price'], gold=gross, seller_gold=gross-tax, tax=tax,
        freight=quantity*freight, distance=distance, path=path,
        buyer=buyer_id, seller=seller_id, buy_order=buy['id'], sell_order=sell['id'],
        buyer_state=buy['state_id'], seller_state=sell['state_id'],
        buyer_province=buy['province_id'], seller_province=sell['province_id'],
        purpose=buy['purpose'], final_consumption=buy.get('final_consumption', False)))
    store.set_status(sell, 'filled' if sell['remaining'] <= eps else 'partial')
    owner._demand_revision[good] = owner._demand_revision[good]+1
    # Прогнозу выпуска нужны только свои сделки.
    owner._trades_by_seller[seller_id] = owner._trades_by_seller[seller_id]+1
    mark_dirty(good)
    return trades[len(trades)-1]
