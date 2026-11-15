"""Граница production-ядра: один вызов из V3.produce, эталон и fallback.

Разведочный вертикальный срез production: в Cython уходит только числовой
цикл по предприятиям. Пролог (труд по домохозяйствам, природные ресурсы,
глубокое наблюдение), побочные эффекты (transfer через distribute, report,
dirty-поля и обновление записи предприятия) остаются в Python-хуках.

Порядок обхода сохраняется буквально: список предприятий перемешивает
``engine.shuffled`` в Python, RNG ядро не трогает. Склад меняется только
через существующие методы движка, которые делегируют в stock boundary, —
второй копии склада нет.

Эталонная Python-ветка (``produce_python``) повторяет прежнее тело
``Economy.produce`` и служит образцом для focused-теста на побитовое
совпадение с ядром, а также fallback, если расширение не собрано.
"""
import copy
import importlib
from collections import defaultdict

# EPS проекта. Импорт из support безопасен: support не импортирует эту
# границу, поэтому цикла support -> boundary -> support не возникает
# (в отличие от stock_boundary, которую support импортирует сам).
from ..support import EPS


class ProductionHooks(object):
    """Побочные эффекты production, которые обязаны остаться в Python.

    Ядро вызывает эти методы в том же порядке, в каком прежний код писал
    report и поля предприятия. Ни один метод не считает физику выпуска —
    только фиксирует уже посчитанные значения.
    """

    __slots__ = ('engine',)

    def __init__(self, engine, observe):
        self.engine = engine

    def observe(self, e, account, constraints, plan, month):
        """Глубокое наблюдение: снимок ограничений и склада до выпуска."""
        engine = self.engine
        e['production_observation'] = dict(month=month, constraints=dict(constraints),
            stocks_before=dict(account['stock']), free_cash_before=engine.free_cash(e['id']),
            procurement_plan=copy.deepcopy(plan) if plan.get('month') == month else None,
            expected_margin=e['expected_margin'], expected_unit_cost=e['expected_unit_cost'],
            expected_price=e['expected_price'])

    def input_used(self, good, quantity):
        """report['inputs_used']: расход сырья по товару."""
        report = self.engine.report['inputs_used']
        report[good] = report.get(good, 0)+quantity

    def equipment_worn(self, e, good, quantity):
        """report['equipment_wear'] и e['equipment_wear']: износ оборудования."""
        report = self.engine.report['equipment_wear']
        report[good] = report.get(good, 0)+quantity
        e['equipment_wear'][good] = e['equipment_wear'].get(good, 0)+quantity

    def production(self, good, quantity):
        """report['production']: выпуск по товару."""
        report = self.engine.report['production']
        report[good] = report.get(good, 0)+quantity


