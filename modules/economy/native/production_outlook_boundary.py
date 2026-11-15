"""Граница прогноза выпуска: эталон, fallback и один native call site.

В Cython уходит только числовая часть ``V3.production_outlook``: средние
продажи по трём последним наблюдениям, прогноз спроса, остаток склада после
торгов, постепенное сокращение мощности, ``fixed_unit``, маржа, причина
предела и итоговый выпуск. Порядок ключей результата тот же.

Что остаётся Python-хуками и приходит в ядро готовым:

  * ``unit_cost`` — себестоимость: ``reference_price``/``equipment_wear``
    ходят в книгу заявок и в кэш движка;
  * ``net_price`` — цена с налогом потребления: это существующая граница
    ``net_price_boundary``;
  * ``reachable_demand`` — достижимый спрос: читает ``sells`` и заявки;
  * ``expected_stock_sales`` — продажа старого склада: свой кэш движка
    ``_stock_sales_cache``;
  * ``enterprise_overhead`` — оброк за площадку: остаётся хуком внутри ядра
    и вызывается ровно дважды, как в прежнем коде;
  * ``report['trades']`` и ``accounts[...]['stock']`` — чтение живого
    состояния месяца;
  * кэш прогноза ``_production_outlook_cache`` и его ключ.

Живые заявки, ``offers``, ``market_book``, matching, отчёт, RNG и
инвалидация кэша не переносятся: ядро их не трогает.

Ключ кэша собирается здесь, а не в ядре, и дополнительно защищён
``VERSION``: при несовпадении версии записи кэша не переиспользуются, даже
если ключ совпал по форме. Это защита от старого формата записи после
обновления границы.
"""

import importlib

from ..support import EPS


# Версия формы ключа и значения кэша прогноза. Меняется вместе с составом
# ключа или формы результата; записи другой версии не переиспользуются.
VERSION = 1


class OutlookHooks(object):
    """Python-хуки, которые обязаны остаться в Python.

    Ни один из них не считает прогноз: они отдают уже готовые числа либо
    читают живое состояние месяца. Порядок вызовов задаёт ядро.
    """

    __slots__ = ('engine', 'e', 'sells')

    def __init__(self, engine, e, sells):
        self.engine = engine
        self.e = e
        self.sells = sells

    def unit_cost(self):
        """Себестоимость единицы — прежний ``self.unit_cost(e, sells)``."""
        return self.engine.unit_cost(self.e, self.sells)

    def net_price(self, item):
        """Цена с налогом потребления — прежний ``self.net_price(item)``."""
        return self.engine.net_price(item)

    def reachable_demand(self, quote):
        """Достижимый спрос — прежний ``self.reachable_demand(quote, sells)``."""
        return self.engine.reachable_demand(quote, self.sells)

    def expected_stock_sales(self, demand):
        """Продажа старого склада — прежний ``self.expected_stock_sales(...)``."""
        return self.engine.expected_stock_sales(self.e, self.sells, demand)

    def overhead(self):
        """Оброк за площадку — прежний ``self.enterprise_overhead(e)``."""
        return self.engine.enterprise_overhead(self.e)

    def trades(self):
        """Сделки месяца — прежний ``self.report['trades']``."""
        return self.engine.report['trades']

    def stock(self, good):
        """Склад счёта предприятия — прежний ``self.accounts[...]['stock']``."""
        return self.engine.accounts[self.e['id']]['stock'].get(good, 0)

    def observations(self):
        """Три последних наблюдения продаж — прежний срез ``sales_history``."""
        return self.e.get('sales_history', [])[-3:]


def cache_key(engine, e, sells, hooks):
    """Ключ кэша прогноза: та же форма, что была в V3, плюс ``VERSION``.

    ``cacheable`` проверяется вызывающим: прогноз по чужому словарю не
    кэшируется, как и раньше. Версия стоит первым элементом, чтобы запись
    прежней формы не совпала с новой.
    """
    history = tuple((row.get('sold', 0), row.get('output', 0),
                     bool(row.get('input_limited')))
                    for row in e.get('sales_history', [])[-3:])
    return (VERSION, id(sells), e['id'], e['tile_id'], e['good'], e['price'],
            e.get('production_price'), e['capacity'], e.get('planned_output', 0),
            hooks.stock(e['good']), history,
            engine._demand_revision[e['good']], engine._outlook_quote_versions(e),
            engine._trades_by_seller[e['id']])


