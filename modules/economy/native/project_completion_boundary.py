"""Граница завершения проектов: одна привязка реализации на импорт.

В Cython уходят два числовых среза состояния проектов:

  * цикл ``Economy.finish_projects``: проверка сметы и кассы, накопление
    ``asset`` из доставки и склада, запись ``e['capacity']`` и
    ``e['asset_value']`` при расширении;
  * ``Economy.limit_project_capacity``: пересчёт ``requested_capacity``,
    ``capacity``, ``working_cash``, ``expected_profit``,
    ``expected_investment`` ещё не начатого проекта.

Что осталось в Python и почему:

  * список счетов, порядок обхода и выбор проектов — в ``v3``: ядро получает
    готовый список;
  * снятие проекта со счёта, защита денег и перевод доставки — хуки: деньги
    двигает прежний ``Economy.transfer`` через ``cash_store``, а ``protected``
    пишет сам ``CashStore``; второго cash ledger нет;
  * склад уменьшает прежний ``use_stock`` (через существующую stock
    boundary) — хук, второй склад не заводится;
  * создание предприятия, квитанции, отчёты, события и lifecycle — хуки:
    это выбор/создание и отчётность, а не numeric-записи;
  * расчёт ``chain_requirements`` — хук движка, ядро лишь передаёт аргументы;
  * текстовая причина ``capacity_limit`` приходит аргументом из границы:
    ядро пишет её в тот же момент, что и прежний код, но само строк не
    содержит.

Арифметика намеренно ``object``: смета и доставка бывают и int, и float,
и тип результата виден дальше в отчёте. Порядок сложений ``asset`` и порядок
ключей ``project.update(...)`` сохранены буквально.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец
для focused-теста на побитовое совпадение и как fallback, если расширение
не собрано. Имена привязываются один раз при импорте.
"""

import importlib

from . import cash_boundary

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
        a.pop('project')
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


def run_finish_python(engine, accounts, hooks, eps):
    """Эталон числового цикла finish_projects — буквальный прежний код V3."""
    for a in accounts:
        p = a.get('project')
        if not p:
            continue
        bill = hooks.construction_bill(p['capacity'])
        need_cash = p['working_cash']+p.get('delivery', 0)
        if any(p['materials'].get(g, 0)+eps < q for g, q in bill.items()) \
                or hooks.free_cash(a['id'])+a.get('protected', 0)+eps < need_cash:
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


def limit_project_capacity_python(project, capacity, eps, hooks, reason):
    """Эталон урезания мощности — буквальный прежний код V3."""
    previous = project['capacity']
    if capacity+eps >= previous:
        return False
    previous_working = project['working_cash']
    construction = max(0, project['expected_investment']-previous_working)
    working = 0 if project['expansion'] else max(hooks.startup_cash(),
        capacity*project['expected_unit_cost'])
    project.update(requested_capacity=round(previous, 12), capacity=capacity,
        working_cash=working,
        expected_profit=project['expected_profit']*capacity/previous,
        expected_investment=construction*capacity/previous+working,
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


NATIVE_KERNEL = False

_kernel = load_project_completion_kernel()
if _kernel is not None:
    run_finish = _kernel.run_finish
    limit_capacity = _kernel.limit_project_capacity
    NATIVE_KERNEL = True
else:
    run_finish = run_finish_python
    limit_capacity = limit_project_capacity_python


def finish_projects(engine):
    """Единственная точка входа из ``Economy.finish_projects``.

    ``accounts`` — живой словарь счетов: ядро обходит его готовый список
    значений в прежнем порядке, сам сортировок не делает.
    """
    run_finish(engine, list(engine.accounts.values()),
        ProjectCompletionHooks(engine), EPS)


def limit_project_capacity(engine, project, capacity):
    """Урезать ещё не начатый проект: числа из ядра, причина — здесь."""
    return limit_capacity(project, capacity, EPS,
        ProjectCompletionHooks(engine), CAPACITY_LIMIT_REASON)
