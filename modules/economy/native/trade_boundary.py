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
from .stock_store_boundary import get_stock_store


class _TradeTransaction(object):
    """Checkpoint the owners touched by one trade application.

    The calculation and the normal writes stay in ``commit_trade``.  This
    checkpoint only records the small owner surface needed to undo a failed
    application; it does not replace CashStore, StockStore or OrderStore.
    """

    def __init__(self, owner, buy, sell):
        self.owner = owner
        self.buy = buy
        self.sell = sell
        buyer_id = buy['participant']
        seller_id = sell['participant']
        good = buy['good']
        seller = owner.accounts[seller_id]
        state_id = seller['state_id']
        self.account_ids = (buyer_id, seller_id, 'treasury:'+state_id,
                            'carrier:'+state_id)
        self.cash = {}
        self.stock = {}
        self.values = {}
        self.projects = {}
        stock_store = get_stock_store(owner)
        for account_id in (buyer_id, seller_id):
            if account_id not in owner.accounts:
                continue
            account = owner.accounts[account_id]
            self.cash[account_id] = (owner.cash_store.get_balance(account_id),
                                     owner.cash_store.get_reserved_cash(account_id))
            stock_values = account['stock']
            reserved_field_present = 'stock_reserved' in account
            reserved_values = account.get('stock_reserved', {})
            self.stock[account_id] = (stock_values, good in stock_values,
                                      stock_values.get(good), reserved_values,
                                      good in reserved_values,
                                      reserved_values.get(good),
                                      reserved_field_present)
            values = account.get('values', {})
            self.values[account_id] = (values, good in values, values.get(good))
            if account_id == buyer_id and buy['purpose'] == 'Строительные материалы':
                materials = account['project']['materials']
                self.projects = (materials, good in materials, materials.get(good))
        self.enterprise = None
        if seller_id in owner.enterprises:
            enterprise = owner.enterprises[seller_id]
            self.enterprise = {key: (key in enterprise, enterprise.get(key))
                               for key in ('revenue', 'tax', 'cost_of_sales')}
        self.orders = {id(order): (order, order.get('reserved'),
                                    order.get('remaining'), order.get('status'))
                       for order in (buy, sell)}
        report = owner.report
        self.flow_count = len(report['flows'])
        self.trade_count = len(report['trades'])
        demand = owner._demand_revision
        self.demand_revision = (demand, good in demand, demand.get(good))
        trades_by_seller = owner._trades_by_seller
        self.trades_by_seller = (trades_by_seller, seller_id in trades_by_seller,
                                 trades_by_seller.get(seller_id))
        self.quote_version_present = '_market_quote_version' in owner.__dict__
        self.quote_version = owner.__dict__.get('_market_quote_version', 0)
        versions_present = '_market_quote_versions' in owner.__dict__
        versions = owner.__dict__.get('_market_quote_versions')
        self.quote_versions_present = versions_present
        self.quote_versions = (versions, good in versions, versions.get(good)) \
            if versions is not None else None
        self.quote_caches = tuple(
            (key, owner.__dict__[key])
            for key in ('_quoted_cost_cache', '_reference_price_cache')
            if key in owner.__dict__)
        totals = owner.__dict__.get('_physical_stock_totals')
        row = totals.get(buy['good']) if totals is not None else None
        self.physical_totals = ((totals, row, row['quantity'], row['count'])
                                if row is not None else None)

    def rollback(self):
        owner = self.owner
        cash = owner.cash_store
        stock = get_stock_store(owner)
        for account_id, (balance, reserved) in self.cash.items():
            cash.set_balance(account_id, balance)
            cash.set_reserved_cash(account_id, reserved)
        for account_id, saved in self.stock.items():
            (stock_values, stock_present, quantity, reserved_values,
             reserved_present, reserved, reserved_field_present) = saved
            stock.set_stock(account_id, self.buy['good'], quantity)
            stock.set_reserved(account_id, self.buy['good'], reserved)
            if stock_present:
                stock_values[self.buy['good']] = quantity
            else:
                stock_values.pop(self.buy['good'], None)
            if reserved_present:
                reserved_values[self.buy['good']] = reserved
            else:
                reserved_values.pop(self.buy['good'], None)
            if not reserved_field_present:
                owner.accounts[account_id].pop('stock_reserved', None)
            values, had_value, previous = self.values[account_id]
            if had_value:
                values[ self.buy['good']] = previous
            else:
                values.pop(self.buy['good'], None)
        if self.projects:
            materials, had_value, previous = self.projects
            if had_value:
                materials[self.buy['good']] = previous
            else:
                materials.pop(self.buy['good'], None)
        for order, reserved, remaining, status in self.orders.values():
            owner.order_store.set_reserved(order, reserved)
            owner.order_store.set_remaining(order, remaining)
            owner.order_store.set_status(order, status)
        if self.enterprise is not None:
            enterprise = owner.enterprises[self.sell['participant']]
            for key, (present, previous) in self.enterprise.items():
                if present:
                    enterprise[key] = previous
                else:
                    enterprise.pop(key, None)
        del owner.report['flows'][self.flow_count:]
        del owner.report['trades'][self.trade_count:]
        demand, present, previous = self.demand_revision
        if present:
            demand[self.buy['good']] = previous
        else:
            demand.pop(self.buy['good'], None)
        trades_by_seller, present, previous = self.trades_by_seller
        if present:
            trades_by_seller[self.sell['participant']] = previous
        else:
            trades_by_seller.pop(self.sell['participant'], None)
        if self.quote_version_present:
            owner._market_quote_version = self.quote_version
        else:
            owner.__dict__.pop('_market_quote_version', None)
        if self.quote_versions is not None:
            versions, present, previous = self.quote_versions
            if present:
                versions[self.buy['good']] = previous
            else:
                versions.pop(self.buy['good'], None)
        elif not self.quote_versions_present:
            owner.__dict__.pop('_market_quote_versions', None)
        for key, value in self.quote_caches:
            owner.__dict__[key] = value
        if self.physical_totals is not None:
            totals, row, quantity, count = self.physical_totals
            row['quantity'], row['count'] = quantity, count


def commit_trade_atomic(owner, buy, sell, quote, quantity, price, eps,
                        cash, store):
    """Apply one selected trade atomically through the existing owners."""
    transaction = _TradeTransaction(owner, buy, sell)
    try:
        return commit_trade(owner, buy, sell, quote, quantity, price, eps,
                            cash, store)
    except Exception:
        transaction.rollback()
        raise

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
    get_stock_store(owner).release_reserved(seller_id, good, quantity)
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