def run_enterprises_python(order, engine, labor, resources, observe, hooks, eps):
    """Эталон числового цикла production — буквальный прежний код V3."""
    month = engine.meta['month']
    for e in order:
        if e['status'] == 'closed' or e.get('suspended') or e['available_from'] > month:
            continue
        plan = e.get('production_plan', {})
        planned = plan['output'] if plan.get('month') == month else 0.0
        e['planned_output'] = planned
        recipe, a = engine.goods[e['good']], engine.accounts[e['id']]
        # Закупочные резервы уже сняты, проектный капитал остаётся защищён.
        project_hold = a.get('project', {}).get('working_cash', 0)+a.get('project', {}).get('delivery', 0)
        a['protected'] = project_hold
        constraints = {'План': planned, 'Мощность': e['capacity'], 'Труд': labor[e['state_id'], e['province_id']]/recipe['labor'],
            'Деньги на зарплату': engine.free_cash(e['id'])/(recipe['labor']*engine.cfg['wage'])}
        if recipe['resource']:
            constraints['Природный ресурс'] = resources[e['province_id'], recipe['resource']]
        for g, ratio in recipe['inputs'].items():
            constraints['Сырьё: '+g] = engine.free_stock(e['id'], g)/ratio
        for g, ratio in recipe.get('equipment', {}).items():
            constraints['Оборудование: '+g] = engine.free_stock(e['id'], g)/ratio
        if observe:
            hooks.observe(e, a, constraints, plan, month)
        reason = min(constraints, key=constraints.get)
        q = max(0, constraints[reason])
        e['input_limited'] = any(k.startswith('Сырьё') and v+eps < planned for k, v in constraints.items())
        if planned > eps:
            e['production_limit'] = reason
        e['reason'] = e['production_limit']
        if q <= eps:
            e['status'] = 'idle'
            continue
        workers = q*recipe['labor']
        labor[e['state_id'], e['province_id']] -= workers
        if recipe['resource']:
            resources[e['province_id'], recipe['resource']] -= q
        wages = workers*engine.cfg['wage']
        engine.distribute(e['id'], engine.workforce(e), wages, 'Заработная плата')
        cost = wages
        for g, ratio in recipe['inputs'].items():
            quantity = q*ratio
            cost += engine.use_stock(e['id'], g, quantity)
            hooks.input_used(g, quantity)
        for g, quantity in engine.equipment_wear(recipe, q).items():
            cost += engine.use_stock(e['id'], g, quantity)
            hooks.equipment_worn(e, g, quantity)
        # Цена нового выпуска заменяет цену распродажи только у пустого склада.
        if a['stock'].get(e['good'], 0) <= eps:
            e['price'] = plan.get('sale_price', e['price'])
        engine.add_stock(e['id'], e['good'], q, cost)
        e.update(status='active', workers=workers, output=q, production_cost=cost,
                 unit_cost=cost/q, wages=wages)
        hooks.production(e['good'], q)


def _labor(engine):
    """Труд по (государство, провинция) до цикла — прежний пролог V3."""
    labor = defaultdict(float)
    for a in engine.accounts.values():
        if a['kind'] == 'household':
            labor[a['state_id'], a['province_id']] += engine.population(a)*engine.cfg['labor_share']
    for key, reserved in getattr(engine, '_expedited_construction_reservations', {}).items():
        labor[key] = max(0, labor[key]-reserved)
    return labor


def _resources(engine):
    """Природные ресурсы по (провинция, ресурс) — прежний пролог V3."""
    resources = defaultdict(float)
    for t in engine.tiles.values():
        for resource in t['resources']:
            resources[t['province_id'], resource] += engine.cfg['extraction_per_tile']
    return resources


def _run(engine, existing, loop):
    """Общий пролог, один вызов цикла и фиксация труда для строительства."""
    observe = engine.report_detail() == 'deep'
    labor, resources = _labor(engine), _resources(engine)
    if observe:
        engine.report['labor_available'] = [dict(state_id=s, province_id=p, workers=q)
            for (s, p), q in labor.items()]
    hooks = ProductionHooks(engine, observe)
    # Перемешивание и расход RNG остаются в Python: ядро получает готовый порядок.
    loop(engine.shuffled(existing), engine, labor, resources, observe, hooks, EPS)
    engine._construction_labor = labor


def produce(engine, existing):
    """Единственный вход из V3.produce: native-цикл или эталонный fallback."""
    _run(engine, existing, run_enterprises)


def produce_python(engine, existing):
    """Эталонная Python-ветка целиком: пролог и числовой цикл."""
    _run(engine, existing, run_enterprises_python)


def load_production_kernel():
    """Вернуть native-модуль production_kernel или None, если он не собран.

    Отсутствие собранного расширения даёт ImportError, а не
    ModuleNotFoundError: пакет ``modules.economy.native`` существует, а
    подмодуля нет. Обе ситуации означают одно — ядро не собрано.
    """
    try:
        return importlib.import_module('.production_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_production_kernel()
if _kernel is not None:
    run_enterprises = _kernel.run_enterprises
    NATIVE_KERNEL = True
else:
    run_enterprises = run_enterprises_python
