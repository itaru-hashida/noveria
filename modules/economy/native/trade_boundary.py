"""Граница исполнения сделки: одна привязка реализации на импорт.

Модуль не хранит состояние движка: ядро получает ссылки на сам движок, его
``OrderStore`` и ``CashStore``. Копий заявок и счетов нет, поэтому остальной
код продолжает читать те же словари.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-теста на побитовое совпадение и как fallback, если расширение не
собрано. Имена привязываются один раз при импорте, чтобы не платить за
диспетчер на каждой сделке.

Модуль не импортирует ``modules.economy.support``: EPS приходит аргументом,
поэтому циклического импорта нет.
"""

import builtins
import importlib

# Встроенные max/min: на границе ``max(0, x)`` они сохраняют тип аргумента,
# поэтому целый 0 остаётся целым, как в прежнем Python-коде.
_PY_MAX = builtins.max
_PY_MIN = builtins.min


def commit_trade_python(owner, buy, sell, quote, quantity, price, eps, cash, store):
    """Эталон прежнего участка ``match``: одна сделка, порядок операций тот же.

    Аргументы повторяют native-ядро один в один, чтобы focused-тест мог
    прогнать обе реализации на одинаковом состоянии и сравнить побитово.
    """
    buyer_id = buy['participant']
    seller_id = sell['participant']
    good = buy['good']
    freight, distance, path = quote[1:]
    seller = owner.accounts[seller_id]
    # Освобождается только резерв исполненной части, остальной остаётся защищён.
    release = quantity*buy['price']
    if quantity*price > owner.free_cash(buyer_id)+release+eps:
        release = _PY_MIN(buy['reserved'], _PY_MAX(release, quantity*price))
    cash.release_reserved_cash(buyer_id, release)
    store.set_reserved(buy, _PY_MAX(0, buy['reserved']-release))
    seller['stock_reserved'][good] = _PY_MAX(0, seller['stock_reserved'][good]-quantity)
    value = owner.use_stock(seller_id, good, quantity)
    owner.add_stock(buyer_id, good, quantity, quantity*price)
    gross = quantity*sell['price']
    tax = gross*owner.cfg['consumption_tax'] if buy.get('final_consumption', False) else 0.0
    flow_start = len(owner.report['flows'])
    trade_id = f'{owner.meta["month"]}:trade:{len(owner.report["trades"])}'
    owner.transfer(buyer_id, seller_id, gross-tax, 'Товар')
    owner.transfer(buyer_id, 'treasury:'+seller['state_id'], tax,
                   'Налог с конечной потребительской продажи')
    owner.transfer(buyer_id, 'carrier:'+seller['state_id'], quantity*freight, 'Перевозка')
    for flow in owner.report['flows'][flow_start:]:
        flow.update(buy_order=buy['id'], sell_order=sell['id'], good=good,
                    quantity=quantity, trade_id=trade_id)
    firm = owner.enterprises.get(seller_id)
    if firm:
        firm['revenue'] += gross
        firm['tax'] = firm.get('tax', 0)+tax
        firm['cost_of_sales'] = firm.get('cost_of_sales', 0)+value
    if buy['purpose'] == 'Строительные материалы':
        materials = owner.accounts[buyer_id]['project']['materials']
        materials[good] = materials.get(good, 0)+quantity
    store.set_remaining(buy, _PY_MAX(0, buy['remaining']-quantity))
    store.set_remaining(sell, _PY_MAX(0, sell['remaining']-quantity))
    owner.report['trades'].append(dict(
        trade_id=trade_id, good=good, quantity=quantity, price=sell['price'],
        gold=gross, seller_gold=gross-tax, tax=tax, freight=quantity*freight,
        distance=distance, path=path, buyer=buyer_id, seller=seller_id,
        buy_order=buy['id'], sell_order=sell['id'],
        buyer_state=buy['state_id'], seller_state=sell['state_id'],
        buyer_province=buy['province_id'], seller_province=sell['province_id'],
        purpose=buy['purpose'], final_consumption=buy.get('final_consumption', False)))
    store.set_status(sell, 'filled' if sell['remaining'] <= eps else 'partial')
    owner._demand_revision[good] += 1
    owner._trades_by_seller[seller_id] += 1
    owner.mark_market_quotes_dirty(good)
    return owner.report['trades'][-1]


def load_trade_kernel():
    """Вернуть native-модуль trade_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.trade_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_TRADE_KERNEL = False

_kernel = load_trade_kernel()
if _kernel is not None:
    commit_trade = _kernel.commit_trade
    NATIVE_TRADE_KERNEL = True
else:
    commit_trade = commit_trade_python
