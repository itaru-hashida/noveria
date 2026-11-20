"""Граница котировок и себестоимости: один native call site, эталон и fallback.

Перенесён числовой срез ``V3``:

  * ``reference_price`` — первая живая цена книги либо ориентир рынка;
  * ``quoted_cost`` — обход книги, накопление стоимости и потолка, затем
    ориентир восстановления выпуска;
  * ``unit_cost`` — компенсированные суммы ``raw``/``wear`` и итог;
  * ``volume_input_prices`` — средние доставленные цены входов плана;
  * ``input_bid_ceiling`` — предельная цена автоматической закупки;
  * ``reprice_input_order`` — решение о новой цене и доплате.

Кэши котировок (``_reference_price_cache``/``_quoted_cost_cache``/
``_unit_cost_cache``), ``_recovery_quotes``, книга заявок, склад, касса и
``landed`` остаются за движком: граница читает их через хуки и не заводит
второго хранилища. Мутации заявки и кассы в ``reprice_input_order`` тоже
остаются здесь — ядро отдаёт только числа.

Эталонная Python-ветка (``*_python``) повторяет прежнее тело ``V3``
буквально: она служит образцом для focused-теста на побитовое совпадение с
ядром и fallback, если расширение не собрано. Обе ветки читают одни и те же
хуки, поэтому по порядку вызовов они не расходятся.

Порядок вызовов хуков сохранён: ``reference_price`` зовёт ``market_signal``
только когда книга пуста, ``quoted_cost`` разбирает ``_recovery_quotes`` и
зовёт ``landed`` только когда книга не покрыла объём.
"""

import importlib
from collections import namedtuple

from ..support import EPS


QuoteSnapshot = namedtuple(
    'QuoteSnapshot', 'offers recovery_rows reference landed owner_id')


class QuoteHooks(object):
    """Живой доступ ядра к движку: только чтение плюс готовые числа.

    Хуки намеренно узкие. Всё, что говорит с кэшами котировок, книгой заявок,
    складом и кассой, остаётся в Python: ядру нужны лишь значения и вызовы,
    которые оно делает в прежнем порядке.
    """

    __slots__ = ('engine', 'buyer', 'good', 'sells')

    def __init__(self, engine, buyer, good, sells):
        self.engine = engine
        self.buyer = buyer
        self.good = good
        self.sells = sells

    def offers(self):
        """Живое представление книги — прежний ``self.offers(buyer, good, sells)``."""
        return self.engine.offers(self.buyer, self.good, self.sells)

    def signal(self):
        """Ориентир рынка — ``market_signal(...)['landed_price']``."""
        return self.engine.market_signal(
            self.engine.meta.get('market_history', []),
            self.buyer['state_id'], self.engine.market_province(self.buyer),
            self.good)['landed_price']

    def default_price(self):
        """Цена контента — прежний ``self.goods[good]['price']``."""
        return self.engine.goods[self.good]['price']

    def reference(self):
        """Прежний ``self.reference_price(buyer, good, sells)`` как хук."""
        return self.engine.reference_price(self.buyer, self.good, self.sells)

    def recovery_rows(self):
        """Прежний срез ``_recovery_quotes[good]``: пары ``(firm, price)``."""
        return getattr(self.engine, '_recovery_quotes', {}).get(self.good, [])

    def landed(self, tile_id, price):
        """Прежний ``self.landed(tile_id, market_tile(buyer), good, price)``."""
        return self.engine.landed(tile_id, self.engine.market_tile(self.buyer),
                                  self.good, price)


def prepare_quote_context(engine, buyer, good, sells):
    """Подготовить неизменяемую часть серии котировок одного товара."""
    hooks = QuoteHooks(engine, buyer, good, sells)
    return QuoteSnapshot(hooks.offers(), hooks.recovery_rows(), hooks.reference,
                         hooks.landed, buyer['id'])


