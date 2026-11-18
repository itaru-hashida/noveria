"""Граница планирования закупок предприятий: эталон, fallback и один вход.

В Cython уходят расчётные срезы ``V3``:

  * штатный обход ``plan_enterprises`` (порядок по убыванию глубины товара,
    условия восстановления и планирования);
  * ``input_requests`` целиком — ``cash_needed``, 40 итераций бисекции по
    доступной кассе, потребности в сырье и оборудовании, резерв зарплат и
    числовые поля ``production_plan``.

Мутации остаются в Python-хуках границы и идут прежними путями:

  * ``self.order(...)`` — единственная точка создания заявок; деньги двигает
    ``cash_store``, склад — ``stock_store``, заявки — ``order_store``;
  * поля ``protected``/``payroll_protected`` счёта и поля предприятия
    (``planned_output``, ``input_target_output``, ``production_limit``,
    ``production_plan``) пишутся в том же порядке, что и раньше;
  * ``recover_enterprises(sells, [e])`` и ``self.plan(e, sells=sells)`` —
    прежние методы движка, ядро только решает, когда их звать.

Что остаётся Python по построению: RNG и перемешивание списка предприятий,
``quoted_cost``/``reference_price``/``volume_input_prices`` (книга, кэш,
``landed``), прогноз выпуска ``production_plan`` (свой кэш) и
``recover_enterprises`` (сделки, отчёт, закрытие). Ядро их не дублирует.

Эталонные ``*_python`` функции повторяют прежние тела ``V3`` буквально и
служат образцом для focused-тестов на побитовое совпадение, а также fallback,
если расширение не собрано. Имена привязываются один раз при импорте.
"""

import copy
import importlib
from collections import namedtuple

from ..support import EPS
from . import quote_boundary as _quote


PlanCalculationInputs = namedtuple(
    'PlanCalculationInputs',
    'month input_items input_ratios equipment_rates stock_values labor wage '
    'overhead quote_contexts requested sale_price expected_net_price '
    'reachable_demand sales_forecast expected_margin production_limit fixed_unit '
    'cash first_order target input_prices')
PlanCalculationResult = namedtuple('PlanCalculationResult', 'plan orders')


class PlanHooks(object):
    """Живой доступ ядра к движку: чтение чисел плюс прежние мутации.

    Хуки намеренно узкие и повторяют прежний порядок записи. Ни один из них
    не считает числа заново: ядро передаёт уже посчитанные значения, а
    граница фиксирует их там, где это делал прежний код.
    """

    __slots__ = ('engine', 'enterprise', 'sells', '_reference_cache',
                 '_stock_cache', '_quote_cache', '_quote_contexts')

    def __init__(self, engine, enterprise, sells):
        self.engine = engine
        self.enterprise = enterprise
        self.sells = sells
        self._reference_cache = {}
        self._stock_cache = {}
        self._quote_cache = {}
        self._quote_contexts = {}

    # --- чтение чисел ------------------------------------------------------
    def reference_price(self, good):
        value = self._reference_cache.get(good)
        if value is None:
            value = self.engine.reference_price(
                self.engine.accounts[self.enterprise['id']], good, self.sells)
            self._reference_cache[good] = value
        return value

    def stock(self, good):
        value = self._stock_cache.get(good)
        if value is None:
            value = self.engine.free_stock(self.enterprise['id'], good)
            self._stock_cache[good] = value
        return value

    def quote(self, good, quantity):
        key = (good, quantity)
        value = self._quote_cache.get(key)
        if value is None:
            context = self._quote_contexts.get(good)
            if context is None:
                context = _quote.prepare_quote_context(
                    self.engine, self.engine.accounts[self.enterprise['id']],
                    good, self.sells)
                self._quote_contexts[good] = context
            value = _quote.quoted_cost_prepared(context, quantity)
            self._quote_cache[key] = value
        return value

    def free_cash(self):
        return self.engine.free_cash(self.enterprise['id'])

    def plan(self):
        return self.engine.plan(self.enterprise, sells=self.sells)

    def volume_prices(self, target):
        return self.engine.volume_input_prices(self.enterprise, target, self.sells)

    # --- мутации счёта и предприятия --------------------------------------
    def reset_protected(self):
        """Снять прошлый ``operating_protected``: прежний ``max(0, ...)``."""
        account = self.engine.accounts[self.enterprise['id']]
        self.engine.cash_store.release_protected_cash(
            self.enterprise['id'],
            self.engine.cash_store.release_operating_protected(
                self.enterprise['id']))

    def commit_target(self, target, requested, eps):
        """Записать итоговый выпуск и причину предела до ``production_plan``.

        Прежний порядок: ``planned_output``/``input_target_output``, затем
        ``production_limit`` при урезании, и только потом ``production_plan``,
        в который попадает уже выставленная причина.
        """
        e = self.enterprise
        e['planned_output'] = e['input_target_output'] = target
        if target+eps < requested:
            e['production_limit'] = 'Деньги на выпуск'

    def commit_plan(self, plan, target):
        """Записать ``production_plan`` и резерв зарплат в прежнем порядке.

        ``payroll_protected`` и прибавка к ``protected`` идут после плана: этот
        резерв читает ``free_cash`` следующих предприятий. Прибавку к
        ``protected`` делает ``CashStore``: это денежная колонка счёта.
        """
        e = self.enterprise
        e['production_plan'] = plan
        account = self.engine.accounts[e['id']]
        payroll = (plan['output']
                   *self.engine.goods[e['good']]['labor']
                   *self.engine.cfg['wage'])
        self.engine.cash_store.set_payroll_protected(e['id'], payroll)
        self.engine.cash_store.add_protected_cash(e['id'],
                                                  payroll)

    # --- заявки: единственная точка мутации -------------------------------
    def begin_orders(self):
        return len(self.engine.report['orders'])

    def order(self, good, quantity, price, purpose):
        return self.engine.order(self.enterprise['id'], good, quantity, 'buy',
                                 price, purpose)

    def mark_adaptive(self, order):
        self.engine.order_store.set_metadata(order, 'adaptive_input_budget', True)

    def order_ids(self, first_order):
        return [o['id'] for o in self.engine.report['orders'][first_order:]]

    def commit_input_orders(self, ids):
        self.enterprise['production_plan']['input_orders'] = ids


