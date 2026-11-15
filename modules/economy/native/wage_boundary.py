"""Граница зарплатного среза публичных строек: одна привязка на импорт.

Ядро считает только числа — готовность стройки, число работников, wage bill и
надбавку за жильё. Граница даёт ему доступ к движку через узкий набор хуков и
выполняет уже разрешённые side effects в прежнем порядке: склад, износ, отчёт,
завершение стройки и мутация ``project``.

Деньги двигает только прежний ``Economy.distribute``, а он — прежний
``Economy.transfer`` над ``cash_store``. Собственного ledger у границы нет,
второй cash ledger не заводится, ``balance`` напрямую не меняется.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец для
focused-тестов и как fallback, если расширение не собрано. Имена привязываются
один раз при импорте, чтобы не платить за диспетчер на каждой стройке.
"""

import importlib
from collections import defaultdict

# EPS проекта. Импорт из support безопасен: support не импортирует эту
# границу, поэтому цикла support -> boundary -> support не возникает.
from ..support import EPS


class WageHooks:
    """Side effects зарплатного среза: единственные точки мутации.

    Хуки намеренно узкие и повторяют прежний порядок записи. Ни один из них
    не считает числа: ядро передаёт уже посчитанные значения, а граница лишь
    фиксирует их там, где это делал прежний код.
    """

    __slots__ = ('engine', 'remaining')

    def __init__(self, engine):
        self.engine = engine
        # Список незавершённых строек текущего счёта. Прежний код собирал его
        # в локальную переменную и присваивал счёту после обхода проектов.
        self.remaining = None

    # --- чтение, которое ядру нужно для чисел -----------------------------
    def free_cash(self, account_id):
        return self.engine.free_cash(account_id)

    # --- обход счетов ------------------------------------------------------
    def begin(self, account):
        self.remaining = []

    def end(self, account):
        account['public_projects'] = self.remaining
        self.remaining = None

    # --- деньги: единственные переводы среза ------------------------------
    def pay_wages(self, account_id, project, wages):
        self.engine.distribute(account_id, self.engine.workforce(project['location']),
            wages, 'Заработная плата строителей')

    def pay_lodging(self, account_id, project, lodging):
        self.engine.distribute(account_id, self.engine.workforce(project['location']),
            lodging, 'Временное жильё строителей')

    # --- хвост стройки: склад, износ, отчёт, завершение --------------------
    def tail(self, account, project, ready, workers, advanced):
        engine = self.engine
        location = project['location']
        key = (location['state_id'], location['province_id'])
        if advanced:
            mode = engine.cfg['construction_modes'][project['mode']]
            if project['mode'] != 'accelerated':
                # Остаток труда уменьшается за границей, потому что labor —
                # живой словарь месяца, который читает и ядро.
                engine._construction_labor[key] -= workers
            for good, per_worker in mode['support_goods'].items():
                quantity = workers*per_worker
                if quantity <= EPS:
                    continue
                project['support_stock'][good] -= quantity
                engine.use_stock(account['id'], good, quantity)
                consumed = engine.report['consumed']
                consumed[good] = consumed.get(good, 0)+quantity
            fraction = workers/project['work_required']
            for good, total_wear in project['equipment_wear'].items():
                worn = min(project['equipment_held'].get(good, 0), total_wear*fraction)
                if worn <= EPS:
                    continue
                project['equipment_held'][good] -= worn
                engine.use_stock(account['id'], good, worn)
                wear = engine.report['equipment_wear']
                wear[good] = wear.get(good, 0)+worn
            project['work_done'] += workers
            project['status'] = 'building'
        complete = project['work_done']+EPS >= project['work_required']
        row = dict(project_id=project['id'], city_id=project['city_id'],
            state_id=location['state_id'], province_id=location['province_id'],
            kind=project['kind'],
            status='completed' if complete else project['status'],
            workers=workers, work_done=project['work_done'],
            work_required=project['work_required'],
            progress=min(1.0, project['work_done']/project['work_required']),
            ready=ready, mode=project['mode'])
        engine.report['public_projects'].append(row)
        if advanced:
            engine.report['construction_labor'].append(dict(state_id=location['state_id'],
                province_id=location['province_id'], workers=workers,
                project_id=project['id']))
        if complete:
            engine._complete_public_project(account, project)
        else:
            self.remaining.append(project)