def prepare_native_quote_context(engine, buyer, good, sells, maximum_missing,
                                 *, needs_reference):
    """Подготовить quote context без reference/landed callbacks."""
    hooks = QuoteHooks(engine, buyer, good, sells)
    offers = tuple((price, sell['remaining']) for price, sell, _ in hooks.offers())
    left = maximum_missing
    for _, remaining in offers:
        left -= min(left, remaining)
        if left <= EPS:
            break
    if needs_reference or left > EPS:
        # Старый quoted_cost запрашивает reference именно при недостаче
        # объёма, даже если книга частично покрыла заявку.
        reference = hooks.reference() if left > EPS else (
            offers[0][0] if offers else hooks.reference())
    else:
        reference = None
    if left <= EPS:
        return offers, reference, ()
    recovery_rows = hooks.recovery_rows()
    recovery_cache = engine.__dict__.setdefault(
        '_native_recovery_price_cache', {})
    recovery_key = (
        buyer.get('tile_id'), good, id(recovery_rows),
        getattr(engine, '_market_quote_version', 0))
    recovery_prices = recovery_cache.get(recovery_key)
    if recovery_prices is None:
        values = []
        for firm, price in recovery_rows:
            if firm['id'] == buyer['id']:
                continue
            landed = hooks.landed(firm['tile_id'], price)
            if landed:
                values.append(landed[0])
        recovery_prices = tuple(values)
        recovery_cache[recovery_key] = recovery_prices
    return offers, reference, tuple(recovery_prices)


def quoted_cost_prepared(context, quantity, eps=EPS):
    """Посчитать котировку по уже подготовленной книге и hooks."""
    offers, recovery_rows, reference, landed, owner_id = context
    return run_quoted(offers, quantity, reference, recovery_rows, owner_id,
                      landed, eps)


def quoted_cost_native_prepared(context, quantity, eps=EPS):
    """Python fallback для native quote context без callbacks."""
    offers, reference, recovery_prices = context
    left, value, ceiling = quantity, 0.0, 0.0
    for price, remaining in offers:
        q = min(left, remaining)
        value += q*price
        ceiling = max(ceiling, price)
        left -= q
        if left <= eps:
            break
    if left > eps:
        if recovery_prices:
            reference = max(reference, min(recovery_prices))
        value += left*reference
        ceiling = max(ceiling, reference)
    return value, max(ceiling, eps), left <= eps


# --- reference_price -------------------------------------------------------

def reference_price_python(engine, buyer, good, sells):
    """Эталон ``V3.reference_price``: буквальный прежний код с кэшем."""
    source = sells.get(good, [])
    key = (id(source), buyer['id'], engine.market_tile(buyer), good,
           getattr(engine, '_market_quote_version', 0))
    cache = getattr(engine, '_reference_price_cache', {}) if engine.QUOTE_CACHE else {}
    entry = cache.get(key)
    if entry is not None and entry[0] is source:
        return entry[1]
    offers = engine.offers(buyer, good, sells)
    if offers:
        result = offers[0][0]
    else:
        signal = engine.market_signal(engine.meta.get('market_history', []),
                                      buyer['state_id'],
                                      engine.market_province(buyer), good)
        result = signal['landed_price'] or engine.goods[good]['price']
    if engine.QUOTE_CACHE:
        cache[key] = (source, result)
        engine._reference_price_cache = cache
    return result


def reference_price(engine, buyer, good, sells):
    """Единственный вход из ``V3.reference_price``: ядро, кэш и промах."""
    source = sells.get(good, [])
    key = (id(source), buyer['id'], engine.market_tile(buyer), good,
           getattr(engine, '_market_quote_version', 0))
    cache = getattr(engine, '_reference_price_cache', {}) if engine.QUOTE_CACHE else {}
    entry = cache.get(key)
    if entry is not None and entry[0] is source:
        return entry[1]
    hooks = QuoteHooks(engine, buyer, good, sells)
    result = run_reference(hooks.offers(), hooks.signal, hooks.default_price())
    if engine.QUOTE_CACHE:
        cache[key] = (source, result)
        engine._reference_price_cache = cache
    return result


# --- quoted_cost -----------------------------------------------------------