# ---------------------------------------------------------------------------
# Эталон input_requests
# ---------------------------------------------------------------------------

def input_requests_python(engine, e, sells):
    """Эталон ``V3.input_requests``: буквальный прежний код.

    Денежные поля пишет владелец: ``protected`` — колонка ``CashStore``,
    поэтому снятие прошлого ``operating_protected`` и прибавка резерва зарплат
    идут через его методы с той же арифметикой и в том же порядке.
    """
    a = engine.accounts[e['id']]
    protected = engine.cash_store
    input_prices = {good: engine.reference_price(a, good, sells)
        for good in engine.goods[e['good']]['inputs']}
    protected.release_protected_cash(
        e['id'], protected.release_operating_protected(e['id']))
    target = engine.plan(e, sells=sells)
    a, recipe = engine.accounts[e['id']], engine.goods[e['good']]
    cash = engine.free_cash(e['id'])
    requested = target
    first_order = len(engine.report['orders'])

    def cash_needed(quantity):
        needed = engine.enterprise_overhead(e)+quantity*recipe['labor']*engine.cfg['wage']
        for good, ratio in recipe['inputs'].items():
            missing = max(0, quantity*ratio-engine.free_stock(e['id'], good))
            needed += missing*engine.quoted_cost(a, good, missing, sells)[1]
        for good, required in engine.equipment_requirements(recipe, quantity).items():
            missing = max(0, required-engine.free_stock(e['id'], good))
            needed += missing*engine.quoted_cost(a, good, missing, sells)[1]
        return needed

    # Оплаченное ранее сырьё влияет на маржу, но не требует второй оплаты из кассы.
    if cash_needed(target) > cash:
        low, high = 0.0, target
        for _ in range(40):
            middle = (low+high)/2
            if cash_needed(middle) <= cash:
                low = middle
            else:
                high = middle
        target = low
    input_prices = engine.volume_input_prices(e, target, sells)
    # Единственный окончательный план: выпуск не сможет увеличить его после закупок.
    e['planned_output'] = e['input_target_output'] = target
    if target+EPS < requested:
        e['production_limit'] = 'Деньги на выпуск'
    e['production_plan'] = dict(month=engine.meta['month'], output=target,
        sale_price=e['planned_price'],
        expected_net_price=e['expected_price'], input_prices=input_prices,
        input_ratios=dict(recipe['inputs']), wage_unit=recipe['labor']*engine.cfg['wage'],
        equipment_required=engine.equipment_requirements(recipe, target),
        equipment_wear=engine.equipment_wear(recipe, target),
        fixed_unit=e.get('expected_fixed_unit', 0),
        requested_output=requested, free_cash=cash, known_order_count=first_order,
        reachable_demand=e['reachable_demand'], sales_forecast=e['sales_forecast'],
        expected_margin=e['expected_margin'], reason=e['production_limit'])
    payroll = target*recipe['labor']*engine.cfg['wage']
    protected.set_payroll_protected(e['id'], payroll)
    protected.add_protected_cash(e['id'], payroll)
    for good, ratio in recipe['inputs'].items():
        quantity = max(0, target*ratio-engine.free_stock(e['id'], good))
        _, price, _ = engine.quoted_cost(a, good, quantity, sells)
        order = engine.order(e['id'], good, quantity, 'buy', price, 'Сырьё')
        if order:
                    engine.order_store.set_metadata(order, 'adaptive_input_budget', True)
    for good, required in engine.equipment_requirements(recipe, target).items():
        quantity = max(0, required-engine.free_stock(e['id'], good))
        _, price, _ = engine.quoted_cost(a, good, quantity, sells)
        engine.order(e['id'], good, quantity, 'buy', price, 'Оборудование')
    e['production_plan']['input_orders'] = [o['id'] for o in engine.report['orders'][first_order:]]


