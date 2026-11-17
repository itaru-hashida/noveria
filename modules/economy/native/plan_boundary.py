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

import importlib

from ..support import EPS


class PlanHooks(object):
    """Живой доступ ядра к движку: чтение чисел плюс прежние мутации.

    Хуки намеренно узкие и повторяют прежний порядок записи. Ни один из них
    не считает числа заново: ядро передаёт уже посчитанные значения, а
    граница фиксирует их там, где это делал прежний код.
    """

    __slots__ = ('engine', 'enterprise', 'sells')

    def __init__(self, engine, enterprise, sells):
        self.engine = engine
        self.enterprise = enterprise
        self.sells = sells

    # --- чтение чисел ------------------------------------------------------
    def reference_price(self, good):
        return self.engine.reference_price(self.engine.accounts[self.enterprise['id']],
                                           good, self.sells)

    def stock(self, good):
        return self.engine.free_stock(self.enterprise['id'], good)

    def quote(self, good, quantity):
        return self.engine.quoted_cost(self.engine.accounts[self.enterprise['id']],
                                       good, quantity, self.sells)

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
            self.enterprise['id'], account.pop('operating_protected', 0))

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
        account['payroll_protected'] = (plan['output']
                                        *self.engine.goods[e['good']]['labor']
                                        *self.engine.cfg['wage'])
        self.engine.cash_store.add_protected_cash(e['id'],
                                                  account['payroll_protected'])

    # --- заявки: единственная точка мутации -------------------------------
    def begin_orders(self):
        return len(self.engine.report['orders'])

    def order(self, good, quantity, price, purpose):
        return self.engine.order(self.enterprise['id'], good, quantity, 'buy',
                                 price, purpose)

    def mark_adaptive(self, order):
        order['adaptive_input_budget'] = True

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
    protected.release_protected_cash(e['id'], a.pop('operating_protected', 0))
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
    a['payroll_protected'] = target*recipe['labor']*engine.cfg['wage']
    protected.add_protected_cash(e['id'], a['payroll_protected'])
    for good, ratio in recipe['inputs'].items():
        quantity = max(0, target*ratio-engine.free_stock(e['id'], good))
        _, price, _ = engine.quoted_cost(a, good, quantity, sells)
        order = engine.order(e['id'], good, quantity, 'buy', price, 'Сырьё')
        if order:
            order['adaptive_input_budget'] = True
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
    """Единственный вход из ``V3.input_requests``: ядро поверх хуков."""
    return run_input_requests(engine, e, sells, PlanHooks(engine, e, sells), EPS)


def plan_enterprises(engine, existing, sells):
    """Единственный вход из ``V3.plan_enterprises``: ядро поверх движка.

    Перемешивание остаётся здесь: ``engine.shuffled`` — это RNG месяца, и
    порядок расхода случайных чисел не должен переехать в ядро.
    """
    return run_plan(engine, engine.shuffled(existing), sells, EPS)


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
                        equipment_rates, hooks):
    """Python-эквивалент ``cash_needed``."""
    needed = overhead+quantity*labor*wage
    for good, ratio in input_items:
        missing = max(0, quantity*ratio-hooks.stock(good))
        needed += missing*hooks.quote(good, missing)[1]
    for good, ratio, wear in equipment_rates:
        missing = max(0, ratio*quantity-hooks.stock(good))
        needed += missing*hooks.quote(good, missing)[1]
    return needed


def _target_python(overhead, labor, wage, target, cash, input_items,
                   equipment_rates, hooks):
    """Python-эквивалент ``plan_target``: те же 40 итераций."""
    if _cash_needed_python(overhead, labor, wage, target, input_items,
                           equipment_rates, hooks) > cash:
        low, high = 0.0, target
        for _ in range(40):
            middle = (low+high)/2
            if _cash_needed_python(overhead, labor, wage, middle, input_items,
                                   equipment_rates, hooks) <= cash:
                low = middle
            else:
                high = middle
        target = low
    return target


def _input_requests_python(engine, e, sells, hooks=None, eps=EPS):
    """Python-эквивалент ``input_requests`` с тем же порядком шагов."""
    hooks = hooks or PlanHooks(engine, e, sells)
    recipe = engine.goods[e['good']]
    input_items = list(recipe['inputs'].items())
    equipment_rates = [(good, ratio,
                        engine.goods[good].get('durable', {}).get('wear_rate', 0))
                       for good, ratio in recipe.get('equipment', {}).items()]
    for good, _ in input_items:
        hooks.reference_price(good)
    hooks.reset_protected()
    target = hooks.plan()
    cash = hooks.free_cash()
    requested = target
    first_order = hooks.begin_orders()
    target = _target_python(engine.enterprise_overhead(e), recipe['labor'],
                            engine.cfg['wage'], target, cash, input_items,
                            equipment_rates, hooks)
    input_prices = hooks.volume_prices(target)
    hooks.commit_target(target, requested, eps)
    equipment_required = {}
    equipment_wear = {}
    for good, ratio, wear in equipment_rates:
        equipment_required[good] = ratio*target
        equipment_wear[good] = ratio*target*wear
    plan = dict(month=engine.meta['month'], output=target,
                sale_price=e['planned_price'],
                expected_net_price=e['expected_price'], input_prices=input_prices,
                input_ratios=dict(recipe['inputs']),
                wage_unit=recipe['labor']*engine.cfg['wage'],
                equipment_required=equipment_required,
                equipment_wear=equipment_wear,
                fixed_unit=e.get('expected_fixed_unit', 0),
                requested_output=requested, free_cash=cash,
                known_order_count=first_order,
                reachable_demand=e['reachable_demand'],
                sales_forecast=e['sales_forecast'],
                expected_margin=e['expected_margin'],
                reason=e['production_limit'])
    hooks.commit_plan(plan, target)
    for good, ratio in input_items:
        quantity = max(0, target*ratio-hooks.stock(good))
        price = hooks.quote(good, quantity)[1]
        order = hooks.order(good, quantity, price, 'Сырьё')
        if order:
            hooks.mark_adaptive(order)
    for good, ratio, wear in equipment_rates:
        quantity = max(0, ratio*target-hooks.stock(good))
        price = hooks.quote(good, quantity)[1]
        hooks.order(good, quantity, price, 'Оборудование')
    hooks.commit_input_orders(hooks.order_ids(first_order))
    return plan


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
    run_input_requests = _kernel.input_requests
    run_plan = _kernel.plan_enterprises
    NATIVE_KERNEL = True
else:
    run_input_requests = _input_requests_python
    run_plan = _plan_enterprises_python