def quoted_cost_python(engine, buyer, good, quantity, sells):
    """Эталон ``V3.quoted_cost``: буквальный прежний код с кэшем."""
    source = sells.get(good, [])
    key = (id(source), buyer['id'], engine.market_tile(buyer), good, quantity,
           getattr(engine, '_market_quote_version', 0))
    cache = getattr(engine, '_quoted_cost_cache', {}) if engine.QUOTE_CACHE else {}
    entry = cache.get(key)
    if entry is not None and entry[0] is source:
        return entry[1]
    left, value, ceiling = quantity, 0.0, 0.0
    for price, sell, _ in engine.offers(buyer, good, sells):
        q = min(left, sell['remaining'])
        value += q*price
        ceiling = max(ceiling, price)
        left -= q
        if left <= EPS:
            break
    if left > EPS:
        reference = engine.reference_price(buyer, good, sells)
        # Ориентир следующего выпуска не является предложением товара.
        quotes = [quote[0] for firm, price in getattr(engine, '_recovery_quotes', {}).get(good, [])
            if firm['id'] != buyer['id']
            and (quote := engine.landed(firm['tile_id'], engine.market_tile(buyer), good, price))]
        if quotes:
            reference = max(reference, min(quotes))
        value += left*reference
        ceiling = max(ceiling, reference)
    result = value, max(ceiling, EPS), left <= EPS
    if engine.QUOTE_CACHE:
        cache[key] = (source, result)
        engine._quoted_cost_cache = cache
    return result


def quoted_cost(engine, buyer, good, quantity, sells):
    """Котировка через числовой snapshot без native→Python callbacks."""
    source = sells.get(good, [])
    key = (id(source), buyer['id'], engine.market_tile(buyer), good, quantity,
           getattr(engine, '_market_quote_version', 0))
    cache = getattr(engine, '_quoted_cost_cache', {}) if engine.QUOTE_CACHE else {}
    entry = cache.get(key)
    if entry is not None and entry[0] is source:
        return entry[1]
    context = prepare_native_quote_context(
        engine, buyer, good, sells, quantity, needs_reference=True)
    result = quoted_cost_native_prepared(context, quantity, EPS)
    if engine.QUOTE_CACHE:
        cache[key] = (source, result)
        engine._quoted_cost_cache = cache
    return result


# --- unit_cost -------------------------------------------------------------

def unit_cost_python(engine, e, sells):
    """Эталон ``V3.unit_cost``: буквальный прежний код с кэшем."""
    source_key = id(sells)
    key = (source_key, e['id'], e['tile_id'], e['good'],
           getattr(engine, '_market_quote_version', 0))
    cache = getattr(engine, '_unit_cost_cache', {})
    cached = cache.get(key)
    if cached is not None:
        return cached
    recipe = engine.goods[e['good']]
    raw = sum(ratio*engine.reference_price(e, good, sells)
              for good, ratio in recipe['inputs'].items())
    wear = sum(quantity*engine.reference_price(e, good, sells)
               for good, quantity in engine.equipment_wear(recipe, 1).items())
    result = recipe['labor']*engine.cfg['wage']+raw+wear
    cache[key] = result
    engine._unit_cost_cache = cache
    return result


def unit_cost(engine, e, sells):
    """Единственный вход из ``V3.unit_cost``: ядро, кэш и промах."""
    source_key = id(sells)
    key = (source_key, e['id'], e['tile_id'], e['good'],
           getattr(engine, '_market_quote_version', 0))
    cache = getattr(engine, '_unit_cost_cache', {})
    cached = cache.get(key)
    if cached is not None:
        return cached
    recipe = engine.goods[e['good']]
    raw = run_prepared_cost_sum([
        (ratio, engine.reference_price(e, good, sells))
        for good, ratio in recipe['inputs'].items()])
    wear = run_prepared_cost_sum([
        (quantity, engine.reference_price(e, good, sells))
        for good, quantity in engine.equipment_wear(recipe, 1).items()])
    result = recipe['labor']*engine.cfg['wage']+raw+wear
    cache[key] = result
    engine._unit_cost_cache = cache
    return result


# --- volume_input_prices ---------------------------------------------------

def volume_input_prices_python(engine, e, quantity, sells):
    """Эталон ``V3.volume_input_prices``: буквальный прежний код."""
    prices = {}
    account = engine.accounts[e['id']]
    for good, ratio in engine.goods[e['good']]['inputs'].items():
        missing = max(0, quantity*ratio-engine.free_stock(e['id'], good))
        if missing > EPS:
            value, _, _ = engine.quoted_cost(account, good, missing, sells)
            prices[good] = value/missing
        else:
            prices[good] = engine.reference_price(e, good, sells)
    return prices