def plan_enterprises_python(engine, existing, sells):
    """Эталон ``V3.plan_enterprises``: буквальный прежний код."""
    for e in sorted(engine.shuffled(existing), key=lambda e: -engine.depth(e['good'])):
        a = engine.accounts[e['id']]
        if e['status'] == 'closed' or (engine.free_cash(e['id']) <= EPS
                and a.get('operating_protected', 0) <= EPS):
            # Спрос на входы появляется после планирования следующего звена цепочки.
            engine.recover_enterprises(sells, [e])
        if e['status'] != 'closed' and not e.get('suspended') and e['available_from'] <= engine.meta['month']:
            engine.input_requests(e, sells)


# ---------------------------------------------------------------------------
# Точки входа
# ---------------------------------------------------------------------------

def input_requests(engine, e, sells):
    """Единственный вход из ``V3.input_requests``: snapshot → расчёт → apply."""
    hooks = PlanHooks(engine, e, sells)
    recipe = engine.goods[e['good']]
    account = engine.accounts[e['id']]
    previous_full = copy.deepcopy(e)
    previous_account = copy.deepcopy(account)
    previous_demand = copy.deepcopy(getattr(engine, '_demand_revision', {}))
    protected = engine.cash_store.get_protected_cash(e['id'])
    try:
        snapshot, result = _calculate_input_requests(engine, e, sells, hooks, recipe)
    except Exception:
        engine.cash_store.set_protected_cash(e['id'], protected)
        account.clear()
        account.update(previous_account)
        e.clear()
        e.update(previous_full)
        engine._demand_revision.clear()
        engine._demand_revision.update(previous_demand)
        raise
    return apply_input_request_result(
        hooks, snapshot, result, EPS, previous_full, previous_account,
        previous_demand)