def ready_python(project, eps):
    """Эталон готовности: прежние два ``all()`` с тем же short-circuit."""
    return (all(project['materials'].get(good, 0)+eps >= quantity
                for good, quantity in project['material_bill'].items())
            and all(project['equipment_held'].get(good, 0)+eps >= quantity
                    for good, quantity in project['equipment'].items()))


def run_advance_python(engine, accounts, labor, expedited, hooks, eps):
    """Эталон числового среза — буквальный прежний код ``advance_public_projects``."""
    for account in accounts:
        remaining = []
        for project in account.get('public_projects', []):
            ready = ready_python(project, eps)
            location = project['location']
            key = (location['state_id'], location['province_id'])
            workers = 0.0
            if ready:
                mode = engine.cfg['construction_modes'][project['mode']]
                if project['mode'] == 'accelerated':
                    workers = expedited.get(project['id'], 0)
                else:
                    workers = min(project['max_workers'],
                        project['work_required']-project['work_done'],
                        labor[key], engine.free_cash(account['id'])/engine.cfg['wage'])
                if workers > eps:
                    wages = workers*engine.cfg['wage']*mode['wage_multiplier']
                    engine.distribute(account['id'], engine.workforce(location),
                        wages, 'Заработная плата строителей')
                    lodging = workers*mode['lodging_allowance']
                    if lodging > eps:
                        engine.distribute(account['id'], engine.workforce(location),
                            lodging, 'Временное жильё строителей')
                    if project['mode'] != 'accelerated':
                        labor[key] -= workers
                    for good, per_worker in mode['support_goods'].items():
                        quantity = workers*per_worker
                        if quantity <= EPS:
                            continue
                        project['support_stock'][good] -= quantity
                        engine.use_stock(account['id'], good, quantity)
                        consumed = engine.report['consumed']
                        consumed[good] = consumed.get(good, 0)+quantity
                    fraction = workers/project['work_required']
                    for good, total_wear in project['equipment_wear'].items():
                        worn = min(project['equipment_held'].get(good, 0),
                            total_wear*fraction)
                        if worn <= EPS:
                            continue
                        project['equipment_held'][good] -= worn
                        engine.use_stock(account['id'], good, worn)
                        wear = engine.report['equipment_wear']
                        wear[good] = wear.get(good, 0)+worn
                    project['work_done'] += workers
                    project['status'] = 'building'
            complete = project['work_done']+EPS >= project['work_required']
            row = dict(project_id=project['id'], city_id=project['city_id'],
                state_id=location['state_id'], province_id=location['province_id'],
                kind=project['kind'],
                status='completed' if complete else project['status'],
                workers=workers, work_done=project['work_done'],
                work_required=project['work_required'],
                progress=min(1.0, project['work_done']/project['work_required']),
                ready=ready, mode=project['mode'])
            engine.report['public_projects'].append(row)
            if workers > eps:
                engine.report['construction_labor'].append(
                    dict(state_id=location['state_id'],
                        province_id=location['province_id'], workers=workers,
                        project_id=project['id']))
            if complete:
                engine._complete_public_project(account, project)
            else:
                remaining.append(project)
        account['public_projects'] = remaining
    return None


def load_wage_kernel():
    """Вернуть native-модуль wage_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.wage_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_wage_kernel()
if _kernel is not None:
    run_advance = _kernel.run_advance
    NATIVE_KERNEL = True
else:
    run_advance = run_advance_python


def advance_public_projects(engine):
    """Единственная точка входа: ядро поверх хуков, без второй копии цикла.

    ``labor`` — тот же живой словарь, что лежит в ``engine._construction_labor``:
    ядро читает его на каждой стройке, а хук ``tail`` уменьшает остаток.
    Поэтому объект привязывается к движку до вызова ядра, а не после.
    """
    labor = getattr(engine, '_construction_labor', defaultdict(float))
    engine._construction_labor = labor
    expedited = getattr(engine, '_expedited_construction_workers', {})
    accounts = sorted(engine.accounts.values(), key=lambda a: a['id'])
    hooks = WageHooks(engine)
    run_advance(engine, accounts, labor, expedited, hooks, EPS)
    engine._construction_labor = labor