def volume_input_prices(engine, e, quantity, sells):
    """Посчитать цены по подготовленному snapshot без native callbacks."""
    account = engine.accounts[e['id']]
    items = list(engine.goods[e['good']]['inputs'].items())
    stock_values = {}
    quote_values = {}
    reference_values = {}
    for good, ratio in items:
        stock = engine.free_stock(e['id'], good)
        missing = max(0, quantity*ratio-stock)
        stock_values[good] = stock
        if missing > EPS:
            quote_values[good] = engine.quoted_cost(account, good, missing, sells)[0]
        else:
            reference_values[good] = engine.reference_price(e, good, sells)
    if _kernel is not None and hasattr(_kernel, 'volume_input_prices_prepared'):
        return _kernel.volume_input_prices_prepared(
            items, quantity, stock_values, quote_values, reference_values, EPS)
    return _volume_prepared_python(
        items, quantity, stock_values, quote_values, reference_values, EPS)


# --- input_bid_ceiling -----------------------------------------------------

def _bid_plan(engine, buy):
    """Общие проверки ``input_bid_ceiling``: план, месяц и состав входов.

    Возвращает ``(plan, inputs, prices)`` либо ``None``. Проверки оставлены в
    границе, потому что это чтение состояния предприятия и кассы, а не числа.
    """
    firm = engine.enterprises.get(buy['participant'])
    if (not firm or firm['status'] == 'closed' or firm.get('suspended')
            or not buy.get('adaptive_input_budget') or buy['side'] != 'buy'
            or buy['purpose'] != 'Сырьё' or buy.get('final_consumption')):
        return None
    plan = firm.get('production_plan', {})
    inputs = plan.get('input_ratios', {})
    prices = plan.get('input_prices', {})
    if (plan.get('month') != engine.meta['month'] or plan.get('output', 0) <= EPS
            or buy['id'] not in plan.get('input_orders', [])
            or inputs.get(buy['good'], 0) <= EPS
            or not inputs.keys() <= prices.keys()
            or 'expected_net_price' not in plan or 'wage_unit' not in plan):
        return None
    return plan, inputs, prices


def input_bid_ceiling_python(engine, buy):
    """Эталон ``V3.input_bid_ceiling``: буквальный прежний код."""
    found = _bid_plan(engine, buy)
    if found is None:
        return None
    plan, inputs, prices = found
    other_cost = plan['wage_unit']+plan.get('fixed_unit', 0)
    for good, quantity in inputs.items():
        if good == buy['good']:
            continue
        committed = [o['price'] for o in engine.report['orders']
            if o['id'] in plan['input_orders'] and o['good'] == good]
        other_cost += quantity*max([prices[good]]+committed)
    return max(0, (plan['expected_net_price']-other_cost)/inputs[buy['good']])


def input_bid_ceiling(engine, buy):
    """Единственный вход из ``V3.input_bid_ceiling``: проверки + ядро."""
    found = _bid_plan(engine, buy)
    if found is None:
        return None
    plan, inputs, prices = found
    return run_ceiling(list(inputs.items()), buy['good'], inputs[buy['good']],
                       prices, engine.report['orders'], plan['input_orders'],
                       plan['expected_net_price'], plan['wage_unit'],
                       plan.get('fixed_unit', 0))


# --- reprice_input_order ---------------------------------------------------

def reprice_input_order_python(engine, buy, sells):
    """Эталон ``V3.reprice_input_order``: буквальный прежний код."""
    ceiling = engine.input_bid_ceiling(buy)
    if ceiling is None or buy['remaining'] <= EPS:
        return
    a = engine.accounts[buy['participant']]
    _, quote, _ = engine.quoted_cost(a, buy['good'], buy['remaining'], sells)
    budget = buy['reserved']+engine.free_cash(a['id'])
    price = min(quote, ceiling, budget/buy['remaining'])
    if price > buy['price']+EPS:
        extra = buy['remaining']*(price-buy['price'])
        engine.cash_store.add_reserved_cash(buy['participant'], extra)
        store = engine.order_store
        store.set_reserved(buy, buy['reserved']+extra)
        buy.setdefault('initial_input_limit', buy['price'])
        store.set_price(buy, price)
        store.set_metadata(buy, 'backing', buy['reserved'])
        engine._demand_revision[buy['good']] += 1