def _calculate_input_requests(engine, e, sells, hooks, recipe):
    hooks.reset_protected()
    requested = hooks.plan()
    cash = hooks.free_cash()
    first_order = hooks.begin_orders()
    snapshot = {
        'month': engine.meta['month'],
        'input_items': list(recipe['inputs'].items()),
        'input_ratios': dict(recipe['inputs']),
        'equipment_rates': [
            (good, ratio,
             engine.goods[good].get('durable', {}).get('wear_rate', 0))
            for good, ratio in recipe.get('equipment', {}).items()
        ],
        'stock_values': {
            good: hooks.stock(good)
            for good in list(recipe['inputs']) + [
                good for good in recipe.get('equipment', {})
                if good not in recipe['inputs']
            ]
        },
        'labor': recipe['labor'],
        'wage': engine.cfg['wage'],
        'overhead': engine.enterprise_overhead(e),
    }
    maximum_missing = {}
    for good, ratio in (*snapshot['input_items'],
                        *((good, ratio) for good, ratio, _ in snapshot['equipment_rates'])):
        missing = max(0, requested*ratio-snapshot['stock_values'][good])
        maximum_missing[good] = max(maximum_missing.get(good, 0), missing)
    snapshot['quote_contexts'] = {
        good: _quote.prepare_native_quote_context(
            engine, engine.accounts[e['id']], good, sells, missing,
            needs_reference=good in recipe['inputs'])
        for good, missing in maximum_missing.items()
    }
    snapshot['requested'] = requested
    snapshot.update(
        sale_price=e['planned_price'], expected_net_price=e['expected_price'],
        reachable_demand=e['reachable_demand'], sales_forecast=e['sales_forecast'],
        expected_margin=e['expected_margin'], production_limit=e['production_limit'],
        fixed_unit=e.get('expected_fixed_unit', 0),
    )
    snapshot['cash'] = cash
    snapshot['first_order'] = first_order
    snapshot = PlanCalculationInputs(**snapshot, target=None, input_prices={})
    snapshot = snapshot._replace(
        target=run_target(snapshot, snapshot.cash, snapshot.requested, EPS))
    input_prices = {}
    for good, ratio in snapshot.input_items:
        missing = max(0, snapshot.target*ratio-
                      snapshot.stock_values[good])
        if missing > EPS:
            input_prices[good] = _quote.quoted_cost_native_prepared(
                snapshot.quote_contexts[good], missing)[0]/missing
        else:
            input_prices[good] = snapshot.quote_contexts[good][1]
    snapshot = snapshot._replace(input_prices=input_prices)
    result = run_input_requests(snapshot, EPS)
    return snapshot, result


def apply_input_request_result(hooks, snapshot, result, eps=EPS,
                               enterprise_before=None, account_before=None,
                               demand_before=None):
    """Применить immutable planning result через владельцев состояния."""
    plan, orders = result.plan, result.orders
    target = snapshot.target
    engine = hooks.engine
    enterprise = hooks.enterprise
    account = engine.accounts[enterprise['id']]
    enterprise_before = (copy.deepcopy(enterprise)
                         if enterprise_before is None else enterprise_before)
    account_before = (copy.deepcopy(account)
                      if account_before is None else account_before)
    demand_before = (copy.deepcopy(getattr(engine, '_demand_revision', {}))
                     if demand_before is None else demand_before)
    created = []
    try:
        hooks.commit_target(target, snapshot.requested, eps)
        plan['reason'] = enterprise['production_limit']
        hooks.commit_plan(plan, target)
        for good, quantity, price, purpose, adaptive in orders:
            order = hooks.order(good, quantity, price, purpose)
            if order:
                created.append(order)
                if adaptive:
                    hooks.mark_adaptive(order)
        hooks.commit_input_orders(hooks.order_ids(snapshot.first_order))
    except Exception:
        for order in reversed(created):
            if order['status'] not in ('cancelled', 'expired'):
                engine.cancel_order(order, 'Откат неудачного планирования')
            engine.order_store.remove(order)
        enterprise.clear()
        enterprise.update(enterprise_before)
        account.clear()
        account.update(account_before)
        engine._demand_revision.clear()
        engine._demand_revision.update(demand_before)
        engine.cash_store.set_reserved_cash(
            enterprise['id'], account_before.get('reserved', 0))
        engine.cash_store.set_protected_cash(
            enterprise['id'], account_before.get('protected', 0))
        raise
    return plan


def plan_enterprises(engine, existing, sells):
    """Единственный вход из ``V3.plan_enterprises``: ядро поверх движка.

    Перемешивание остаётся здесь: ``engine.shuffled`` — это RNG месяца, и
    порядок расхода случайных чисел не должен переехать в ядро.
    """
    # Orchestration остаётся у владельца engine: здесь сохраняются RNG,
    # recovery, порядок предприятий и переход к следующему snapshot.
    return _plan_enterprises_python(engine, engine.shuffled(existing), sells, EPS)