def production_outlook_python(engine, e, sells=None, hooks=None):
    """Эталон числовой части — буквальный прежний код ``V3.production_outlook``.

    Кэш, ``sells`` по умолчанию и копия словаря предприятия ведут себя как
    раньше; результат — прежний словарь из одиннадцати ключей.
    """
    from collections import defaultdict
    sells = sells or defaultdict(list)
    e = dict(e)
    hooks = hooks or OutlookHooks(engine, e, sells)
    cost = hooks.unit_cost()
    # Рентабельную текущую цену (включая B2B без налога) повышать не требуется.
    base_net = hooks.net_price(e)
    if base_net+EPS >= cost:
        price = e['price']
    else:
        price = e.get('production_price', e['price'])
    quote = dict(e, price=price)
    observations = hooks.observations()
    own = (sum(h['sold'] for h in observations)/len(observations)
           if observations else e['sales_forecast'])
    demand = hooks.reachable_demand(quote)
    sold_now = sum(t['quantity'] for t in hooks.trades()
                   if t['seller'] == e['id'] and t['good'] == e['good'])
    forecast = max(own, sold_now+demand)
    stock = hooks.stock(e['good'])
    expected_sales = (hooks.expected_stock_sales(demand if price == e['price']
                                                 else None)
                      if engine.STOCK_PLANNING_REVISION else 0.0)
    projected_stock = max(0, stock-expected_sales)
    target = max(0, min(e['capacity'], forecast-projected_stock))
    previous = e.get('planned_output', 0)
    if (engine.STOCK_PLANNING_REVISION >= 2 and target < previous-EPS
            and not (stock > forecast*engine.cfg['output_overstock_factor'])):
        target = previous-engine.cfg['output_decline_rate']*(previous-target)
        target = max(0, min(e['capacity'], target))
    fixed_unit = hooks.overhead()/target if target > EPS else 0.0
    expected_price = base_net if price == e['price'] else hooks.net_price(quote)
    margin = expected_price-cost-fixed_unit
    if projected_stock >= forecast and forecast > EPS:
        limit = 'Готовые запасы'
    elif forecast <= EPS:
        limit = 'Нет обеспеченного спроса'
    else:
        limit = 'План'
    if margin < -EPS:
        target, limit = 0, 'Отрицательная ожидаемая маржа'
    return {'sales_forecast': forecast, 'reachable_demand': demand,
            'expected_unit_cost': cost, 'expected_price': expected_price,
            'expected_margin': margin, 'production_limit': limit,
            'planned_output': target, 'input_target_output': target,
            'planned_price': price, 'expected_fixed_cost': hooks.overhead(),
            'expected_fixed_unit': fixed_unit}


def production_outlook(engine, e, sells=None):
    """Единственный вход из ``V3.production_outlook``.

    Кэш, его ключ и копия предприятия остаются здесь: ядро получает только
    числа и хуки. Hit возвращает ту же запись, что и прежде; miss считает,
    пишет запись и возвращает результат.
    """
    from collections import defaultdict
    sells = sells or defaultdict(list)
    cache = getattr(engine, '_production_outlook_cache', {})
    cacheable = engine.enterprises.get(e.get('id')) is e
    key = None
    if cacheable:
        key = cache_key(engine, e, sells, OutlookHooks(engine, e, sells))
        cached = cache.get(key)
        if cached is not None:
            return cached
    # Предприятие копируется до расчёта: прежний код работал с копией, и
    # внешний словарь не должен увидеть промежуточные поля.
    copy_e = dict(e)
    hooks = OutlookHooks(engine, copy_e, sells)
    result = _compute(engine, copy_e, sells, hooks, key is not None)
    if key is not None:
        cache[key] = result
        engine._production_outlook_cache = cache
    return result


