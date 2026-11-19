"""Проверка готовности проекта и последовательное применение завершения."""

import importlib

from . import cash_boundary, project_slot

# EPS проекта. Импорт из support безопасен: support не импортирует эту
# границу, поэтому цикла support -> boundary -> support не возникает.
from ..support import EPS


CAPACITY_LIMIT_REASON = 'Устойчивый поток входов'


class ProjectCompletionHooks(object):
    """Side effects завершения проекта: единственные точки мутаций вне чисел.

    Хуки намеренно узкие и повторяют прежний порядок записи. Ни один из них
    не считает числа: ядро передаёт уже посчитанные значения.
    """

    __slots__ = ('engine',)

    def __init__(self, engine):
        self.engine = engine

    # --- чтение, которое ядру нужно для чисел -----------------------------
    def construction_bill(self, capacity):
        return self.engine.construction_bill(capacity)

    def free_cash(self, account_id):
        return self.engine.free_cash(account_id)

    def startup_cash(self):
        return self.engine.cfg['startup_cash']

    def recipe_requirements(self, good, capacity):
        return self.engine.recipe_requirements(good, capacity)

    # --- завершение проекта: прежние методы движка -------------------------
    def pending(self, a, p):
        """Отчёт о незавершённом проекте: прежний report append."""
        import copy
        self.engine.report['investment_decisions'].append(
            dict(kind='investment_decision', investor=a['id'], outcome='pending',
                 **copy.deepcopy(p)))

    def detach_project(self, a):
        """Снятие проекта со счёта и сброс защиты денег."""
        project_slot.detach(a)
        cash_boundary.store_for(self.engine, a).set_protected_cash(a['id'], 0)

    def pay_delivery(self, a, p):
        self.engine.transfer(a['id'], 'carrier:'+a['state_id'],
            p.get('delivery', 0), 'Доставка строительных материалов на площадку')

    def use_stock(self, account_id, good, quantity):
        """Списание материала со склада: прежний ``use_stock`` движка."""
        return self.engine.use_stock(account_id, good, quantity)

    def construction_used(self, good, quantity):
        report = self.engine.report['construction_used']
        report[good] = report.get(good, 0)+quantity

    def expanded(self, e, p):
        """Отчёт и dirty-поле расширения: прежние side effects."""
        self.engine.mark_investment_dirty(e['good'])
        self.engine.report['lifecycle'].append(dict(enterprise=e['id'],
            action='expanded', good=e['good'], capacity_added=p['capacity'],
            reason='Оплаченное выгодное расширение'))

    def built(self, a, p, bill, asset):
        """Новое предприятие: квитанция, open_enterprise и прежняя цена."""
        engine = self.engine
        loc = p['location']
        key = (a['id'], loc['province_id'], loc['tile_id'], p['capacity'])
        engine.construction_receipts[key] = dict(working_cash=p['working_cash'],
            materials=bill, asset_value=asset)
        e = engine.open_enterprise(p['good'], loc, a['id'], p['capacity'],
            sales_estimate=p['capacity'])
        e['price'] = p['expected_price']
        return e

    def decision(self, a, p, e):
        self.engine.report['investment_decisions'].append(
            dict(kind='investment_decision', investor=a['id'],
                 outcome='expanded' if p['expansion'] else 'built',
                 enterprise=e['id']))


def materials_ready_python(bill, materials, eps):
    return not any(materials.get(g, 0)+eps < q for g, q in bill.items())


def cash_ready_python(free, protected, needed, eps):
    return not (free+protected+eps < needed)


def _run_finish(engine, accounts, hooks, eps, has_materials, has_cash):
    """Проверить готовность и применить завершение проекта."""
    for a in accounts:
        p = a.get('project')
        if not p:
            continue
        bill = hooks.construction_bill(p['capacity'])
        need_cash = p['working_cash']+p.get('delivery', 0)
        if (bill and not has_materials(bill, p['materials'], eps)) \
                or not has_cash(hooks.free_cash(a['id']), a.get('protected', 0), need_cash, eps):
            hooks.pending(a, p)
            continue
        hooks.detach_project(a)
        hooks.pay_delivery(a, p)
        asset = p.get('delivery', 0)
        for g, q in bill.items():
            asset += hooks.use_stock(a['id'], g, q)
            hooks.construction_used(g, q)
        if p['expansion']:
            e = engine.enterprises[a['id']]
            e['capacity'] += p['capacity']
            e['asset_value'] += asset
            hooks.expanded(e, p)
        else:
            e = hooks.built(a, p, bill, asset)
        hooks.decision(a, p, e)
    return None


def capacity_values_python(previous, capacity, profit, construction, working):
    return round(previous, 12), profit*capacity/previous, construction*capacity/previous+working


def _limit_capacity(project, capacity, eps, hooks, reason, calculate):
    previous = project['capacity']
    if capacity+eps >= previous:
        return False
    previous_working = project['working_cash']
    construction = max(0, project['expected_investment']-previous_working)
    working = 0 if project['expansion'] else max(hooks.startup_cash(),
        capacity*project['expected_unit_cost'])
    requested, profit, investment = calculate(
        previous, capacity, project['expected_profit'], construction, working)
    project.update(requested_capacity=requested, capacity=capacity,
        working_cash=working,
        expected_profit=profit, expected_investment=investment,
        chain_requirements=hooks.recipe_requirements(project['good'], capacity),
        capacity_limit=reason)
    return True


def load_project_completion_kernel():
    """Вернуть native-модуль project_completion_kernel или None.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.project_completion_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


def run_finish_python(engine, accounts, hooks, eps):
    return _run_finish(engine, accounts, hooks, eps, materials_ready_python, cash_ready_python)


def run_finish(engine, accounts, hooks, eps):
    return _run_finish(engine, accounts, hooks, eps, materials_ready, cash_ready)


def limit_project_capacity_python(project, capacity, eps, hooks, reason):
    return _limit_capacity(project, capacity, eps, hooks, reason, capacity_values_python)


def limit_capacity(project, capacity, eps, hooks, reason):
    return _limit_capacity(project, capacity, eps, hooks, reason, capacity_values)

_kernel = load_project_completion_kernel()
materials_ready = getattr(_kernel, 'materials_ready', materials_ready_python)
cash_ready = getattr(_kernel, 'cash_ready', cash_ready_python)
capacity_values = getattr(_kernel, 'capacity_values', capacity_values_python)
NATIVE_KERNEL = materials_ready is not materials_ready_python


def finish_projects(engine):
    """Единственная точка входа из ``Economy.finish_projects``.

    Граница обходит снимок списка счетов в порядке вставки.
    """
    run_finish(engine, list(engine.accounts.values()),
        ProjectCompletionHooks(engine), EPS)


def limit_project_capacity(engine, project, capacity):
    """Урезать ещё не начатый проект: числа из ядра, причина — здесь."""
    return limit_capacity(project, capacity, EPS,
        ProjectCompletionHooks(engine), CAPACITY_LIMIT_REASON)