def load_plan_kernel():
    """Вернуть native-модуль plan_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — ядро не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.plan_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


# --- Python-эквиваленты ядра с теми же сигнатурами -------------------------

def _cash_needed_python(overhead, labor, wage, quantity, input_items,
                        equipment_rates, stock_values, quote_contexts, hooks):
    """Python-эквивалент ``cash_needed``."""
    needed = overhead+quantity*labor*wage
    for good, ratio in input_items:
        missing = max(0, quantity*ratio-stock_values[good])
        needed += missing*_quote.quoted_cost_native_prepared(
            quote_contexts[good], missing)[1]
    for good, ratio, wear in equipment_rates:
        missing = max(0, ratio*quantity-stock_values[good])
        needed += missing*_quote.quoted_cost_native_prepared(
            quote_contexts[good], missing)[1]
    return needed


def _target_python(overhead, labor, wage, target, cash, input_items,
                   equipment_rates, stock_values, quote_contexts, hooks):
    """Python-эквивалент ``plan_target``: те же 40 итераций."""
    if _cash_needed_python(overhead, labor, wage, target, input_items,
                           equipment_rates, stock_values, quote_contexts, hooks) > cash:
        low, high = 0.0, target
        for _ in range(40):
            middle = (low+high)/2
            if _cash_needed_python(overhead, labor, wage, middle, input_items,
                                   equipment_rates, stock_values, quote_contexts, hooks) <= cash:
                low = middle
            else:
                high = middle
        target = low
    return target


def _input_requests_python(snapshot, eps=EPS):
    """Python-эквивалент ``input_requests`` с тем же порядком шагов."""
    input_items = snapshot.input_items
    equipment_rates = snapshot.equipment_rates
    stock_values = snapshot.stock_values
    quote_contexts = snapshot.quote_contexts
    target = snapshot.target
    cash = snapshot.cash
    requested = snapshot.requested
    first_order = snapshot.first_order
    input_prices = snapshot.input_prices
    equipment_required = {}
    equipment_wear = {}
    for good, ratio, wear in equipment_rates:
        equipment_required[good] = ratio*target
        equipment_wear[good] = ratio*target*wear
    plan = dict(month=snapshot.month, output=target,
                sale_price=snapshot.sale_price,
                expected_net_price=snapshot.expected_net_price, input_prices=input_prices,
                input_ratios=dict(snapshot.input_ratios),
                wage_unit=snapshot.labor*snapshot.wage,
                equipment_required=equipment_required,
                equipment_wear=equipment_wear,
                fixed_unit=snapshot.fixed_unit,
                requested_output=requested, free_cash=cash,
                known_order_count=first_order,
                reachable_demand=snapshot.reachable_demand,
                sales_forecast=snapshot.sales_forecast,
                expected_margin=snapshot.expected_margin,
                reason=snapshot.production_limit)
    orders = []
    for good, ratio in input_items:
        quantity = max(0, target*ratio-stock_values[good])
        price = _quote.quoted_cost_native_prepared(
            quote_contexts[good], quantity)[1]
        orders.append((good, quantity, price, 'Сырьё', True))
    for good, ratio, wear in equipment_rates:
        quantity = max(0, ratio*target-stock_values[good])
        price = _quote.quoted_cost_native_prepared(
            quote_contexts[good], quantity)[1]
        orders.append((good, quantity, price, 'Оборудование', False))
    return PlanCalculationResult(plan, orders)


def _plan_enterprises_python(engine, enterprises, sells, eps):
    """Python-эквивалент ``plan_enterprises`` с прежним порядком обхода."""
    for _, _, enterprise in sorted(
            [(-engine.depth(e['good']), index, e)
             for index, e in enumerate(enterprises)]):
        a = engine.accounts[enterprise['id']]
        if enterprise['status'] == 'closed' or (
                engine.free_cash(enterprise['id']) <= eps
                and a.get('operating_protected', 0) <= eps):
            engine.recover_enterprises(sells, [enterprise])
        if (enterprise['status'] != 'closed' and not enterprise.get('suspended')
                and enterprise['available_from'] <= engine.meta['month']):
            engine.input_requests(enterprise, sells)
    return None


NATIVE_KERNEL = False

_kernel = load_plan_kernel()
if _kernel is not None:
    def run_input_requests(snapshot, eps):
        return PlanCalculationResult(*_kernel.input_requests(snapshot, eps))
    run_target = _kernel.calculate_target
    NATIVE_KERNEL = True
else:
    run_input_requests = _input_requests_python
    run_target = lambda snapshot, cash, target, eps: _target_python(
        snapshot.overhead, snapshot.labor, snapshot.wage, target, cash,
        snapshot.input_items, snapshot.equipment_rates,
        snapshot.stock_values, snapshot.quote_contexts, None)