def _compute(engine, e, sells, hooks, cacheable):
    """Единственный путь расчёта: хуки зовёт граница, числа — ``run_*``.

    При собранном ядре ``run_price``/``run_sold``/``run_outlook`` — Cython, при
    отсутствии ``.pyd`` — Python-эквиваленты с той же сигнатурой. Поэтому
    fallback не отдельная ветка кода, а другая привязка тех же трёх имён, и
    разойтись с ядром по порядку вызовов он не может.
    """
    cost = hooks.unit_cost()
    base_net = hooks.net_price(e)
    price = run_price(e['price'], base_net, cost,
                      e.get('production_price', e['price']), EPS)
    quote = dict(e, price=price)
    demand = hooks.reachable_demand(quote)
    sold_now = run_sold(hooks.trades(), e['id'], e['good'])
    # Короткое замыкание прежнего выражения: при выключенной ревизии склада
    # хук не вызывается вовсе, ядро получает None и подставляет 0.0.
    if engine.STOCK_PLANNING_REVISION:
        expected_sales = hooks.expected_stock_sales(
            demand if price == e['price'] else None)
    else:
        expected_sales = None
    return run_outlook(hooks.observations(), e['sales_forecast'], sold_now,
                       demand, expected_sales, engine.STOCK_PLANNING_REVISION,
                       hooks.stock(e['good']), e['capacity'],
                       e.get('planned_output', 0), cost,
                       (base_net if price == e['price']
                        else hooks.net_price(quote)),
                       hooks.overhead, EPS,
                       engine.cfg['output_overstock_factor'],
                       engine.cfg['output_decline_rate'], price)


def load_production_outlook_kernel():
    """Вернуть native-модуль или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.production_outlook_kernel',
                                       __package__)
    except (ImportError, ModuleNotFoundError):
        return None


def _price_python(price, base_net, cost, production_price, eps):
    """Python-эквивалент ``select_price`` с той же сигнатурой."""
    if base_net+eps >= cost:
        return price
    return production_price


def _sold_python(trades, seller, good):
    """Python-эквивалент ``sold_quantity``: прежний встроенный ``sum``."""
    return sum(row['quantity'] for row in trades
               if row['seller'] == seller and row['good'] == good)


def _outlook_python(observations, sales_forecast, sold_now, demand,
                    expected_sales, stock_planning, stock, capacity, previous,
                    cost, expected_price, overhead_hook, eps, overstock_factor,
                    decline_rate, price):
    """Python-эквивалент ``outlook`` с той же сигнатурой."""
    if observations:
        own = sum(row['sold'] for row in observations)/len(observations)
    else:
        own = sales_forecast
    forecast = max(own, sold_now+demand)
    if not stock_planning:
        expected_sales = 0.0
    projected_stock = max(0, stock-expected_sales)
    target = max(0, min(capacity, forecast-projected_stock))
    if (stock_planning >= 2 and target < previous-eps
            and not (stock > forecast*overstock_factor)):
        target = previous-decline_rate*(previous-target)
        target = max(0, min(capacity, target))
    fixed_unit = overhead_hook()/target if target > eps else 0.0
    overhead_cost = overhead_hook()
    margin = expected_price-cost-fixed_unit
    if projected_stock >= forecast and forecast > eps:
        limit = 'Готовые запасы'
    elif forecast <= eps:
        limit = 'Нет обеспеченного спроса'
    else:
        limit = 'План'
    if margin < -eps:
        target, limit = 0, 'Отрицательная ожидаемая маржа'
    return {'sales_forecast': forecast, 'reachable_demand': demand,
            'expected_unit_cost': cost, 'expected_price': expected_price,
            'expected_margin': margin, 'production_limit': limit,
            'planned_output': target, 'input_target_output': target,
            'planned_price': price, 'expected_fixed_cost': overhead_cost,
            'expected_fixed_unit': fixed_unit}


NATIVE_KERNEL = False

_kernel = load_production_outlook_kernel()
if _kernel is not None:
    run_price = _kernel.select_price
    run_sold = _kernel.sold_quantity
    run_outlook = _kernel.outlook
    NATIVE_KERNEL = True
else:
    run_price = _price_python
    run_sold = _sold_python
    run_outlook = _outlook_python
