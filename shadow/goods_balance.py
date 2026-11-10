"""Мировой товарный баланс: по каждому товару отдельно, после каждого месяца.

Проверяет тождество

    запасы на начало
    + произведено
    - производственное потребление
    - потребление населения
    - строительство (муниципальные и частные объекты)
    - прочие списания (износ оборудования, закреплённые материалы)
    = запасы на конец

Сделка товар не создаёт и не уничтожает: она двигает его между счетами, поэтому
в итог не входит вовсе. Резерв — не физический товар: он только запрещает
продавать уже имеющееся.

Где живёт товар (иначе баланс будет ложным):

* `account['stock']` — склады всех счетов, включая домохозяйства, казну,
  муниципалитеты, перевозчиков и владельцев. **Это единственное физическое
  место товара в модели.**
* `project['materials']` и `public_projects[].materials` — **не добавка**:
  закрепление только помечает товар, но не снимает его со склада
  (`_claim_public_stock` и `project_candidate` уменьшают лишь свободную часть).
  Физически он списывается позже, в `finish_projects` через `use_stock`, и там
  же попадает в `construction_used`. Учёт закреплённого вместе со складом
  удваивал бы товар — на первом прогоне это дало ложные +522 по доскам.

Изменения берутся из `report`, а не пересчитыванием: движок сам пишет туда
`production`, `inputs_used`, `consumed`, `construction_used`, `equipment_wear`.
"""
from collections import defaultdict

from modules.economy.support import EPS

# Допуск: суммы считаются в float64 по разным порядкам, последний разряд гуляет.
TOLERANCE = 1e-6


def physical_stock(engine):
    """Физический товар мира: только склады счетов.

    Закреплённые материалы строек физически лежат на тех же складах до момента
    `finish_projects`, поэтому отдельно не суммируются. Резерв (`stock_reserved`)
    — не товар, а запрет продавать уже имеющееся.
    """
    stock = defaultdict(float)
    for account in engine.accounts.values():
        for good, quantity in account['stock'].items():
            if quantity > 0:
                stock[good] += quantity
    return stock


def totals(engine):
    """Полный физический запас по товару."""
    return dict(physical_stock(engine))


def reserved_by_goods(engine):
    """Закреплённое за стройками: для диагностики, не для баланса."""
    held = defaultdict(float)
    for account in engine.accounts.values():
        project = account.get('project') or {}
        for bucket in ('materials', 'equipment_held'):
            for good, quantity in (project.get(bucket) or {}).items():
                held[good] += quantity
        for public in account.get('public_projects') or []:
            for bucket in ('materials', 'equipment_held', 'support_stock'):
                for good, quantity in (public.get(bucket) or {}).items():
                    held[good] += quantity
    return dict(held)


def check(engine, before, report=None):
    """Сверка месяца. Возвращает строки расхождений, пусто — баланс сошёлся.

    `before` — `totals()` до хода. `report` — отчёт месяца; если не передан,
    берётся `engine.report`.
    """
    report = engine.report if report is None else report
    after = totals(engine)
    produced = report.get('production', {}) or {}
    inputs_used = report.get('inputs_used', {}) or {}
    consumed = report.get('consumed', {}) or {}
    construction = report.get('construction_used', {}) or {}
    wear = report.get('equipment_wear', {}) or {}

    rows = []
    for good in sorted(set(before) | set(after) | set(produced) | set(inputs_used)
                       | set(consumed) | set(construction) | set(wear)):
        start = before.get(good, 0.0)
        end = after.get(good, 0.0)
        made = produced.get(good, 0.0)
        used = inputs_used.get(good, 0.0)
        eaten = consumed.get(good, 0.0)
        built = construction.get(good, 0.0)
        worn = wear.get(good, 0.0)
        expected = start+made-used-eaten-built-worn
        delta = end-expected
        if abs(delta) > TOLERANCE*max(1.0, abs(expected)):
            rows.append(dict(good=good, month=engine.meta.get('month'), start=start,
                             produced=made, inputs_used=used, consumed=eaten,
                             construction=built, wear=worn, end=end,
                             expected=expected, delta=delta))
    return rows


def describe(engine, rows):
    """Человекочитаемый разбор первого расхождения с указанием операций."""
    if not rows:
        return 'баланс сошёлся'
    row = rows[0]
    return ('месяц %s, товар %s: начало %.4f + произведено %.4f - сырьё %.4f '
            '- население %.4f - стройка %.4f - износ %.4f = %.4f, а на конец %.4f '
            '(расхождение %.6f)'
            % (row['month'], row['good'], row['start'], row['produced'],
               row['inputs_used'], row['consumed'], row['construction'],
               row['wear'], row['expected'], row['end'], row['delta']))


def audit(engine, before, report=None):
    """Проверка с исключением: расхождение означает дефект учёта товара."""
    rows = check(engine, before, report)
    if rows:
        raise ValueError('Нарушен мировой товарный баланс: '+describe(engine, rows))
    return rows