def reprice_input_order(engine, buy, sells):
    """Единственный вход из ``V3.reprice_input_order``: ядро + мутации."""
    ceiling = engine.input_bid_ceiling(buy)
    if ceiling is None or buy['remaining'] <= EPS:
        return
    a = engine.accounts[buy['participant']]
    _, quote, _ = engine.quoted_cost(a, buy['good'], buy['remaining'], sells)
    budget = buy['reserved']+engine.free_cash(a['id'])
    decision = run_reprice(quote, ceiling, budget, buy['remaining'], buy['price'], EPS)
    if decision is not None:
        price, extra = decision
        engine.cash_store.add_reserved_cash(buy['participant'], extra)
        store = engine.order_store
        store.set_reserved(buy, buy['reserved']+extra)
        buy.setdefault('initial_input_limit', buy['price'])
        store.set_price(buy, price)
        store.set_metadata(buy, 'backing', buy['reserved'])
        engine._demand_revision[buy['good']] += 1


def load_quote_kernel():
    """Вернуть native-модуль quote_kernel или None, если он не собран."""
    try:
        return importlib.import_module('.quote_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


# --- Python-эквиваленты ядра с теми же сигнатурами -------------------------

def _quoted_python(offers, quantity, reference_hook, recovery_rows, owner_id,
                   landed_hook, eps):
    """Python-эквивалент ``quoted_cost``: та же арифметика и тот же порядок."""
    left, value, ceiling = quantity, 0.0, 0.0
    for price, sell, _ in offers:
        q = min(left, sell['remaining'])
        value += q*price
        ceiling = max(ceiling, price)
        left -= q
        if left <= eps:
            break
    if left > eps:
        reference = reference_hook()
        quotes = [quote[0] for firm, price in recovery_rows
                  if firm['id'] != owner_id
                  and (quote := landed_hook(firm['tile_id'], price))]
        if quotes:
            reference = max(reference, min(quotes))
        value += left*reference
        ceiling = max(ceiling, reference)
    return value, max(ceiling, eps), left <= eps


def _reference_python(offers, signal_hook, default_price):
    """Python-эквивалент ``reference_price``."""
    if offers:
        return offers[0][0]
    return signal_hook() or default_price


def _sum_python(items, hook):
    """Python-эквивалент ``cost_sum``: встроенный ``sum`` над генератором."""
    return sum(ratio*hook(good) for good, ratio in items)


def _prepared_cost_sum_python(items):
    return sum(ratio*price for ratio, price in items)


def _volume_python(items, quantity, stock_hook, quoted_hook, reference_hook,
                   account, eps):
    """Python-эквивалент ``volume_input_prices``."""
    prices = {}
    for good, ratio in items:
        missing = max(0, quantity*ratio-stock_hook(good))
        if missing > eps:
            value, _, _ = quoted_hook(account, good, missing)
            prices[good] = value/missing
        else:
            prices[good] = reference_hook(good)
    return prices


def _volume_prepared_python(items, quantity, stock_values, quote_values,
                            reference_values, eps):
    prices = {}
    for good, ratio in items:
        missing = max(0, quantity*ratio-stock_values[good])
        prices[good] = (quote_values[good]/missing if missing > eps
                        else reference_values[good])
    return prices


def _ceiling_python(inputs, excluded_good, ratio, prices, orders,
                    input_order_ids, expected_net_price, wage_unit,
                    fixed_unit):
    """Python-эквивалент ``input_bid_ceiling``."""
    other_cost = wage_unit+fixed_unit
    for good, quantity in inputs:
        if good == excluded_good:
            continue
        committed = [o['price'] for o in orders
                     if o['id'] in input_order_ids and o['good'] == good]
        other_cost += quantity*max([prices[good]]+committed)
    return max(0, (expected_net_price-other_cost)/ratio)


def _reprice_python(quote, ceiling, budget, remaining, price, eps):
    """Python-эквивалент ``reprice_decision``."""
    new_price = min(quote, ceiling, budget/remaining)
    if new_price > price+eps:
        return new_price, remaining*(new_price-price)
    return None


NATIVE_KERNEL = False

_kernel = load_quote_kernel()
if _kernel is not None:
    run_quoted = _kernel.quoted_cost
    run_reference = _kernel.reference_price
    run_sum = _kernel.cost_sum
    run_volume = _kernel.volume_input_prices
    run_ceiling = _kernel.input_bid_ceiling
    run_reprice = _kernel.reprice_decision
    NATIVE_KERNEL = True
else:
    run_quoted = _quoted_python
    run_reference = _reference_python
    run_sum = _sum_python
    run_volume = _volume_python
    run_ceiling = _ceiling_python
    run_reprice = _reprice_python

run_prepared_cost_sum = getattr(_kernel, 'prepared_cost_sum', _prepared_cost_sum_python)
