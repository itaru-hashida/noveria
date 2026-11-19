"""Генератор architecture.json для браузерного architecture viewer.

Скрипт читает только код экономики и сам разрешает каждый узел, каждую связь и
каждое нарушение в реальную строку файла. Номера строк в модели не хардкодятся:
они берутся из `ast` (для .py) или из регулярного разбора (для .pyx/.pxd).
Каждая ссылка дополнительно проверяется якорем — подстрокой, которая обязана
быть в найденной строке. Если якорь не совпал, генератор падает, поэтому
architecture.json не может сослаться на несуществующее место.

Запуск:
    python architecture/generate_architecture.py

Выходные файлы:
    architecture/architecture.json
    architecture/viewer/public/architecture.json
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'architecture' / 'architecture.json'
VIEWER_COPY = ROOT / 'architecture' / 'viewer' / 'public' / 'architecture.json'

# Файлы, которые реально вошли в анализ. Список печатается в README и в meta.
ANALYSED_FILES = [
    'core/turn_service.py',
    'core/world_service.py',
    'core/economy_state.py',
    'core/lab.py',
    'core/db.py',
    'modules/economy/v3.py',
    'modules/economy/support.py',
    'modules/economy/__init__.py',
    'modules/economy/native/monthly_boundary.py',
    'modules/economy/native/monthly_driver.pyx',
    'modules/economy/native/monthly_hooks.py',
    'modules/economy/native/serial_step.pyx',
    'modules/economy/native/cash_store.pyx',
    'modules/economy/native/cash_boundary.py',
    'modules/economy/native/stock_store.pyx',
    'modules/economy/native/stock_store_boundary.py',
    'modules/economy/native/stock_boundary.py',
    'modules/economy/native/stock_kernel.pyx',
    'modules/economy/native/order_store.pyx',
    'modules/economy/native/order_boundary.py',
    'modules/economy/native/world_state.pyx',
    'modules/economy/native/production_boundary.py',
    'modules/economy/native/production_kernel.pyx',
    'modules/economy/native/match_boundary.py',
    'modules/economy/native/match_kernel.pyx',
    'modules/economy/native/match_driver.pyx',
    'modules/economy/native/trade_boundary.py',
    'modules/economy/native/trade_kernel.pyx',
    'modules/economy/native/consumption_boundary.py',
    'modules/economy/native/consumption_kernel.pyx',
    'modules/economy/native/owner_consumption_boundary.py',
    'modules/economy/native/topup_boundary.py',
    'modules/economy/native/quote_boundary.py',
    'modules/economy/native/quote_kernel.pyx',
    'modules/economy/native/demand_boundary.py',
    'modules/economy/native/demand_kernel.pyx',
    'modules/economy/native/money_boundary.py',
    'modules/economy/native/money_kernel.pyx',
    'modules/economy/native/dividend_boundary.py',
    'modules/economy/native/enterprise_settlement_boundary.py',
    'modules/economy/native/settlement_boundary.py',
    'modules/economy/native/settlement_kernel.pyx',
    'modules/economy/native/wage_boundary.py',
    'modules/economy/native/project_completion_boundary.py',
    'modules/economy/native/project_slot.py',
    'modules/economy/native/material_boundary.py',
    'modules/economy/native/delivery_boundary.py',
    'modules/economy/native/material_finalize_boundary.py',
    'modules/economy/native/cancel_boundary.py',
    'modules/economy/native/plan_boundary.py',
    'modules/economy/native/plan_kernel.pyx',
    'modules/economy/native/report_history_boundary.py',
    'modules/economy/native/event_buffer_boundary.py',
    'modules/economy/native/event_writer.py',
    'modules/economy/native/__init__.py',
]

# Тесты, на которые ссылается раздел evidence. Читаются только именами.
EVIDENCE_TESTS = [
    'tests/test_economy_native_cash_runtime.py',
    'tests/test_economy_native_cash_ownership.py',
    'tests/test_economy_native_order_ownership.py',
    'tests/test_economy_native_stock_ownership.py',
    'tests/test_economy_state.py',
    'tests/test_economy_native_monthly.py',
    'tests/test_economy_native_monthly_hooks.py',
]


# ---------------------------------------------------------------------------
# Индекс символов
# ---------------------------------------------------------------------------

class SymbolIndex:
    """Отображение (файл, qualname) -> список строк, где символ объявлен."""

    def __init__(self, root):
        self.root = root
        self.py = {}
        self.cy = {}
        self.lines = {}

    def line_count(self, path):
        if path not in self.lines:
            text = (self.root / path).read_text(encoding='utf-8')
            self.lines[path] = text.splitlines()
        return len(self.lines[path])

    def text(self, path, line):
        self.line_count(path)
        rows = self.lines[path]
        if 1 <= line <= len(rows):
            return rows[line - 1]
        return ''

    def add_py(self, path):
        tree = ast.parse((self.root / path).read_text(encoding='utf-8'))
        found = self.py.setdefault(path, {})

        def walk(node, prefix):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f'{prefix}{child.name}'
                    found.setdefault(name, []).append(child.lineno)
                    walk(child, name + '.')
                elif isinstance(child, ast.ClassDef):
                    name = f'{prefix}{child.name}'
                    found.setdefault(name, []).append(child.lineno)
                    walk(child, name + '.')

        walk(tree, '')

    def add_cy(self, path):
        rows = (self.root / path).read_text(encoding='utf-8').splitlines()
        found = self.cy.setdefault(path, {})
        cls, cls_indent = None, -1
        for number, raw in enumerate(rows, 1):
            line = raw.rstrip()
            if not line or line.lstrip().startswith('#'):
                continue
            indent = len(line) - len(line.lstrip())
            match = re.match(r'^(cdef\s+class|class)\s+(\w+)', line)
            if match and indent == 0:
                cls, cls_indent = match.group(2), indent
                found.setdefault(cls, []).append(number)
                continue
            match = re.match(r'^(\s*)def\s+(\w+)', line)
            if match:
                name = match.group(2)
                if cls is not None and len(match.group(1)) > cls_indent:
                    found.setdefault(f'{cls}.{name}', []).append(number)
                else:
                    found.setdefault(name, []).append(number)

    def build(self):
        for path in ANALYSED_FILES:
            if path.endswith('.py'):
                self.add_py(path)
            elif path.endswith(('.pyx', '.pxd', '.pxi')):
                self.add_cy(path)

    def resolve(self, path, spec, anchor=None):
        """Вернуть номер строки для символа (или литеральной строки).

        Спецификация с префиксом ``~`` ищет литерал по файлу: это нужно там, где
        точка вызова не является объявлением символа.
        """
        if isinstance(spec, int):
            return spec
        if isinstance(spec, str) and spec.startswith('~'):
            literal = spec[1:]
            rows = (self.root / path).read_text(encoding='utf-8').splitlines()
            for number, raw in enumerate(rows, 1):
                if literal in raw:
                    return number
            raise LookupError(f'НЕ НАЙДЕН ЛИТЕРАЛ: {path}::{literal}')
        table = self.py.get(path) or self.cy.get(path) or {}
        candidates = table.get(spec)
        if not candidates:
            raise LookupError(f'НЕ НАЙДЕН СИМВОЛ: {path}::{spec}')
        if anchor:
            for number in candidates:
                if anchor in self.text(path, number):
                    return number
            return candidates[0]
        return candidates[0]


INDEX = SymbolIndex(ROOT)
INDEX.build()

LOCATIONS = {}
LOCATION_ORDER = []


def location(path, spec, anchor=None, symbol=None, end_line=None):
    """Зарегистрировать и проверить одну ссылку файл:строка.

    Разрешение идёт по объявлению символа; если точка вызова лежит не в
    объявлении, берётся литеральная подстрока-якорь по всему файлу. Обе
    стратегии проверяют, что строка существует и содержит якорь.
    """
    if not (ROOT / path).is_file():
        raise SystemExit(f'НЕТ ФАЙЛА: {path}')
    line = None
    try:
        line = INDEX.resolve(path, spec, anchor)
    except LookupError:
        line = None
    if line is None or (anchor and anchor not in INDEX.text(path, line)):
        # Вторая стратегия: искать сам якорь по файлу.
        if not anchor:
            raise SystemExit(f'НЕ РАЗРЕШЁН СИМВОЛ БЕЗ ЯКОРЯ: {path}::{spec}')
        rows = (ROOT / path).read_text(encoding='utf-8').splitlines()
        hits = [number for number, raw in enumerate(rows, 1) if anchor in raw]
        if not hits:
            raise SystemExit(f'ЯКОРЬ НЕ НАЙДЕН В ФАЙЛЕ: {path}::{spec} ищем {anchor!r}')
        line = hits[0]
    total = INDEX.line_count(path)
    if not 1 <= line <= total:
        raise SystemExit(f'СТРОКА ВНЕ ФАЙЛА: {path}:{line} (всего {total})')
    if anchor and anchor not in INDEX.text(path, line):
        raise SystemExit(f'ЯКОРЬ НЕ НАЙДЕН В СТРОКЕ: {path}:{line} {anchor!r}')
    key = (path, line)
    if key in LOCATIONS:
        record = LOCATIONS[key]
        if anchor and anchor not in record['anchor']:
            record['anchor'] = (record['anchor'] + ' ' + anchor).strip()
        return record['id']
    record = {
        'id': f'loc-{len(LOCATION_ORDER) + 1:04d}',
        'file': path,
        'line': line,
        'end_line': end_line,
        'symbol': symbol or (spec if isinstance(spec, str) else None),
        'anchor': (anchor or INDEX.text(path, line)).strip(),
        'language': 'cython' if path.endswith(('.pyx', '.pxd', '.pxi')) else 'python',
        'verified': True,
    }
    LOCATIONS[key] = record
    LOCATION_ORDER.append(record)
    return record['id']


# ---------------------------------------------------------------------------
# Домены
# ---------------------------------------------------------------------------

DOMAINS = [
    dict(id='entry', title='Вход и координация',
         description='WorldService, TurnService, Lab: кто запускает месяц и в какой транзакции.',
         plain='Администрация игры. Здесь нажимается «следующий месяц» и решается, '
               'что весь ход либо пройдёт целиком, либо откатится.',
         color='#5b7cfa'),
    dict(id='engine', title='Движок и драйвер месяца',
         description='Economy V3, monthly_boundary, Cython-драйвер месяца и Python-хуки.',
         plain='Сам движок и расписание месяца. Здесь записано, в каком порядке идут '
               'этапы: цены → заявки → торги → производство → расчёты → отчёт.',
         color='#8a5cf6'),
    dict(id='production', title='Производство',
         description='Ограничения выпуска, расход сырья, износ, себестоимость.',
         plain='Заводы. Каждый месяц каждый завод решает, сколько может выпустить: '
               'упирается в план, мощность, рабочих, деньги на зарплату или сырьё.',
         color='#0f9d58'),
    dict(id='market', title='Рынок',
         description='Продающие заявки, матчинг, сделки, итоги книги за месяц.',
         plain='Биржа. Сюда выставляют «продаю» и «хочу купить», здесь они встречаются, '
               'и каждая встреча — это сделка с переводом денег и товара.',
         color='#e8710a'),
    dict(id='consumption', title='Потребление',
         description='Домохозяйства, собственники, допкупка базовых нужд.',
         plain='Люди едят и покупают вещи. Семьи и владельцы заводов выставляют заявки '
               'по своим деньгам, а недокупленное превращается в дефицит.',
         color='#c5221f'),
    dict(id='projects', title='Проекты и строительство',
         description='Инвестиционные проекты, муниципальные стройки, материалы, завершение.',
         plain='Стройки. Новый завод или школа — это проект: нужны материалы, деньги, '
               'рабочие. Когда всё готово, проект превращается в работающее предприятие.',
         color='#1a73e8'),
    dict(id='settlement', title='Расчёты',
         description='Оброк, зарплаты, дивиденды, демография, миграция, инварианты.',
         plain='День раздачи денег и проверки арифметики: зарплаты, дивиденды, налоги, '
               'население. В конце — контроль, что нигде не появилось отрицательных величин.',
         color='#b06000'),
    dict(id='money', title='Деньги',
         description='balance/reserved/protected, переводы, резервы под заявки и проекты.',
         plain='Кошельки и касса. Здесь лежит, сколько у кого денег, сколько уже '
               'отложено под заявки и стройки, и кто имеет право это менять.',
         color='#00a000'),
    dict(id='stock', title='Склад',
         description='stock/stock_reserved/values: количество и учётная стоимость.',
         plain='Склад. Сколько чего лежит, сколько уже обещано под заявки '
               'и во сколько обошлась эта партия.',
         color='#12a4a4'),
    dict(id='orders', title='Заявки',
         description='report["orders"]: статус, остаток, цена, резерв заявки.',
         plain='Список заявок текущего месяца: кто, что, сколько и по какой цене. '
               'У каждой есть статус и остаток, и менять их может только регистратор.',
         color='#d01884'),
    dict(id='save', title='Сохранение и отчёт',
         description='EconomyState, Parquet-checkpoint, отчёты, события, запись в Ladybug.',
         plain='Сейвы и отчёты. Экономика живёт в памяти, а на диск попадает только '
               'по явному сохранению — в отдельные файлы, не в базу мира.',
         color='#5f6368'),
    dict(id='native', title='Python/Cython границы',
         description='Boundary-модули, ядра Cython, binding и fallback по импорту.',
         plain='Шов между медленным и быстрым кодом. Через него считают числа на Cython, '
               'а решение и запись остаются в привычном Python.',
         color='#795548'),
]


# ---------------------------------------------------------------------------
# Узлы
# ---------------------------------------------------------------------------

# Расшифровка типов узла человеческим языком: что это вообще такое.
KIND_TITLES = {
    'class': 'Класс',
    'module': 'Модуль',
    'function': 'Функция или метод',
    'store': 'Хранилище (store)',
    'kernel': 'Ядро на Cython',
    'boundary': 'Граница Python↔Cython',
}

# Пояснения к типам: одна фраза «что это значит на практике».
KIND_EXPLANATIONS = {
    'class': 'Шаблон объекта: в нём живут данные и методы, которые с ними работают.',
    'module': 'Файл с кодом, который подключают другие части программы.',
    'function': 'Кусок кода с именем: его вызывают, чтобы что-то посчитать или изменить.',
    'store': 'Единственный «хозяин» поля: все изменения идут через его методы, а не напрямую.',
    'kernel': 'Быстрый код на Cython: считает числа, но почти ничего не хранит и не решает сам.',
    'boundary': 'Прослойка: переводит вызовы между обычным Python и быстрым Cython, держит запасной вариант.',
}

# Расшифровка видов связи: что означает стрелка.
EDGE_KIND_TITLES = {
    'calls': 'вызывает',
    'writes': 'пишет в',
    'owns': 'владеет',
    'binds': 'подменяет при импорте',
    'callback': 'вызывает обратно',
}

EDGE_KIND_EXPLANATIONS = {
    'calls': 'Один код вызывает другой: управление переходит туда и возвращается.',
    'writes': 'Код меняет состояние: сумму на счёте, остаток на складе, поле заявки.',
    'owns': 'Объект хранит ссылку на состояние и отвечает за него.',
    'binds': 'При импорте модуль подставляет быструю реализацию вместо медленной.',
    'callback': 'Быстрый код возвращается в Python, чтобы позвать функцию-«хук». Это дорогое место.',
}


def node(identity, name, short, kind, domain, language, path, spec, responsibility,
         owner=None, anchor=None, tags=(), end_line=None, plain=None):
    return dict(
        id=identity, name=name, short=short, type=kind, domain=domain,
        language=language, responsibility=responsibility, owner=owner,
        source_location=location(path, spec, anchor=anchor, symbol=name, end_line=end_line),
        tags=list(tags),
        plain=plain or '',
        kind_title=KIND_TITLES.get(kind, kind),
    )


NODES = [
    # --- вход и координация -------------------------------------------------
    node('entry.world_service', 'core.world_service.WorldService', 'WorldService',
         'class', 'entry', 'python', 'core/world_service.py', 'WorldService',
         'Единственный вход игровых действий: держит БД, шину событий и одно экономическое состояние мира.',
         owner='WorldService', anchor='class WorldService'),
    node('entry.advance_turn', 'core.world_service.WorldService.advance_turn',
         'WorldService.advance_turn', 'function', 'entry', 'python',
         'core/world_service.py', 'WorldService.advance_turn',
         'Команда «следующий ход»: при существующей кампании передаёт управление TurnService.finish.',
         anchor='def advance_turn'),
    node('entry.turn_service', 'core.turn_service.TurnService', 'TurnService',
         'class', 'entry', 'python', 'core/turn_service.py', 'TurnService',
         'Месячный цикл: список фаз внутри одной транзакции Ladybug, откат экономики снимком памяти.',
         owner='TurnService', anchor='class TurnService'),
    node('entry.turn_finish', 'core.turn_service.TurnService.finish',
         'TurnService.finish', 'function', 'entry', 'python',
         'core/turn_service.py', 'TurnService.finish',
         'Граница хода: begin_turn, прогон фаз, flush_structural, результат хода, autosave.',
         anchor='def finish'),
    node('entry.economy_phase', 'core.turn_service.TurnService._economy',
         'TurnService._economy', 'function', 'entry', 'python',
         'core/turn_service.py', 'TurnService._economy',
         'Фаза economy: вызывает modules.economy.monthly_phase в общей транзакции хода.',
         anchor='def _economy'),
    node('entry.monthly_phase', 'modules.economy.v3.monthly_phase', 'monthly_phase',
         'function', 'entry', 'python', 'modules/economy/v3.py', 'monthly_phase',
         'Мост TurnService → EconomyState.step; пишет событие economy_month в Ladybug.',
         anchor='def monthly_phase'),
    node('entry.lab', 'core.lab.execute_experiment', 'lab.execute_experiment',
         'function', 'entry', 'python', 'core/lab.py', 'execute_experiment',
         'Изолированный прогон: один объект состояния на весь эксперимент, checkpoint пишется один раз.',
         anchor='def execute_experiment'),

    # --- движок -------------------------------------------------------------
    node('engine.economy', 'modules.economy.v3.Economy', 'Economy',
         'class', 'engine', 'python', 'modules/economy/v3.py', 'Economy',
         'Движок V3: правила, счета, предприятия, отчёт месяца; владеет состоянием между ходами.',
         owner='Economy', anchor='class Economy'),
    node('engine.step', 'modules.economy.v3.Economy.step', 'Economy.step',
         'function', 'engine', 'python', 'modules/economy/v3.py', 'Economy.step',
         'Фасад месяца: проверка режимов, открытие окна владения cash, ровно один вызов драйвера.',
         anchor='def step'),
    node('engine.monthly_boundary', 'modules.economy.native.monthly_boundary',
         'monthly_boundary', 'boundary', 'native', 'python',
         'modules/economy/native/monthly_boundary.py', 'monthly_step_python',
         'Единственная точка входа месяца: native-драйвер либо Python-эталон; require_native_monthly.',
         anchor='def monthly_step_python'),
    node('engine.monthly_driver', 'modules.economy.native.monthly_driver.monthly_step',
         'monthly_driver.monthly_step', 'kernel', 'engine', 'cython',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'Cython-оркестрация месяца: порядок фаз, тайминги, ветки planning/investment/native.',
         anchor='def monthly_step'),
    node('engine.monthly_hooks', 'modules.economy.native.monthly_hooks.MonthlyHooks',
         'MonthlyHooks', 'boundary', 'native', 'python',
         'modules/economy/native/monthly_hooks.py', 'MonthlyHooks',
         'Python-хуки месяца: сброс резервов, защита кассы, истечение заявок, диагностика. Состояния не держит.',
         anchor='class MonthlyHooks'),
    node('engine.serial_step', 'modules.economy.native.serial_step',
         'serial_step', 'kernel', 'native', 'cython',
         'modules/economy/native/serial_step.pyx', 'update_prices',
         'Один packed-шаг: цены, recovery, продающие заявки, запросы домохозяйств и собственников.',
         anchor='def update_prices'),

    # --- деньги -------------------------------------------------------------
    node('money.cash_store', 'modules.economy.native.cash_store.CashStore',
         'CashStore', 'store', 'money', 'cython',
         'modules/economy/native/cash_store.pyx', 'CashStore',
         'Владелец balance/reserved/protected: колонки — истина, dict счёта — зеркало.',
         owner='CashStore', anchor='cdef class CashStore'),
    node('money.cash_store_python', 'modules.economy.native.cash_boundary.CashStorePython',
         'CashStorePython', 'store', 'money', 'python',
         'modules/economy/native/cash_boundary.py', 'CashStorePython',
         'Python-эталон и fallback владельца денежных полей с той же семантикой сверки.',
         owner='CashStorePython', anchor='class CashStorePython'),
    node('money.transfer', 'modules.economy.support.EconomyServices.transfer',
         'EconomyServices.transfer', 'function', 'money', 'python',
         'modules/economy/support.py', 'EconomyServices.transfer',
         'Единственная точка изменения balance: проверки, move_balance через владельца, ровно один flow.',
         anchor='def transfer'),
    node('money.money_boundary', 'modules.economy.native.money_boundary',
         'money_boundary', 'boundary', 'money', 'python',
         'modules/economy/native/money_boundary.py', 'settle',
         'Резервы под проекты, распределения и второй цикл settle поверх money_kernel.',
         anchor='def settle('),
    node('money.money_kernel', 'modules.economy.native.money_kernel',
         'money_kernel', 'kernel', 'money', 'cython',
         'modules/economy/native/money_kernel.pyx', 'distribute',
         'Числовые расчёты денег; читает состояние только через Python-хуки.',
         anchor='def distribute'),
    node('money.runtime_window', 'modules.economy.native.cash_store.CashStore.begin_runtime',
         'CashStore.begin_runtime', 'function', 'money', 'cython',
         'modules/economy/native/cash_store.pyx', 'CashStore.begin_runtime',
         'Окно владения на месяц: на входе принимает импорт, внутри месяца запрещает запись мимо владельца.',
         anchor='def begin_runtime'),

    # --- склад --------------------------------------------------------------
    node('stock.stock_store', 'modules.economy.native.stock_store.StockStore',
         'StockStore', 'store', 'stock', 'cython',
         'modules/economy/native/stock_store.pyx', 'StockStore',
         'Владелец stock/stock_reserved по счёту и товару; учётная стоимость values идёт через set_value.',
         owner='StockStore', anchor='cdef class StockStore'),
    node('stock.stock_store_python', 'modules.economy.native.stock_store_boundary.StockStorePython',
         'StockStorePython', 'store', 'stock', 'python',
         'modules/economy/native/stock_store_boundary.py', 'StockStorePython',
         'Python-эталон и fallback склада с теми же живыми словарями счетов.',
         owner='StockStorePython', anchor='class StockStorePython'),
    node('stock.stock_boundary', 'modules.economy.native.stock_boundary',
         'stock_boundary', 'boundary', 'native', 'python',
         'modules/economy/native/stock_boundary.py', 'add_stock_python',
         'Граница склада: привязка ядра/эталона при импорте, без диспетчера на каждой операции.',
         anchor='def add_stock_python'),
    node('stock.stock_kernel', 'modules.economy.native.stock_kernel',
         'stock_kernel', 'kernel', 'stock', 'cython',
         'modules/economy/native/stock_kernel.pyx', 'add_stock',
         'add_stock/use_stock/locked_material/free_stock/total_stock/gold; пишет только через StockStore.',
         anchor='def add_stock'),

    # --- заявки -------------------------------------------------------------
    node('orders.order_store', 'modules.economy.native.order_store.OrderStore',
         'OrderStore', 'store', 'orders', 'cython',
         'modules/economy/native/order_store.pyx', 'OrderStore',
         'Владелец статуса/остатка/цены/количества/резерва заявки; живой список report["orders"].',
         owner='OrderStore', anchor='cdef class OrderStore'),
    node('orders.order_store_python', 'modules.economy.native.order_boundary.OrderStorePython',
         'OrderStorePython', 'store', 'orders', 'python',
         'modules/economy/native/order_boundary.py', 'OrderStorePython',
         'Python-эталон и fallback владельца заявок.',
         owner='OrderStorePython', anchor='class OrderStorePython'),
    node('orders.world_state', 'modules.economy.native.world_state.WorldState',
         'WorldState', 'store', 'engine', 'cython',
         'modules/economy/native/world_state.pyx', 'WorldState',
         'Контейнер ссылок на существующие хранилища на время native-шага; своего состояния не держит.',
         anchor='cdef class WorldState'),

    # --- производство -------------------------------------------------------
    node('production.produce', 'modules.economy.v3.Economy.produce',
         'Economy.produce', 'function', 'production', 'python',
         'modules/economy/v3.py', 'Economy.produce',
         'Точка входа производства: порядок предприятий и RNG остаются здесь.',
         anchor='def produce'),
    node('production.boundary', 'modules.economy.native.production_boundary',
         'production_boundary', 'boundary', 'production', 'python',
         'modules/economy/native/production_boundary.py', 'produce',
         'Ограничения выпуска, применение результата, побочные эффекты отчёта через хуки.',
         anchor='def produce'),
    node('production.kernel', 'modules.economy.native.production_kernel.calculate_output',
         'production_kernel.calculate_output', 'kernel', 'production', 'cython',
         'modules/economy/native/production_kernel.pyx', 'calculate_output',
         'Чистый расчёт фактического выпуска по словарю ограничений.',
         anchor='def calculate_output'),

    # --- рынок --------------------------------------------------------------
    node('market.sell_orders', 'modules.economy.v3.Economy.sell_orders',
         'Economy.sell_orders', 'function', 'market', 'python',
         'modules/economy/v3.py', 'Economy.sell_orders',
         'Публикация продающих заявок предприятий и складских остатков.',
         anchor='def sell_orders'),
    node('market.order', 'modules.economy.v3.Economy.order', 'Economy.order',
         'function', 'market', 'python', 'modules/economy/v3.py', 'Economy.order',
         'Создание заявки: резерв кассы (buy) или склада (sell) через владельцев.',
         anchor='def order'),
    node('market.cancel_order', 'modules.economy.v3.Economy.cancel_order',
         'Economy.cancel_order', 'function', 'market', 'python',
         'modules/economy/v3.py', 'Economy.cancel_order',
         'Отмена заявки: освобождение резерва через CashStore/StockStore, статус через OrderStore.',
         anchor='def cancel_order'),
    node('market.match', 'modules.economy.v3.Economy.match', 'Economy.match',
         'function', 'market', 'python', 'modules/economy/v3.py', 'Economy.match',
         'Матчинг одной заявки покупателя с книгой предложений.',
         anchor='def match'),
    node('market.match_boundary', 'modules.economy.native.match_boundary',
         'match_boundary', 'boundary', 'market', 'python',
         'modules/economy/native/match_boundary.py', 'run_buyers_python',
         'Внешний цикл покупателей: обход, порядок, обратные вызовы сделки остаются в Python.',
         anchor='def run_buyers_python'),
    node('market.match_kernel', 'modules.economy.native.match_kernel.calculate_quantity',
         'match_kernel.calculate_quantity', 'kernel', 'market', 'cython',
         'modules/economy/native/match_kernel.pyx', 'calculate_quantity',
         'Чистый расчёт объёма сделки; книгу и записи не трогает.',
         anchor='def calculate_quantity'),
    node('market.trade_boundary', 'modules.economy.native.trade_boundary.apply_trade',
         'trade_boundary.apply_trade', 'boundary', 'market', 'python',
         'modules/economy/native/trade_boundary.py', 'apply_trade',
         'Применение сделки: деньги, склад, заявки через владельцев; откат одной сделки.',
         anchor='def apply_trade'),
    node('market.trade_kernel', 'modules.economy.native.trade_kernel.calculate_trade',
         'trade_kernel.calculate_trade', 'kernel', 'market', 'cython',
         'modules/economy/native/trade_kernel.pyx', 'calculate_trade',
         'Чистый расчёт количества, цены и доставки сделки.',
         anchor='def calculate_trade'),
    node('market.record_market', 'modules.economy.v3.Economy.record_market',
         'Economy.record_market', 'function', 'market', 'python',
         'modules/economy/v3.py', 'Economy.record_market',
         'Итоги книги за месяц: volume, trade_gold, freight, unmet_solvent, price_feedback.',
         anchor='def record_market'),
    node('market.report_history', 'modules.economy.native.report_history_boundary',
         'report_history_boundary', 'boundary', 'save', 'python',
         'modules/economy/native/report_history_boundary.py', 'record_market_rows_python',
         'Числовые агрегаты отчёта, истории продаж и строки рынка.',
         anchor='def record_market_rows_python'),

    # --- потребление --------------------------------------------------------
    node('consumption.household_requests', 'modules.economy.v3.Economy.household_requests',
         'Economy.household_requests', 'function', 'consumption', 'python',
         'modules/economy/v3.py', 'Economy.household_requests',
         'Потребности домохозяйств превращаются в покупательские заявки.',
         anchor='def household_requests'),
    node('consumption.owner_requests', 'modules.economy.v3.Economy.owner_requests',
         'Economy.owner_requests', 'function', 'consumption', 'python',
         'modules/economy/v3.py', 'Economy.owner_requests',
         'Личное потребление собственников: бюджет от свободной кассы.',
         anchor='def owner_requests'),
    node('consumption.owner_boundary', 'modules.economy.native.owner_consumption_boundary',
         'owner_consumption_boundary', 'boundary', 'consumption', 'python',
         'modules/economy/native/owner_consumption_boundary.py', 'consume_owner_purchases',
         'Применение купленного собственниками; списание идёт через stock boundary.',
         anchor='def consume_owner_purchases'),
    node('consumption.boundary', 'modules.economy.native.consumption_boundary',
         'consumption_boundary', 'boundary', 'consumption', 'python',
         'modules/economy/native/consumption_boundary.py', 'consume_households',
         'Числовой цикл потребления домохозяйств; списание через StockStore.',
         anchor='def consume_households'),
    node('consumption.kernel', 'modules.economy.native.consumption_kernel.calculate_consumption',
         'consumption_kernel.calculate_consumption', 'kernel', 'consumption', 'cython',
         'modules/economy/native/consumption_kernel.pyx', 'calculate_consumption',
         'Чистый расчёт доступного объёма потребления.',
         anchor='def calculate_consumption'),
    node('consumption.topup', 'modules.economy.native.topup_boundary.top_up',
         'topup_boundary.top_up', 'boundary', 'consumption', 'python',
         'modules/economy/native/topup_boundary.py', 'top_up',
         'Допкупка базовых нужд после освобождения резервов.',
         anchor='def top_up'),

    # --- проекты ------------------------------------------------------------
    node('projects.project_requests', 'modules.economy.v3.Economy.project_requests',
         'Economy.project_requests', 'function', 'projects', 'python',
         'modules/economy/v3.py', 'Economy.project_requests',
         'Инвестиционные решения месяца: кандидаты, расширения, очередь проектов.',
         anchor='def project_requests'),
    node('projects.project_slot', 'modules.economy.native.project_slot',
         'project_slot', 'function', 'projects', 'python',
         'modules/economy/native/project_slot.py', 'attach',
         'Единственная точка записи инвестиционного проекта на счёте (attach/detach).',
         anchor='def attach'),
    node('projects.start_public', 'modules.economy.v3.Economy.start_public_project',
         'Economy.start_public_project', 'function', 'projects', 'python',
         'modules/economy/v3.py', 'Economy.start_public_project',
         'Создание муниципальной стройки по статической смете.',
         anchor='def start_public_project'),
    node('projects.advance_public', 'modules.economy.v3.Economy.advance_public_projects',
         'Economy.advance_public_projects', 'function', 'projects', 'python',
         'modules/economy/v3.py', 'Economy.advance_public_projects',
         'Продвижение строек: труд, зарплаты, прогресс.',
         anchor='def advance_public_projects'),
    node('projects.wage_boundary', 'modules.economy.native.wage_boundary',
         'wage_boundary', 'boundary', 'projects', 'python',
         'modules/economy/native/wage_boundary.py', 'advance_public_projects',
         'Расчёт рабочих и зарплат строек; выплаты через хуки движка.',
         anchor='def advance_public_projects'),
    node('projects.finish', 'modules.economy.v3.Economy.finish_projects',
         'Economy.finish_projects', 'function', 'projects', 'python',
         'modules/economy/v3.py', 'Economy.finish_projects',
         'Завершение готовых проектов и создание предприятия.',
         anchor='def finish_projects'),
    node('projects.completion_boundary', 'modules.economy.native.project_completion_boundary',
         'project_completion_boundary', 'boundary', 'projects', 'python',
         'modules/economy/native/project_completion_boundary.py', 'finish_projects',
         'Проверка готовности и последовательное применение завершения проекта.',
         anchor='def finish_projects'),
    node('projects.material_boundary', 'modules.economy.native.material_boundary',
         'material_boundary', 'boundary', 'projects', 'python',
         'modules/economy/native/material_boundary.py', 'reserve_materials',
         'Резерв стройматериалов и заявки на недостающее.',
         anchor='def reserve_materials'),
    node('projects.delivery_boundary', 'modules.economy.native.delivery_boundary',
         'delivery_boundary', 'boundary', 'projects', 'python',
         'modules/economy/native/delivery_boundary.py', 'request_project_materials',
         'Смета доставки, защита кассы и закупка материалов проекта.',
         anchor='def request_project_materials'),
    node('projects.material_requests', 'modules.economy.v3.Economy.project_material_requests',
         'Economy.project_material_requests', 'function', 'projects', 'python',
         'modules/economy/v3.py', 'Economy.project_material_requests',
         'Последовательные закупки и отмены уже принятых проектов.',
         anchor='def project_material_requests'),
    node('projects.cancel_boundary', 'modules.economy.native.cancel_boundary',
         'cancel_boundary', 'boundary', 'projects', 'python',
         'modules/economy/native/cancel_boundary.py', 'cancel_stalled_project',
         'Отмена проектов по простою и по марже/спросу; снятие проекта — хуком.',
         anchor='def cancel_stalled_project'),

    # --- расчёты ------------------------------------------------------------
    node('settlement.settle', 'modules.economy.v3.Economy.settle', 'Economy.settle',
         'function', 'settlement', 'python', 'modules/economy/v3.py', 'Economy.settle',
         'Вход расчётов: оброк и финансовые итоги предприятий плюс распределения.',
         anchor='def settle'),
    node('settlement.enterprise_boundary', 'modules.economy.native.enterprise_settlement_boundary',
         'enterprise_settlement_boundary', 'boundary', 'settlement', 'python',
         'modules/economy/native/enterprise_settlement_boundary.py', 'settle_month',
         'Финансовые итоги предприятия, месяцы убыточности, закрытие; атомарный откат.',
         anchor='def settle_month'),
    node('settlement.dividend_boundary', 'modules.economy.native.dividend_boundary',
         'dividend_boundary', 'boundary', 'settlement', 'python',
         'modules/economy/native/dividend_boundary.py', 'pay_dividends',
         'Дивиденды: резерв, доступная касса, предел и доли; выплаты через transfer.',
         anchor='def pay_dividends('),
    node('settlement.demography', 'modules.economy.v3.Economy.update_demography',
         'Economy.update_demography', 'function', 'settlement', 'python',
         'modules/economy/v3.py', 'Economy.update_demography',
         'Демография: стресс, рождения, смерти по провинциям и стратам.',
         anchor='def update_demography'),
    node('settlement.boundary', 'modules.economy.native.settlement_boundary',
         'settlement_boundary', 'boundary', 'settlement', 'python',
         'modules/economy/native/settlement_boundary.py', 'run_migration',
         'Численные проверки инвариантов, демография и миграция.',
         anchor='def run_migration('),
    node('settlement.kernel', 'modules.economy.native.settlement_kernel.run_demography',
         'settlement_kernel.run_demography', 'kernel', 'settlement', 'cython',
         'modules/economy/native/settlement_kernel.pyx', 'run_demography',
         'Числовое тело демографии; население читает через population_hook.',
         anchor='def run_demography'),
    node('settlement.validate', 'modules.economy.v3.Economy.validate',
         'Economy.validate', 'function', 'settlement', 'python',
         'modules/economy/v3.py', 'Economy.validate',
         'Инварианты месяца: read-only проверки населения и счетов.',
         anchor='def validate'),

    # --- сохранение и отчёт -------------------------------------------------
    node('save.economy_state', 'core.economy_state.EconomyState', 'EconomyState',
         'class', 'save', 'python', 'core/economy_state.py', 'EconomyState',
         'Владелец экономического состояния между ходами, отчётов и Parquet-checkpoint.',
         owner='EconomyState', anchor='class EconomyState'),
    node('save.state_step', 'core.economy_state.EconomyState.step', 'EconomyState.step',
         'function', 'save', 'python', 'core/economy_state.py', 'EconomyState.step',
         'Один месяц: вызывает движок и хранит last_report/reports.',
         anchor='def step'),
    node('save.begin_turn', 'core.economy_state.EconomyState.begin_turn',
         'EconomyState.begin_turn', 'function', 'save', 'python',
         'core/economy_state.py', 'EconomyState.begin_turn',
         'Откат хода = снимок изменяемой части движка (export_state).',
         anchor='def begin_turn'),
    node('save.flush_structural', 'core.economy_state.EconomyState.flush_structural',
         'EconomyState.flush_structural', 'function', 'save', 'python',
         'core/economy_state.py', 'EconomyState.flush_structural',
         'Единственная запись экономики в Ladybug: Institution и SocialGroup(population, welfare).',
         anchor='def flush_structural'),
    node('save.save_checkpoint', 'core.economy_state.EconomyState.save_checkpoint',
         'EconomyState.save_checkpoint', 'function', 'save', 'python',
         'core/economy_state.py', 'EconomyState.save_checkpoint',
         'Запись всей экономики в Parquet: staging → подмена, previous как резерв.',
         anchor='def save_checkpoint'),
    node('save.load_checkpoint', 'core.economy_state.EconomyState.load_checkpoint',
         'EconomyState.load_checkpoint', 'function', 'save', 'python',
         'core/economy_state.py', 'EconomyState.load_checkpoint',
         'Восстановление из Parquet с проверкой схемы, отпечатка мира и keep_reports.',
         anchor='def load_checkpoint'),
    node('save.record_event', 'core.db.WorldDB.record_event', 'WorldDB.record_event',
         'function', 'save', 'python', 'core/db.py', 'WorldDB.record_event',
         'Запись события в Ladybug: события и структура мира, не экономика.',
         anchor='def record_event'),
    node('save.autosave', 'core.turn_service.TurnService._autosave',
         'TurnService._autosave', 'function', 'save', 'python',
         'core/turn_service.py', 'TurnService._autosave',
         'Autosave по умолчанию выключен: экономика пишется только явным сохранением.',
         anchor='def _autosave'),
    node('save.event_recorder', 'modules.economy.native.event_buffer_boundary.EventRecorderPython',
         'EventRecorder', 'boundary', 'save', 'python',
         'modules/economy/native/event_buffer_boundary.py', 'EventRecorderPython',
         'Синхронный буфер событий месяца: фильтр, батчи, ссылки на готовые строки отчёта.',
         anchor='class EventRecorderPython'),
    node('save.event_writer', 'modules.economy.native.event_writer.EventWriter',
         'EventWriter', 'boundary', 'save', 'python',
         'modules/economy/native/event_writer.py', 'EventWriter',
         'Фоновый writer событий: единственный, кто касается диска в этом слое.',
         anchor='class EventWriter'),
    node('save.world_save', 'core.world_service.WorldService.save_checkpoint',
         'WorldService.save_checkpoint', 'function', 'save', 'python',
         'core/world_service.py', 'WorldService.save_checkpoint',
         'Явное сохранение мира: определяет путь и вызывает EconomyState.save_checkpoint.',
         anchor='def save_checkpoint'),
]


# ---------------------------------------------------------------------------
# Простые объяснения узлов
# ---------------------------------------------------------------------------
#
# Здесь то, что не влезает в formal responsibility: аналогия и «зачем это мне».
# Текст намеренно бытовой — карта читается и теми, кто не писал этот код.

PLAIN_NODES = {
    'entry.world_service':
        'Это главный вход в игру. Как ресепшн: сюда приходят все команды игрока '
        '(сделать ход, построить, поменять налог), и уже отсюда их раздают нужным отделам. '
        'Он же держит в памяти тот самый единственный «мир экономики».',
    'entry.advance_turn':
        'Кнопка «следующий месяц». Если мир уже создан — просто передаёт эстафету '
        'TurnService.finish и ничего сам не считает.',
    'entry.turn_service':
        'Прораб одного хода. У него есть список этапов, и он проходит их по порядку '
        'внутри одной транзакции: либо месяц прошёл целиком, либо откатился назад.',
    'entry.turn_finish':
        'Само тело хода: запомнить состояние на случай сбоя → выполнить все этапы → '
        'записать результат в базу → при необходимости сохранить экономику.',
    'entry.economy_phase':
        'Один из этапов хода — «экономика». Просто зовёт monthly_phase и ничего больше.',
    'entry.monthly_phase':
        'Переходник между миром и экономикой: берёт живое состояние мира и говорит ему '
        '«проживи месяц», а потом пишет в базу событие о том, что месяц прожит.',
    'entry.lab':
        'Отдельный стенд для экспериментов: гоняет много месяцев подряд без игрока, '
        'но экономику на диск пишет только один раз в конце — иначе прогон был бы медленным.',

    'engine.economy':
        'Сердце игры. Здесь лежат все правила: счета, заводы, цены, зарплаты, налоги. '
        'Один такой объект живёт всю игру и меняется от месяца к месяцу.',
    'engine.step':
        'Вход в месяц. Как дирижёр: проверяет, что попросили правильный режим, '
        'объявляет «начинаю» (открывает окно владения деньгами) и делает РОВНО ОДИН '
        'вызов драйвера месяца. Вся настоящая работа — дальше.',
    'engine.monthly_boundary':
        'Развилка на две дороги. Если быстрая версия собрана — отдаёт месяц ей. '
        'Если нет — использует медленный запасной вариант на чистом Python. '
        'Плюс умеет строго потребовать быструю версию и упасть, если её нет.',
    'engine.monthly_driver':
        'Настоящий план месяца, записанный на быстром языке. Держит порядок: '
        'сначала цены, потом заявки, потом торги, потом производство, потом расчёты, '
        'потом отчёт. Именно по этому файлу построена вкладка «Месячный ход».',
    'engine.monthly_hooks':
        '«Поручения для Python» на время месяца. Драйвер на Cython не может сам аккуратно '
        'сделать несколько вещей — и зовёт эти функции. Состояния не хранит, только передаёт.',
    'engine.serial_step':
        'Огромный быстрый агрегат: за один заход считает сразу цены, восстановление, '
        'продающие заявки и запросы покупателей. Так быстрее, чем дёргать Python по десять раз.',

    'money.cash_store':
        'Кассир. Единственный, кому разрешено менять деньги на счетах. Все остальные '
        'обязаны просить его. Если кто-то полезет в кошелёк напрямую в течение месяца — '
        'программа намеренно упадёт с ошибкой.',
    'money.cash_store_python':
        'Тот же кассир, но написанный на обычном Python. Нужен как образец для сверки '
        'и как запасной вариант, если быстрая версия не собралась.',
    'money.transfer':
        'Перевод денег со счёта на счёт. Единственное законное место, где деньги '
        'реально переезжают, и оно оставляет ровно одну запись в отчёте о движении средств.',
    'money.money_boundary':
        'Прослойка для денежных расчётов: сколько нужно отложить под стройку, '
        'сколько раздать. Считает быстрое ядро, а применяет Python.',
    'money.money_kernel':
        'Калькулятор денег на Cython. Считает резервы и распределения, но читает данные '
        'только через переданные ему функции — сам в счета не лазит.',
    'money.runtime_window':
        'Объявление «месяц начался, касса под охраной». Внутри этого окна любая попытка '
        'изменить баланс мимо кассира — ошибка. В конце месяца окно закрывается со сверкой.',

    'stock.stock_store':
        'Кладовщик. Ведёт склад: сколько чего лежит и во сколько это обошлось. '
        'Как и кассир, обязан быть единственным, кто меняет эти цифры.',
    'stock.stock_store_python':
        'Тот же кладовщик на чистом Python: образец для тестов и запасной вариант.',
    'stock.stock_boundary':
        'Прослойка склада: при запуске программы решает, использовать быструю версию '
        'или запасную, и запоминает это имя. Дальше вызов идёт без лишних проверок.',
    'stock.stock_kernel':
        'Быстрые операции склада: положить, списать по средней цене, посчитать свободный остаток. '
        'Пишет только через кладовщика.',

    'orders.order_store':
        'Регистратор заявок. Заявка — это «хочу купить 10 хлеба по 5» или «продаю 10 хлеба». '
        'Он единственный меняет им статус, остаток и цену, чтобы не было двух разных правд.',
    'orders.order_store_python':
        'Тот же регистратор на чистом Python: образец и запасной вариант.',
    'orders.world_state':
        'Сборный ящик со ссылками на кассира, кладовщика и регистратора. Своего состояния '
        'не имеет — просто чтобы не передавать три объекта по отдельности. Живёт только '
        'в быстром месяце.',

    'production.produce':
        '«Пусть заводы произведут». Здесь решается порядок обхода заводов (его задаёт '
        'случай), а сами расчёты уходят в прослойку.',
    'production.boundary':
        'Цех производства: для каждого завода решает, сколько он МОЖЕТ выпустить '
        'с учётом плана, мощности, рабочих, денег, сырья и оборудования. '
        'Учитывает и списывает сырьё, считает себестоимость.',
    'production.kernel':
        'Один быстрый вопрос: «из всех ограничений какое самое узкое?» Ответ и есть объём выпуска.',

    'market.sell_orders':
        '«Кто что продаёт». Собирает все продающие заявки: у заводов — их товар, '
        'у складов — остатки. Это список предложения на месяц.',
    'market.order':
        'Создание одной заявки. Если это покупка — сразу откладывает (резервирует) деньги. '
        'Если продажа — откладывает товар. Резерв не даёт потратить одно и то же дважды.',
    'market.cancel_order':
        'Отмена заявки: возвращает отложенные деньги или товар обратно и помечает заявку '
        'отменённой. Без этого резервы утекали бы навсегда.',
    'market.match':
        'Торг по одной заявке покупателя: перебирает предложения и заключает сделки, '
        'пока не купит сколько хотел или пока не кончатся варианты.',
    'market.match_boundary':
        'Внешний цикл торгов: перебирает покупателей по очереди. Порядок покупателей — '
        'это случай, и он важен: от него зависят сделки. Поэтому цикл остался в Python.',
    'market.match_kernel':
        'Быстрый расчёт объёма одной сделки: сколько реально можно купить у этого продавца. '
        'Ничего не записывает — только считает.',
    'market.trade_boundary':
        'Проведение сделки: перевести деньги, переложить товар, обновить обе заявки. '
        'Умеет откатить одну сделку целиком, если что-то пошло не так посередине.',
    'market.trade_kernel':
        'Быстрый расчёт параметров сделки: количество, цена с учётом доставки, стоимость перевозки.',
    'market.record_market':
        'Итоги торгов за месяц: объёмы, суммы, доставка, сколько спроса осталось без товара. '
        'По этим цифрам потом растут цены и появляются новые заводы.',
    'market.report_history':
        'Бухгалтерия отчёта: считает итоги по счетам, секторам и складам, ведёт историю продаж.',

    'consumption.household_requests':
        '«Что нужно людям». Каждая семья смотрит на свои деньги и цены и выставляет '
        'заявки на еду и вещи. Сначала базовые нужды, потом дополнительные.',
    'consumption.owner_requests':
        'Личные покупки владельцев заводов и купцов: они тоже едят и одеваются, '
        'и их бюджет зависит от свободных денег.',
    'consumption.owner_boundary':
        'Применение покупок владельцев: списать купленное со склада и записать, сколько съедено.',
    'consumption.boundary':
        'Потребление семей: берёт заявку, смотрит, сколько товара реально пришло, '
        'и списывает его со склада. Не купленное остаётся в дефиците.',
    'consumption.kernel':
        'Один быстрый вопрос: «сколько можно взять, если есть столько-то и нужно столько-то?» '
        'Ответ — минимум из двух.',
    'consumption.topup':
        'Докупить еду после того, как освободились резервы: у семьи может появиться '
        'немного денег, и базовые нужды получают второй шанс.',

    'projects.project_requests':
        'Инвестиции: «где не хватает товара, не построить ли ещё завод?» Считает, '
        'кому выгодно расширяться, ставит проекты в очередь, отменяет безнадёжные.',
    'projects.project_slot':
        'Одна маленькая дверь, через которую проект прикрепляется к счёту или снимается с него. '
        'Нужна, чтобы не писать это присваивание в десяти разных местах.',
    'projects.start_public':
        'Начать муниципальную стройку: школа, больница, дорога. Создаёт проект по готовой смете.',
    'projects.advance_public':
        'Двигать стройки: нанять рабочих, заплатить им, продвинуть готовность.',
    'projects.wage_boundary':
        'Расчёт рабочих и зарплат на стройках: сколько людей можно нанять, сколько им заплатить, '
        'сколько денег на проживание.',
    'projects.finish':
        '«Стройка закончена». Проверяет, что материалы и деньги на месте, и превращает '
        'проект в работающее предприятие.',
    'projects.completion_boundary':
        'Проверка готовности и аккуратное применение завершения: создать предприятие, '
        'списать материалы, оплатить доставку, записать в отчёт.',
    'projects.material_boundary':
        'Закупка стройматериалов: сколько чего нужно, что уже есть, на сколько не хватает — '
        'и заявки на недостающее.',
    'projects.delivery_boundary':
        'Доставка для стройки: считает расстояние и стоимость перевозки, откладывает '
        'деньги на перевозку и материалы.',
    'projects.material_requests':
        'Поход по уже принятым проектам: докупить материалы, а если проект явно '
        'безнадёжен — отменить и вернуть деньги.',
    'projects.cancel_boundary':
        'Правила отмены проекта: если стройка замерла или экономика проекта сошлась в минус — '
        'снять проект и освободить резервы.',

    'settlement.settle':
        '«Пора платить по счетам». Оброк предприятий, их прибыль и убытки, потом '
        'распределение денег от казны и перевозчиков.',
    'settlement.enterprise_boundary':
        'Финансовый итог предприятия за месяц: накладные расходы, прибыль, сколько месяцев '
        'подряд в минусе. Если слишком долго — завод закрывается и возвращает капитал.',
    'settlement.dividend_boundary':
        'Дивиденды: сколько можно вынуть из кассы, не оставив завод без оборотных денег, '
        'и как поделить это между владельцами.',
    'settlement.demography':
        'Население: как стресс от нехватки еды влияет на рождения и смерти в каждой провинции.',
    'settlement.boundary':
        'Проверки «а не поехала ли арифметика» и миграция людей между провинциями.',
    'settlement.kernel':
        'Быстрая демография: считает рождения и смерти по стратам. Население читает через '
        'переданную функцию, сам в мир не пишет.',
    'settlement.validate':
        'Финальная проверка месяца: не появилось ли отрицательных денег, товаров или '
        'разъехавшихся сумм. Лучше упасть, чем тихо испортить сохранение.',

    'save.economy_state':
        '«Сейв» экономики. Держит состояние между ходами, помнит отчёты и умеет '
        'записать всё в файлы и прочитать обратно.',
    'save.state_step':
        'Один месяц на уровне сохранения: позвать движок и запомнить полученный отчёт.',
    'save.begin_turn':
        'Сделать снимок состояния перед ходом. Если месяц упадёт посередине — из этого '
        'снимка всё вернётся как было.',
    'save.flush_structural':
        'Единственное, что из экономики пишется в базу мира: построенные учреждения '
        'и население провинций. Деньги и склады туда не идут — они в отдельном файле.',
    'save.save_checkpoint':
        'Запись экономики на диск. Пишет во временную папку и только потом подменяет '
        'старую — чтобы прерванная запись не оставила битый сейв.',
    'save.load_checkpoint':
        'Чтение экономики с диска. Проверяет, что файл от той же версии и от того же мира, '
        'иначе отказывается загружать.',
    'save.record_event':
        'Запись события в базу мира: «построили», «прошёл месяц». Это журнал, а не экономика.',
    'save.autosave':
        'Автосохранение после хода. По умолчанию выключено: экономика пишется только '
        'когда игрок явно сохранил.',
    'save.event_recorder':
        'Буфер событий месяца: собирает строки, фильтрует по уровню детализации '
        'и режет на пачки. На диск сам не пишет.',
    'save.event_writer':
        'Фоновый писатель событий на диск. Единственный в этом слое, кто вообще '
        'трогает файлы с событиями.',
    'save.world_save':
        'Явное сохранение мира: решает, куда писать, и передаёт работу EconomyState.',
}

for _item in NODES:
    _item['plain'] = PLAIN_NODES.get(_item['id'], '')

_missing_plain = [item['id'] for item in NODES if not item['plain']]
if _missing_plain:
    raise SystemExit('НЕТ ПРОСТОГО ОБЪЯСНЕНИЯ ДЛЯ УЗЛОВ: ' + ', '.join(_missing_plain))


# ---------------------------------------------------------------------------
# Связи
# ---------------------------------------------------------------------------

# Счётчик заводится отдельно от списка: `EDGES += [...]` вычисляет весь правый
# список ДО присваивания, поэтому `len(EDGES)` внутри edge() всегда был бы
# нулём, и все 73 связи получили бы один идентификатор `edge-001`. В JSON это
# выглядело нормально, а Cytoscape оставлял из них ровно одно ребро — снаружи
# это и было «видна только одна стрелка».
_EDGE_SEQUENCE = 0


def edge(source, target, kind, path, spec, label, anchor=None, confidence='confirmed',
         note=None, plain=None):
    global _EDGE_SEQUENCE
    _EDGE_SEQUENCE += 1
    return dict(
        id=f'edge-{_EDGE_SEQUENCE:03d}',
        source=source, target=target, kind=kind, label=label,
        confidence=confidence,
        source_location=location(path, spec, anchor=anchor, symbol=label),
        note=note,
        plain=plain or '',
        kind_title=EDGE_KIND_TITLES.get(kind, kind),
        kind_explanation=EDGE_KIND_EXPLANATIONS.get(kind, ''),
    )


EDGES = []

EDGES += [
    edge('entry.world_service', 'entry.turn_service', 'owns',
         'core/world_service.py', 'WorldService.__init__', 'WorldService создаёт TurnService',
         anchor='TurnService(self)', note='TurnService получает сервис и его БД.'),
    edge('entry.advance_turn', 'entry.turn_finish', 'calls',
         'core/world_service.py', 'WorldService.advance_turn', 'advance_turn → turns.finish',
         anchor='return self.turns.finish'),
    edge('entry.turn_service', 'entry.economy_phase', 'calls',
         'core/turn_service.py', 'TurnService.__init__', 'фазы хода: commands → economy → integrity → snapshot',
         anchor="('economy',self._economy)"),
    edge('entry.economy_phase', 'entry.monthly_phase', 'calls',
         'core/turn_service.py', 'TurnService._economy', 'monthly_phase(service, ctx)',
         anchor='monthly_phase(self.service, ctx)'),
    edge('entry.monthly_phase', 'save.state_step', 'calls',
         'modules/economy/v3.py', 'monthly_phase', 'state.step()', anchor='report = state.step()'),
    edge('save.state_step', 'engine.step', 'calls',
         'core/economy_state.py', 'EconomyState.step', 'engine.step(planning_mode)',
         anchor='self.engine.step'),
    edge('engine.step', 'engine.monthly_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.step', 'monthly_boundary.monthly_step(...)',
         anchor='monthly_boundary.monthly_step'),
    edge('engine.monthly_boundary', 'engine.monthly_driver', 'binds',
         'modules/economy/native/monthly_boundary.py', 'monthly_step_python',
         'monthly_step = _driver.monthly_step (при собранном расширении)',
         anchor='monthly_step = _driver.monthly_step',
         note='Даже native_mode="off" идёт через Cython-драйвер; Python-эталон — только fallback.'),
    edge('engine.monthly_driver', 'engine.monthly_hooks', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'hooks = _make_hooks(engine)', anchor='hooks = _make_hooks(engine)',
         note='Cython → Python: один объект хуков на месяц.'),
    edge('engine.step', 'money.runtime_window', 'calls',
         'modules/economy/v3.py', 'Economy.step', 'cash_store.begin_runtime()',
         anchor='self.cash_store.begin_runtime()'),
    edge('engine.step', 'save.event_writer', 'calls',
         'modules/economy/v3.py', 'Economy.step', '_event_writer.drain_recorder(...)',
         anchor='_event_writer.drain_recorder'),

    # деньги
    edge('engine.economy', 'money.cash_store', 'owns',
         'modules/economy/v3.py', 'Economy.cash_store', 'Economy.cash_store → CashStore',
         anchor='_cash.CashStore(accounts)'),
    edge('money.transfer', 'money.cash_store', 'writes',
         'modules/economy/support.py', 'EconomyServices.transfer', 'cash.move_balance(...)',
         anchor='cash.move_balance'),
    edge('money.money_boundary', 'money.money_kernel', 'calls',
         'modules/economy/native/money_boundary.py', 'settle', 'run_settle → money_kernel',
         anchor='run_settle(MoneyHooks(engine)'),
    edge('money.cash_store', 'money.runtime_window', 'owns',
         'modules/economy/native/cash_store.pyx', 'CashStore.begin_runtime',
         'окно владения открывается на месяц', anchor='def begin_runtime'),

    # склад
    edge('engine.economy', 'stock.stock_store', 'owns',
         'modules/economy/v3.py', 'Economy.stock_store', 'Economy.stock_store → StockStore',
         anchor='def stock_store'),
    edge('stock.stock_boundary', 'stock.stock_kernel', 'binds',
         'modules/economy/native/stock_boundary.py', 'add_stock_python',
         'add_stock = _kernel.add_stock', anchor='_kernel = load_stock_kernel()'),
    edge('stock.stock_kernel', 'stock.stock_store', 'writes',
         'modules/economy/native/stock_kernel.pyx', 'add_stock',
         'stock_store.add_stock / set_value', anchor='stock_store.add_stock'),
    edge('production.boundary', 'stock.stock_store', 'writes',
         'modules/economy/native/production_boundary.py', 'run_enterprises',
         'engine.use_stock → StockStore', anchor='engine.use_stock'),

    # заявки
    edge('engine.economy', 'orders.order_store', 'owns',
         'modules/economy/v3.py', 'Economy.order_store', 'Economy.order_store → OrderStore',
         anchor='_order.OrderStore(orders)'),
    edge('market.order', 'orders.order_store', 'writes',
         'modules/economy/v3.py', 'Economy.order', 'order_store.append(o)',
         anchor='self.order_store.append(o)'),
    edge('market.order', 'money.cash_store', 'writes',
         'modules/economy/v3.py', 'Economy.order', 'add_reserved_cash (buy)',
         anchor='self.cash_store.add_reserved_cash'),
    edge('market.order', 'stock.stock_store', 'writes',
         'modules/economy/v3.py', 'Economy.order', 'add_reserved (sell)',
         anchor='self.stock_store.add_reserved'),
    edge('market.cancel_order', 'money.cash_store', 'writes',
         'modules/economy/v3.py', 'Economy.cancel_order', 'release_reserved_cash',
         anchor='self.cash_store.release_reserved_cash'),
    edge('market.cancel_order', 'stock.stock_store', 'writes',
         'modules/economy/v3.py', 'Economy.cancel_order', 'release_reserved',
         anchor='self.stock_store.release_reserved'),
    edge('orders.world_state', 'orders.order_store', 'owns',
         'modules/economy/v3.py', 'Economy.world_state', 'WorldState перепривязывается к OrderStore',
         anchor='state = self._world_state = _world.WorldState()'),

    # рынок
    edge('engine.monthly_driver', 'market.sell_orders', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'sells = engine.sell_orders(...)', anchor='engine.sell_orders(exclude=lagged_firms)'),
    edge('engine.monthly_driver', 'market.match_boundary', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         '_match.run_buyers(engine, buys, ...)', anchor='_match.run_buyers'),
    edge('market.match_boundary', 'market.trade_boundary', 'callback',
         'modules/economy/native/match_boundary.py', 'run_buyers_python',
         'commit=commit_trade вызывается на каждую сделку',
         anchor='run_buyer_python(owner, buy, sells, eps, commit, inner, local_only)',
         note='Python-обход книги вызывает Python-callback commit_trade на каждую сделку.'),
    edge('market.match_boundary', 'market.match_kernel', 'calls',
         'modules/economy/native/match_boundary.py', 'run_match',
         'inner=run_match → calculate_quantity (kernel)',
         anchor='def run_match'),
    edge('market.trade_boundary', 'market.trade_kernel', 'calls',
         'modules/economy/native/trade_boundary.py', 'apply_trade',
         'calculate_trade (kernel) → apply_trade',
         anchor='_kernel.calculate_trade(prepared, eps)'),
    edge('market.trade_boundary', 'money.cash_store', 'writes',
         'modules/economy/native/trade_boundary.py', 'apply_trade',
         'деньги сделки через cash_store', anchor='def apply_trade'),
    edge('market.report_history', 'market.record_market', 'callback',
         'modules/economy/v3.py', 'Economy.record_market',
         '_report_history.record_market_rows(...)', anchor='record_market_rows('),

    # производство
    edge('engine.monthly_driver', 'production.produce', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.produce(existing, planning_sells)', anchor='engine.produce(existing, planning_sells)'),
    edge('production.produce', 'production.boundary', 'calls',
         'modules/economy/v3.py', 'Economy.produce', '_production.produce(self, existing)',
         anchor='_production.produce'),
    edge('production.boundary', 'production.kernel', 'calls',
         'modules/economy/native/production_boundary.py', 'produce',
         'calculate = production_kernel.calculate_output',
         anchor='_kernel = load_production_kernel()'),

    # потребление
    edge('engine.monthly_driver', 'consumption.household_requests', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.household_requests(...) × basic/extra',
         anchor="engine.household_requests(households, engine.cfg['basic_needs']"),
    edge('engine.monthly_driver', 'consumption.owner_requests', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.owner_requests(sells)', anchor='engine.owner_requests(sells)'),
    edge('engine.monthly_driver', 'consumption.boundary', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         '_consumption.consume_households(...)', anchor='_consumption.consume_households'),
    edge('consumption.boundary', 'consumption.kernel', 'calls',
         'modules/economy/native/consumption_boundary.py', 'consume_households',
         'calculate_consumption (kernel)', anchor='def load_consumption_kernel'),
    edge('engine.monthly_driver', 'consumption.topup', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.household_top_up(...)', anchor='engine.household_top_up'),

    # проекты
    edge('engine.monthly_driver', 'projects.project_requests', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.project_requests(sells) (Python-ветка)', anchor='engine.project_requests(sells)'),
    edge('projects.project_requests', 'projects.delivery_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.project_material_requests',
         '_delivery.request_project_materials(...)', anchor='_delivery.request_project_materials'),
    edge('projects.delivery_boundary', 'projects.material_boundary', 'calls',
         'modules/economy/native/delivery_boundary.py', 'request_project_materials',
         'reserve_materials (закупка материалов)', anchor='reserve_materials('),
    edge('projects.project_requests', 'projects.cancel_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.project_material_requests',
         '_cancel.cancel_stalled_project(...)', anchor='_cancel.cancel_stalled_project'),
    edge('engine.monthly_driver', 'projects.advance_public', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.advance_public_projects()', anchor='engine.advance_public_projects()'),
    edge('projects.advance_public', 'projects.wage_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.advance_public_projects',
         '_wage.advance_public_projects(self)', anchor='_wage.advance_public_projects'),
    edge('engine.monthly_driver', 'projects.finish', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.finish_projects()', anchor='engine.finish_projects()'),
    edge('projects.finish', 'projects.completion_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.finish_projects',
         '_project_completion.finish_projects(self)', anchor='_project_completion.finish_projects'),
    edge('projects.project_slot', 'engine.economy', 'writes',
         'modules/economy/v3.py', 'Economy.apply_investment_project',
         'attach(account, project) — единственная точка записи project',
         anchor='_project_slot.attach(account, project)'),

    # расчёты
    edge('engine.monthly_driver', 'settlement.settle', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.settle(list(enterprises), households)',
         anchor='engine.settle(list(engine.enterprises.values()), households)'),
    edge('settlement.settle', 'settlement.enterprise_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.settle',
         '_enterprise_settlement.settle_month(...)', anchor='_enterprise_settlement.settle_month'),
    edge('settlement.settle', 'money.money_boundary', 'calls',
         'modules/economy/native/enterprise_settlement_boundary.py', 'settle_month',
         'money_boundary.settle(...) — второй цикл', anchor='money_boundary.settle'),
    edge('engine.monthly_driver', 'settlement.dividend_boundary', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.pay_dividends(planning_sells)', anchor='engine.pay_dividends(planning_sells)'),
    edge('settlement.dividend_boundary', 'money.transfer', 'callback',
         'modules/economy/native/dividend_boundary.py', 'pay_dividends',
         'hooks.transfer(...) на каждую выплату', anchor='def transfer(self, sender, recipient, amount, reason)'),
    edge('engine.monthly_driver', 'settlement.demography', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.update_demography()', anchor='engine.update_demography()'),
    edge('settlement.demography', 'settlement.kernel', 'calls',
         'modules/economy/native/settlement_boundary.py', 'run_demography_python',
         'run_demography (kernel) с population_hook', anchor='def run_demography_python'),
    edge('engine.monthly_driver', 'settlement.validate', 'calls',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         'engine.validate(before)', anchor='engine.validate(before)'),
    edge('settlement.validate', 'settlement.boundary', 'calls',
         'modules/economy/v3.py', 'Economy.validate',
         '_settlement.validate(self, EPS)', anchor='_settlement.validate(self, EPS)'),

    # сохранение
    edge('entry.turn_finish', 'save.begin_turn', 'calls',
         'core/turn_service.py', 'TurnService.finish', 'state.begin_turn()',
         anchor='state.begin_turn()'),
    edge('save.begin_turn', 'save.economy_state', 'writes',
         'core/economy_state.py', 'EconomyState.begin_turn',
         'self._undo = engine.export_state()', anchor='self.engine.export_state()'),
    edge('entry.turn_finish', 'save.flush_structural', 'calls',
         'core/turn_service.py', 'TurnService.finish', 'state.flush_structural(self.db)',
         anchor='state.flush_structural(self.db)'),
    edge('save.flush_structural', 'save.record_event', 'writes',
         'core/economy_state.py', 'EconomyState.flush_structural',
         'db.create_entity / db.update_social_groups', anchor='db.update_social_groups('),
    edge('save.world_save', 'save.save_checkpoint', 'calls',
         'core/world_service.py', 'WorldService.save_checkpoint',
         'EconomyState.save_checkpoint(state, destination)',
         anchor='EconomyState.save_checkpoint(state, destination)'),
    edge('save.save_checkpoint', 'save.load_checkpoint', 'writes',
         'core/economy_state.py', 'EconomyState.save_checkpoint',
         'schema keep_reports читается загрузкой',
         anchor='keep_reports=bool(self.keep_reports)'),
    edge('save.economy_state', 'save.save_checkpoint', 'owns',
         'core/economy_state.py', 'EconomyState.save_checkpoint',
         'checkpoint_path/checkpoint_seconds пишет сам EconomyState',
         anchor='self.checkpoint_path = path'),
    edge('entry.lab', 'save.economy_state', 'calls',
         'core/lab.py', 'execute_experiment',
         'state = EconomyState.load_checkpoint(directory/"economy", ...)',
         anchor='EconomyState.load_checkpoint(directory/'),
    edge('entry.lab', 'entry.advance_turn', 'calls',
         'core/lab.py', 'execute_experiment', 'service.advance_turn(...) на каждый месяц',
         anchor="service.advance_turn(command_id=f'lab-"),
    edge('entry.lab', 'save.save_checkpoint', 'calls',
         'core/lab.py', 'execute_experiment',
         'state.save_checkpoint(directory/"economy") — один раз за эксперимент',
         anchor="state.save_checkpoint(directory/'economy')"),
    edge('projects.material_requests', 'projects.cancel_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.project_material_requests',
         '_cancel.cancel_stalled_project(self, a, EPS)',
         anchor='_cancel.cancel_stalled_project(self, a, EPS)'),
    edge('projects.material_requests', 'projects.delivery_boundary', 'calls',
         'modules/economy/v3.py', 'Economy.project_material_requests',
         '_delivery.request_project_materials(...)',
         anchor='_delivery.request_project_materials(self, a, sells, EPS, store, distance)'),
    edge('projects.project_requests', 'projects.material_requests', 'calls',
         'modules/economy/v3.py', 'Economy.project_requests',
         'self.project_material_requests(sells, due, investors)',
         anchor='self.project_material_requests(sells, due, investors)'),
    edge('save.economy_state', 'save.state_step', 'owns',
         'core/economy_state.py', 'EconomyState.step',
         'last_report/reports обновляет EconomyState', anchor='self.last_report = report'),
]


# ---------------------------------------------------------------------------
# Простые объяснения связей
# ---------------------------------------------------------------------------
#
# Тексты для стрелок: «что происходит» и «почему так». Их не 73 штуки вручную —
# объяснение подбирается по виду связи и паре узлов, а для самых важных рёбер
# задано отдельно. Так карта остаётся объяснимой, но не превращается в словарь.

PLAIN_EDGES = {
    ('entry.world_service', 'entry.turn_service'):
        'Мир создаёт прораба хода вместе с собой. Отдельно его никто не заводит.',
    ('entry.advance_turn', 'entry.turn_finish'):
        'Кнопка «следующий месяц» нажата — зовём настоящий ход.',
    ('entry.turn_service', 'entry.economy_phase'):
        'Прораб перечисляет свои этапы; экономика — третий из них.',
    ('entry.economy_phase', 'entry.monthly_phase'):
        'Этап «экономика» просто передаёт слово функции monthly_phase.',
    ('entry.monthly_phase', 'save.state_step'):
        'Просим состояние прожить один месяц.',
    ('save.state_step', 'engine.step'):
        'Состояние передаёт просьбу самому движку.',
    ('engine.step', 'engine.monthly_boundary'):
        'Движок делает единственный вызов месяца — в развилку.',
    ('engine.monthly_boundary', 'engine.monthly_driver'):
        'Развилка подставила быструю реализацию — месяц пойдёт по её плану.',
    ('engine.monthly_driver', 'engine.monthly_hooks'):
        'Быстрый код берёт набор поручений, которые ему придётся звать в Python.',
    ('engine.step', 'money.runtime_window'):
        'Перед месяцем объявляем: касса под охраной, менять её можно только кассиру.',
    ('engine.step', 'save.event_writer'):
        'После месяца отдаём накопленные события фоновому писателю.',
    ('engine.economy', 'money.cash_store'):
        'Движок держит кассира и всегда ходит к деньгам через него.',
    ('engine.economy', 'stock.stock_store'):
        'Движок держит кладовщика: склад меняется только через него.',
    ('engine.economy', 'orders.order_store'):
        'Движок держит регистратора заявок — единственного, кто меняет их поля.',
    ('money.transfer', 'money.cash_store'):
        'Перевод денег просит кассира переложить сумму. Напрямую баланс не трогается.',
    ('market.order', 'orders.order_store'):
        'Новая заявка попадает в список через регистратора.',
    ('market.order', 'money.cash_store'):
        'Покупка сразу откладывает деньги, чтобы их нельзя было потратить дважды.',
    ('market.order', 'stock.stock_store'):
        'Продажа сразу откладывает товар, чтобы его нельзя было продать дважды.',
    ('market.match_boundary', 'market.trade_boundary'):
        'Для каждой найденной пары цикл торгов зовёт проведение сделки обратно в Python. '
        'Это та самая «обратная стрелка», которая стоит денег на каждом шаге.',
    ('money.cash_store', 'money.runtime_window'):
        'Кассир умеет открыть и закрыть месяц охраны кассы.',
    ('production.produce', 'production.boundary'):
        'Вход производства передаёт работу прослойке, которая считает по заводам.',
    ('production.boundary', 'production.kernel'):
        'Прослойка спрашивает у быстрого ядра, какое ограничение самое узкое.',
    ('projects.project_slot', 'engine.economy'):
        'Проект прикрепляется к счёту ровно в одном месте — через маленькую дверь.',
    ('settlement.dividend_boundary', 'money.transfer'):
        'Каждая доля дивидендов выплачивается обычным переводом, а не своей записью в баланс.',
    ('market.report_history', 'market.record_market'):
        'Итоги рынка считает бухгалтерия отчёта, а не сам месяц.',
    ('save.flush_structural', 'save.record_event'):
        'В базу мира уходят только учреждения и население: это структура, а не экономика.',
    ('entry.lab', 'save.save_checkpoint'):
        'Стенд сохраняет экономику один раз в конце прогона, а не после каждого месяца.',
}

EDGE_PLAIN_BY_KIND = {
    'calls': 'Один код зовёт другой, чтобы тот что-то посчитал или сделал.',
    'writes': 'Здесь меняется само хранимое значение — деньги, товар или поле заявки.',
    'owns': 'Объект хранит ссылку на состояние и отвечает за то, чтобы правда была одна.',
    'binds': 'При запуске программы выбрана быстрая реализация вместо запасной.',
    'callback': 'Быстрый код возвращается в Python за функцией. Такие переходы дорогие.',
}


def _edge_plain(item):
    if (item['source'], item['target']) in PLAIN_EDGES:
        return PLAIN_EDGES[(item['source'], item['target'])]
    return EDGE_PLAIN_BY_KIND.get(item['kind'], '')


for _item in EDGES:
    _item['plain'] = _edge_plain(_item)


# ---------------------------------------------------------------------------
# Сценарий месяца
# ---------------------------------------------------------------------------

def step(phase, node_id, call, path, spec, condition=None, anchor=None,
         language='python', note=None):
    return dict(
        phase=phase, node=node_id, call=call, condition=condition,
        language=language, note=note,
        source_location=location(path, spec, anchor=anchor, symbol=call),
    )


PYTHON_STEPS = [
    step('вход', 'entry.advance_turn', 'WorldService.advance_turn',
         'core/world_service.py', 'WorldService.advance_turn',
         anchor='return self.turns.finish'),
    step('вход', 'entry.turn_finish', 'TurnService.finish',
         'core/turn_service.py', 'TurnService.finish', anchor='def finish'),
    step('подготовка', 'save.begin_turn', 'state.begin_turn() — снимок для отката',
         'core/turn_service.py', 'TurnService.finish', anchor='state.begin_turn()'),
    step('подготовка', 'entry.economy_phase', 'фаза economy',
         'core/turn_service.py', 'TurnService._economy', anchor='monthly_phase(self.service, ctx)'),
    step('подготовка', 'entry.monthly_phase', 'monthly_phase → state.step()',
         'modules/economy/v3.py', 'monthly_phase', anchor='report = state.step()'),
    step('подготовка', 'save.state_step', 'EconomyState.step → engine.step',
         'core/economy_state.py', 'EconomyState.step', anchor='self.engine.step'),
    step('подготовка', 'engine.step', 'Economy.step: проверка режимов',
         'modules/economy/v3.py', 'Economy.step', anchor='native_mode not in'),
    step('подготовка', 'money.runtime_window', 'cash_store.begin_runtime()',
         'modules/economy/v3.py', 'Economy.step', anchor='self.cash_store.begin_runtime()'),
    step('подготовка', 'engine.monthly_boundary', 'monthly_boundary.monthly_step(...)',
         'modules/economy/v3.py', 'Economy.step', anchor='monthly_boundary.monthly_step'),
    step('подготовка', 'engine.monthly_hooks', 'hooks = _make_hooks(engine)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks = _make_hooks(engine)', language='cython'),
    step('подготовка', 'engine.monthly_driver', 'activate_rules, reset_report, meta["month"] += 1',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor="engine.meta['month'] += 1", language='cython'),
    step('подготовка', 'engine.monthly_hooks', 'hooks.reset_month_reserves(accounts)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks.reset_month_reserves(engine.accounts)', language='cython'),
    step('подготовка', 'engine.monthly_driver', 'reset_enterprises(existing)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='reset_enterprises(existing)', language='cython'),
    step('подготовка', 'market.sell_orders', 'update_prices()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'off'", anchor='engine.update_prices()', language='cython'),
    step('подготовка', 'engine.economy', 'prepare_recovery_quotes()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode != 'cython'", anchor='engine.prepare_recovery_quotes()'),
    step('подготовка', 'market.sell_orders', 'sells = engine.sell_orders(exclude=lagged_firms)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.sell_orders(exclude=lagged_firms)'),
    step('подготовка', 'consumption.household_requests', 'households = engine.shuffled(...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode != 'cython'", anchor='households = engine.shuffled('),
    step('подготовка', 'consumption.household_requests', 'household_requests(basic_needs)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor="engine.household_requests(households, engine.cfg['basic_needs']"),
    step('подготовка', 'consumption.household_requests', 'household_requests(extra_needs)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor="engine.household_requests(households, engine.cfg['extra_needs']"),
    step('подготовка', 'consumption.owner_requests', 'owner_requests(sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.owner_requests(sells)'),
    step('подготовка', 'engine.economy', 'recover_enterprises(sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode != 'cython'", anchor='engine.recover_enterprises(sells)'),
    step('подготовка', 'projects.start_public', 'public_project_requests(sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode != 'cython'", anchor='engine.public_project_requests(sells)'),
    step('подготовка', 'projects.project_requests', 'project_requests(sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode != 'cython'", anchor='engine.project_requests(sells)'),
    step('планирование', 'engine.economy', 'plan_enterprises(existing, sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.plan_enterprises(existing, sells)'),
    step('рынок', 'market.match', 'match(buy, sells, local_only=True) для базовых нужд',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.match(buy, sells, local_only=True)'),
    step('рынок', 'market.match_boundary', '_match.run_buyers(engine, shuffled(buys), ...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_match.run_buyers'),
    step('рынок', 'market.trade_boundary', 'commit_trade на каждую сделку (callback)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_trade.commit_trade'),
    step('рынок', 'consumption.topup', 'household_top_up(households, sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.household_top_up(households, sells)'),
    step('рынок', 'market.record_market', 'record_market()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.record_market()'),
    step('рынок', 'engine.monthly_hooks', 'hooks.expire_orders()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks.expire_orders()'),
    step('производство', 'production.produce', 'produce(existing, planning_sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.produce(existing, planning_sells)'),
    step('проекты', 'projects.advance_public', 'advance_public_projects()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.advance_public_projects()'),
    step('проекты', 'projects.finish', 'finish_projects()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.finish_projects()'),
    step('потребление', 'consumption.owner_boundary', 'consume_owner_purchases()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.consume_owner_purchases()'),
    step('потребление', 'consumption.boundary', '_consumption.consume_households(...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_consumption.consume_households'),
    step('отчёт', 'market.report_history', 'update_sales_history / update_stock_sales_history',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_report_history.update_sales_history(engine, eps)'),
    step('расчёты', 'engine.monthly_hooks', 'hooks.protect_balances(accounts)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks.protect_balances(engine.accounts)'),
    step('расчёты', 'settlement.settle', 'settle(enterprises, households)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.settle(list(engine.enterprises.values()), households)'),
    step('расчёты', 'settlement.dividend_boundary', 'pay_dividends(planning_sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.pay_dividends(planning_sells)'),
    step('расчёты', 'settlement.demography', 'update_demography()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.update_demography()'),
    step('расчёты', 'engine.economy', 'apply_migration()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.apply_migration()'),
    step('расчёты', 'engine.monthly_hooks', 'hooks.apply_project_reserves(...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks.apply_project_reserves'),
    step('отчёт', 'engine.economy', 'summarize(prior_cash)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="detail != 'none'", anchor='engine.summarize(prior_cash)'),
    step('отчёт', 'market.report_history', 'build_report_aggregates(...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="detail != 'none'", anchor='_report_history.build_report_aggregates'),
    step('отчёт', 'save.event_recorder', '_record_report_channels(engine, detail)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_record_report_channels(engine, detail)'),
    step('отчёт', 'settlement.validate', 'validate(before)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.validate(before)'),
    step('отчёт', 'engine.economy', 'build_causal_events()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="detail == 'deep'", anchor='engine.build_causal_events()'),
    step('выход', 'money.runtime_window', 'cash_store.end_runtime(check=completed)',
         'modules/economy/v3.py', 'Economy.step', anchor='self.cash_store.end_runtime(check=completed)'),
    step('выход', 'save.event_writer', '_event_writer.drain_recorder(...)',
         'modules/economy/v3.py', 'Economy.step', anchor='_event_writer.drain_recorder'),
    step('выход', 'save.state_step', 'last_report = report; reports.append(...)',
         'core/economy_state.py', 'EconomyState.step', anchor='self.last_report = report'),
    step('отчёт', 'entry.monthly_phase', 'db.record_event(economy_month)',
         'modules/economy/v3.py', 'monthly_phase', anchor='service.db.record_event'),
    step('сохранение', 'save.flush_structural', 'state.flush_structural(self.db)',
         'core/turn_service.py', 'TurnService.finish', anchor='state.flush_structural(self.db)'),
    step('сохранение', 'entry.turn_finish', 'save_turn_result + событие turn_advanced',
         'core/turn_service.py', 'TurnService.finish', anchor='save_turn_result'),
    step('сохранение', 'save.autosave', '_autosave(state) — по умолчанию выключен',
         'core/turn_service.py', 'TurnService.finish', anchor='self._autosave(state)'),
    step('сохранение', 'save.world_save', 'явное сохранение: WorldService.save_checkpoint',
         'core/world_service.py', 'WorldService.save_checkpoint',
         anchor='EconomyState.save_checkpoint(state, destination)'),
    step('сохранение', 'save.save_checkpoint', 'EconomyState.save_checkpoint → Parquet',
         'core/economy_state.py', 'EconomyState.save_checkpoint',
         anchor='self.checkpoint_path = path'),
]

CYTHON_STEPS = [
    step('вход', 'entry.advance_turn', 'WorldService.advance_turn',
         'core/world_service.py', 'WorldService.advance_turn',
         anchor='return self.turns.finish'),
    step('вход', 'entry.turn_finish', 'TurnService.finish',
         'core/turn_service.py', 'TurnService.finish', anchor='def finish'),
    step('подготовка', 'save.begin_turn', 'state.begin_turn()',
         'core/turn_service.py', 'TurnService.finish', anchor='state.begin_turn()'),
    step('подготовка', 'engine.step', 'Economy.step: native_mode == "cython"',
         'modules/economy/v3.py', 'Economy.step', anchor='load_serial_step()'),
    step('подготовка', 'engine.monthly_boundary', 'require_native_monthly()',
         'modules/economy/v3.py', 'Economy.step',
         anchor='monthly_boundary.require_native_monthly()'),
    step('подготовка', 'money.runtime_window', 'cash_store.begin_runtime()',
         'modules/economy/v3.py', 'Economy.step', anchor='self.cash_store.begin_runtime()'),
    step('подготовка', 'engine.monthly_driver', 'monthly_step(engine, ...) — Cython',
         'modules/economy/v3.py', 'Economy.step', anchor='monthly_boundary.monthly_step'),
    step('подготовка', 'engine.monthly_hooks', 'hooks = _make_hooks(engine)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks = _make_hooks(engine)', language='cython'),
    step('подготовка', 'engine.monthly_hooks', 'hooks.reset_month_reserves(accounts)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks.reset_month_reserves(engine.accounts)', language='cython'),
    step('подготовка', 'stock.stock_store', 'stock_store.sync()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'cython'", anchor='engine.stock_store.sync()'),
    step('подготовка', 'orders.world_state', 'world.begin_step(engine)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'cython'", anchor='world.begin_step(engine)'),
    step('подготовка', 'engine.serial_step', 'run_native_public_projects(engine) — один pack/call/apply',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'cython'", anchor='run_native_public_projects(engine)'),
    step('подготовка', 'engine.serial_step', 'public_project_preparation(data, rng)',
         'modules/economy/native/update_prices_boundary.py', 'run_native_public_projects',
         anchor='native.public_project_preparation(data, engine.rng)'),
    step('подготовка', 'market.sell_orders', 'sells = native_market["sells"]',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'cython'", anchor="sells = engine.native_market['sells']"),
    step('подготовка', 'consumption.household_requests', 'households = native_households["households"]',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'cython'", anchor="engine.native_households['households']"),
    step('подготовка', 'projects.project_requests', 'run_native_project_requests(engine, sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'cython'", anchor='run_native_project_requests(engine, sells)'),
    step('планирование', 'engine.economy', 'plan_enterprises(existing, sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.plan_enterprises(existing, sells)'),
    step('рынок', 'market.match_boundary', '_match.run_buyers(engine, shuffled(buys), ...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_match.run_buyers'),
    step('рынок', 'market.trade_boundary', 'commit_trade на каждую сделку (callback)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_trade.commit_trade'),
    step('рынок', 'market.record_market', 'record_market()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.record_market()'),
    step('рынок', 'engine.monthly_hooks', 'hooks.expire_orders()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='hooks.expire_orders()'),
    step('производство', 'production.boundary', 'produce(existing, planning_sells) → production_kernel',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.produce(existing, planning_sells)'),
    step('проекты', 'projects.advance_public', 'advance_public_projects()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.advance_public_projects()'),
    step('проекты', 'projects.finish', 'finish_projects()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.finish_projects()'),
    step('потребление', 'consumption.boundary', '_consumption.consume_households(...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_consumption.consume_households'),
    step('расчёты', 'settlement.settle', 'settle(...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.settle(list(engine.enterprises.values()), households)'),
    step('расчёты', 'settlement.dividend_boundary', 'pay_dividends(planning_sells)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.pay_dividends(planning_sells)'),
    step('расчёты', 'settlement.demography', 'update_demography()',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.update_demography()'),
    step('расчёты', 'settlement.boundary', 'apply_migration() → run_migration',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.apply_migration()'),
    step('отчёт', 'market.report_history', 'build_report_aggregates(...)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="detail != 'none'", anchor='_report_history.build_report_aggregates'),
    step('отчёт', 'settlement.validate', 'validate(before)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='engine.validate(before)'),
    step('отчёт', 'save.event_recorder', '_record_report_channels(engine, detail)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         anchor='_record_report_channels(engine, detail)'),
    step('выход', 'orders.world_state', 'world.end_step(engine, completed=completed)',
         'modules/economy/native/monthly_driver.pyx', 'monthly_step',
         condition="native_mode == 'cython'", anchor='world.end_step(engine, completed=completed)'),
    step('выход', 'money.runtime_window', 'cash_store.end_runtime(check=completed)',
         'modules/economy/v3.py', 'Economy.step', anchor='self.cash_store.end_runtime(check=completed)'),
    step('выход', 'save.state_step', 'last_report = report',
         'core/economy_state.py', 'EconomyState.step', anchor='self.last_report = report'),
    step('сохранение', 'save.flush_structural', 'state.flush_structural(self.db)',
         'core/turn_service.py', 'TurnService.finish', anchor='state.flush_structural(self.db)'),
    step('сохранение', 'save.autosave', '_autosave(state)',
         'core/turn_service.py', 'TurnService.finish', anchor='self._autosave(state)'),
    step('сохранение', 'save.save_checkpoint', 'EconomyState.save_checkpoint → Parquet',
         'core/economy_state.py', 'EconomyState.save_checkpoint',
         anchor='self.checkpoint_path = path'),
]


SCENARIOS = [
    dict(
        id='month', title='Месячный ход',
        description=('Порядок вызовов одного месяца: вход, подготовка, производство, рынок, '
                     'потребление, проекты, зарплаты/дивиденды, settlement, отчёт, сохранение. '
                     'Шаги взяты из реального кода драйвера месяца и TurnService; условия — '
                     'из тех же ветвей, что в коде.'),
        variants=[
            dict(id='python', title='Python-эталон (native_mode="off")',
                 summary=('Каждая фаза месяца вызывается из Cython-драйвера отдельным Python-вызовом. '
                          'Драйвер тот же: monthly_boundary подменяет monthly_step на расширение, '
                          'поэтому «Python-режим» — это режим фаз, а не режим оркестрации.'),
                 steps=PYTHON_STEPS,
                 divergences=[
                     'Фазы подготовки рынка идут по отдельности: update_prices → prepare_recovery_quotes → sell_orders → household_requests → owner_requests → recover_enterprises → public_project_requests → project_requests.',
                     'WorldState не создаётся и world.begin_step/end_step не вызываются (world = None).',
                     'stock_store.sync() не вызывается: склад берётся из живого dict.',
                     'Проектные запросы считает Python: engine.project_requests(sells).',
                 ]),
            dict(id='cython', title='Cython (native_mode="cython")',
                 summary=('Подготовка рынка выполняется одним packed-шагом serial_step '
                          '(public_project_preparation) с numpy-буферами, затем результат применяется '
                          'теми же Python-границами. Оркестрация и до/после — тот же monthly_driver.'),
                 steps=CYTHON_STEPS,
                 divergences=[
                     'run_native_public_projects собирает update_prices, recovery, sell_orders, household_requests, owner_requests, recover_enterprises и public_project_requests в один pack/call/apply.',
                     'Создаётся WorldState: begin_step до фаз и end_step в finally.',
                     'stock_store.sync() вызывается после reset_month_reserves.',
                     'Проектные запросы считает сериализованное ядро: serial_step.native_project_requests.',
                     'Перед месяцем require_native_monthly() падает, если расширение не собрано (fallback запрещён).',
                 ]),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Нарушения
# ---------------------------------------------------------------------------

# Расшифровка видов нарушений: что означает каждое.
VIOLATION_KIND_TITLES = {
    'direct_write': 'Запись мимо владельца',
    'ownerless_state': 'Состояние без владельца',
    'callback': 'Обратный вызов из быстрого кода',
    'boundary_leak': 'Протечка границы между слоями',
}

VIOLATION_KIND_EXPLANATIONS = {
    'direct_write': 'Кто-то меняет поле напрямую в словаре, хотя у этого поля есть хозяин. '
                    'Пока работает, но никто не проверяет, что хозяин об этом знает.',
    'ownerless_state': 'Поле меняют несколько мест без единого хозяина. '
                       'Каждое место уверено, что оно главное.',
    'callback': 'Быстрый код на Cython зовёт функцию Python. Это законно, но медленно — '
                'каждый такой переход стоит времени, а в цикле умножается на число сделок.',
    'boundary_leak': 'Один слой знает о внутренностях другого. Пока держится на честном слове.',
}


def violation(identity, title, kind, severity, state, description, path, spec,
              anchor=None, mitigation=None, related=(), confidence='confirmed',
              plain=None, why=None):
    return dict(
        id=identity, title=title, kind=kind, severity=severity, state=state,
        description=description, mitigation=mitigation, related_nodes=list(related),
        confidence=confidence,
        source_location=location(path, spec, anchor=anchor, symbol=title),
        plain=plain or '',
        why=why or '',
        kind_title=VIOLATION_KIND_TITLES.get(kind, kind),
        kind_explanation=VIOLATION_KIND_EXPLANATIONS.get(kind, ''),
    )


VIOLATIONS = [
    violation('v-public-projects-direct', 'Муниципальные стройки пишутся в счёт напрямую',
              'ownerless_state', 'medium', 'projects',
              'Список public_projects на счёте заменяется присваиванием: единого владельца (store) '
              'у этого состояния нет, в отличие от cash/stock/orders.',
              'modules/economy/v3.py', 'Economy.start_public_project',
              anchor="account['public_projects'] = [project]",
              mitigation='Ввести ProjectStore или point-запись через project_slot для public_projects.',
              related=['projects.start_public']),
    violation('v-public-projects-wage', 'Стройка переписывает public_projects из wage_boundary',
              'direct_write', 'medium', 'projects',
              'wage_boundary подменяет список public_projects на счёте дважды за шаг, а также '
              'правит поля проекта (work_done, status) прямо в словаре.',
              'modules/economy/native/wage_boundary.py', 'WageHooks.begin',
              anchor="account['public_projects'] = self.remaining",
              mitigation='Та же точка записи, что и для инвестиционных проектов.',
              related=['projects.advance_public', 'projects.wage_boundary']),
    violation('v-project-fields', 'Поля проекта меняются без владельца',
              'direct_write', 'medium', 'projects',
              'work_done и status проекта обновляются прямо в dict из wage_boundary; '
              'attach/detach из project_slot закрывает только ссылку на проект целиком.',
              'modules/economy/native/wage_boundary.py', 'run_advance_native',
              anchor="project['work_done'] += workers",
              mitigation='Отдельный API проекта с методами progress/complete.',
              related=['projects.wage_boundary']),
    violation('v-production-plan-direct', 'Производственный план пишется прямо в предприятие',
              'direct_write', 'medium', 'production',
              'plan_boundary присваивает e["production_plan"] и вторую форму плана напрямую: '
              'владельца у плана нет, чтение и запись разнесены по границам.',
              'modules/economy/native/plan_boundary.py', 'PlanHooks.commit_plan',
              anchor="e['production_plan'] = plan",
              mitigation='Вынести план в отдельный store/структуру с единственной точкой записи.',
              related=['projects.project_requests']),
    violation('v-production-result-direct', 'Результат выпуска пишется прямо в предприятие',
              'direct_write', 'low', 'production',
              'production_boundary обновляет status/workers/output/production_cost/unit_cost/wages '
              'напрямую в dict предприятия; отчётные строки при этом идут через хуки.',
              'modules/economy/native/production_boundary.py', 'run_enterprises',
              anchor="e.update(status='active', workers=workers",
              mitigation='Владелец у числовых полей предприятия или явный API записи.',
              related=['production.boundary']),
    violation('v-order-metadata-direct', 'Метаданные заявки пишутся мимо OrderStore',
              'direct_write', 'low', 'orders',
              'project_id, reserved_before_top_up, superseded_by_top_up и reserved_at_clearing '
              'присваиваются прямо в словарь заявки. OrderStore защищает колонки '
              'status/remaining/price/quantity/reserved, но не произвольные метаданные.',
              'modules/economy/v3.py', 'Economy.project_requests',
              anchor="order['project_id'] = project['id']",
              mitigation='Использовать OrderStore.set_metadata для всех полей заявки.',
              related=['market.order', 'market.cancel_order']),
    violation('v-unmet-solvent-two-writers', 'unmet_solvent пишется и ядром, и Python-эталоном',
              'direct_write', 'low', 'orders',
              'Одно и то же поле заявки пишет и report_history_kernel (native), и эталонная '
              'Python-граница. Работает одна ветка, но два писателя одного поля повышают риск расхождения.',
              'modules/economy/native/report_history_boundary.py', 'record_market_rows_python',
              anchor="o['unmet_solvent'] = min(o['remaining']",
              mitigation='Оставить один путь записи поля, второй — только расчётом.',
              related=['market.report_history']),
    violation('v-trades-from-reports', 'Сделки в checkpoint зависят от хранения отчётов',
              'boundary_leak', 'medium', 'save',
              'Таблица trades строится из reports[-1:] или last_report: если история отчётов '
              'не хранится и последнего отчёта нет, сделки в файл не попадут. Состояние сохранения '
              'зависит от режима хранения истории.',
              'core/economy_state.py', 'EconomyState._trade_rows',
              anchor="for report in (self.reports[-1:] or ([self.last_report]",
              mitigation='Писать сделки отдельным полем состояния, а не выводить из отчёта.',
              related=['save.save_checkpoint', 'save.state_step']),
    violation('v-stock-values-dict-only', 'Стоимость запасов пишется только в dict',
              'direct_write', 'low', 'stock',
              'У stock и stock_reserved в native-хранилище есть колонки, а values пишется прямо в '
              'dict счёта через set_value (и в Python-fallback, и в ядре). Асимметрия внутри одного владельца.',
              'modules/economy/native/stock_store_boundary.py', 'StockStorePython.set_value',
              anchor="self.accounts[aid]['values'][good] = value",
              mitigation='Добавить колонку values по аналогии со stock, либо задокументировать как зеркало.',
              related=['stock.stock_store']),
    violation('v-account-creation', 'Счёт создаётся прямым словарём',
              'direct_write', 'info', 'money',
              'EconomyServices.account выставляет balance/stock/values при создании счёта, когда '
              'владельцев ещё нет. Это граница импорта/создания, а не runtime-путь.',
              'modules/economy/support.py', 'EconomyServices.account',
              anchor="dict(id=identity, kind=kind, balance=float(balance)",
              mitigation='Отдельный конструктор счёта, проходящий через владельцев после создания.',
              related=['money.cash_store', 'stock.stock_store']),
    violation('v-match-driver-callbacks', 'match_driver содержит callbacks, но не является входом',
              'callback', 'low', 'market',
              'В собранном match_driver.pyx внешний цикл вызывает Python-callback inner и commit '
              'на каждого покупателя, однако в production-пути run_buyer/run_buyers привязаны к '
              'Python-версиям: модуль остаётся только для диагностики.',
              'modules/economy/native/match_driver.pyx', 'monthly_step',
              anchor='inner(owner, buy, options, sells, eps, cash, store, commit)',
              mitigation='Либо включить драйвер как вход, либо удалить из сборки.',
              related=['market.match_boundary']),
    violation('v-callback-quote', 'quote_kernel вызывает Python-хуки на каждую котировку',
              'callback', 'info', 'market',
              'reference_hook/landed_hook/stock_hook/quoted_hook/signal_hook вызываются из Cython '
              'внутри горячего цикла: это осознанная граница, но каждый вызов — переход C→Python.',
              'modules/economy/native/quote_kernel.pyx', 'quoted_cost',
              anchor='reference = reference_hook()',
              mitigation='Кэшировать результаты хуков на месяц либо считать их нативно.',
              related=['market.match_boundary']),
    violation('v-callback-money', 'money_kernel вызывает Python-хуки денег и склада',
              'callback', 'info', 'money',
              'stock_hook/quote_hook/population_hook/balance_hook/free_cash_hook/transfer_hook/'
              'distribute_hook — переходы в Python из native-расчёта.',
              'modules/economy/native/money_kernel.pyx', 'project_cash_reserve',
              anchor='missing = _max0(quantity-stock_hook(good)-ordered)',
              mitigation='Читать нужные величины заранее пакетом (как в serial_step).',
              related=['money.money_kernel']),
    violation('v-callback-demand', 'demand_kernel вызывает Python-хуки книги',
              'callback', 'info', 'market',
              'component_hook/landed_hook/account_hook/offers_hook вызываются из Cython при обходе '
              'заявок и предложений.',
              'modules/economy/native/demand_kernel.pyx', 'reachable_demand',
              anchor="component = component_hook(order['tile_id'])",
              mitigation='Передавать готовые индексы, как это сделано для update_prices.',
              related=['market.report_history']),
    violation('v-callback-demography', 'settlement_kernel зовёт population_hook по каждому домохозяйству',
              'callback', 'info', 'settlement',
              'run_demography читает население через Python-callback в цикле по домохозяйствам '
              'каждой провинции.',
              'modules/economy/native/settlement_kernel.pyx', 'run_demography',
              anchor='population_hook(account)',
              mitigation='Передавать численность массивом в native-вход.',
              related=['settlement.kernel']),
    violation('v-callback-monthly', 'Драйвер месяца вызывает Python-хуки и фазы',
              'callback', 'info', 'engine',
              'monthly_driver собирает хуки месяца и вызывает Python-фазы: единственный обратный '
              'переход Cython→Python внутри месяца, но их столько же, сколько фаз осталось в Python.',
              'modules/economy/native/monthly_driver.pyx', 'monthly_step',
              anchor='cdef object _call(object fn, object engine)',
              mitigation='Переносить фазы по одной, сохраняя parity-тесты.',
              related=['engine.monthly_driver', 'engine.monthly_hooks']),
    violation('v-callback-trade', 'Сделка — Python-callback на каждую пару заявок',
              'callback', 'info', 'market',
              'Обход книги остаётся в Python, commit_trade вызывается на каждую сделку; '
              'нативной считается только арифметика объёма.',
              'modules/economy/native/match_boundary.py', 'run_buyers_python',
              anchor='run_buyer_python(owner, buy, sells, eps, commit, inner, local_only)',
              mitigation='Пакетный native-обход книги с журналом сделок, как в serial_step.',
              related=['market.match_boundary']),
    violation('v-worldstate-scope', 'WorldState существует только в native-месяце',
              'boundary_leak', 'info', 'engine',
              'Контейнер WorldState создаётся лениво и открывается только при native_mode="cython": '
              'в Python-режиме его нет, поэтому «единый контейнер» не является общим для обоих режимов.',
              'modules/economy/native/monthly_driver.pyx', 'monthly_step',
              anchor="world = engine.world_state if native_mode == 'cython' else None",
              mitigation='Либо создавать контейнер всегда, либо явно описать его как native-only.',
              related=['orders.world_state']),
]


# ---------------------------------------------------------------------------
# Простые объяснения нарушений
# ---------------------------------------------------------------------------

PLAIN_VIOLATIONS = {
    'v-public-projects-direct': (
        'Список городских строек лежит прямо на счёте, и его подменяют присваиванием. '
        'Хозяина у него нет.',
        'Если завтра кто-то добавит ещё одну запись в это поле, никто не заметит. '
        'У денег и склада такой хозяин есть, а у строек — нет.'),
    'v-public-projects-wage': (
        'Код зарплат сам переписывает список строек на счёте, хотя это не его работа.',
        'Получается два места, которые считают себя главными. Работает, пока они согласны.'),
    'v-project-fields': (
        'Прогресс и статус стройки меняют прямо в словаре проекта, без общей двери.',
        'У самого проекта нет хозяина — только у ссылки на него. Поля может менять любой, кто до них дотянется.'),
    'v-production-plan-direct': (
        'План производства записывают прямо в предприятие, без отдельного хранилища.',
        'Читают план в одном месте, пишут в другом. Легко забыть обновить оба.'),
    'v-production-result-direct': (
        'Результаты выпуска (сколько сделали, за сколько, сколько заплатили рабочим) '
        'пишутся прямо в предприятие.',
        'Отчётные строки идут через аккуратные функции, а числовые поля — напрямую. Разнобой.'),
    'v-order-metadata-direct': (
        'У заявки есть защищённые поля (статус, остаток), а есть «приписки» вроде номера проекта, '
        'которые пишут напрямую.',
        'Регистратор заявок следит только за главными полями. Приписки может испортить кто угодно.'),
    'v-unmet-solvent-two-writers': (
        'Поле «сколько спроса осталось без денег» пишут две реализации — быстрая и медленная.',
        'Работает только одна, но если их поведение разойдётся, сравнивать отчёты станет нельзя.'),
    'v-trades-from-reports': (
        'Список сделок для сохранения берётся из отчёта, а отчёты хранятся не всегда.',
        'Если история отчётов отключена и последнего отчёта нет — сделки в файл просто не попадут. '
        'Сохранение зависит от режима, в котором его делали.'),
    'v-stock-values-dict-only': (
        'Количество на складе защищено, а стоимость партии пишется просто в словарь.',
        'Внутри одного хозяина получилась дыра: половина полей под охраной, половина нет.'),
    'v-account-creation': (
        'Новый счёт собирается прямым словарём, потому что хозяев ещё не существует.',
        'Это честная граница создания, а не обход в середине месяца. Помечено как справка.'),
    'v-match-driver-callbacks': (
        'Есть собранный быстрый модуль торгов, который на каждого покупателя зовёт Python — '
        'но в рабочей игре он не используется.',
        'Если его когда-нибудь включат, торги станут не быстрее, а медленнее. Пока это просто балласт.'),
    'v-callback-quote': (
        'Быстрый расчёт цены то и дело возвращается в Python, чтобы спросить про склад и маршрут.',
        'Каждый такой возврат — переход между языками. В цикле по всем заявкам их набираются тысячи.'),
    'v-callback-money': (
        'Калькулятор денег спрашивает Python про склад, свободную кассу и переводы.',
        'Считает быстро, а данные получает медленно. Выигрыш съедается на каждом вопросе.'),
    'v-callback-demand': (
        'Быстрый обход спроса зовёт Python за предложениями и маршрутами.',
        'То же самое: арифметика на Cython, а половина входов приходит из Python.'),
    'v-callback-demography': (
        'Быстрая демография спрашивает Python численность каждого домохозяйства.',
        'Цикл по провинциям и семьям, и в каждом шаге — возврат в Python.'),
    'v-callback-monthly': (
        'Драйвер месяца сам на Cython, но большинство этапов всё равно зовут Python.',
        'Переход на Cython сделан ради порядка и единой точки входа, а не ради скорости. '
        'Это осознанное решение, а не недоделка.'),
    'v-callback-trade': (
        'Торги идут в Python, и каждая сделка — отдельный вызов из цикла.',
        'Пока это самый дорогой участок месяца: сделок много, и каждая обходится в переход.'),
    'v-worldstate-scope': (
        'Сборный ящик со ссылками существует только в быстром режиме месяца.',
        'В медленном режиме его нет, поэтому «единое хранилище для всех» — не совсем правда.'),
}

for _item in VIOLATIONS:
    _plain = PLAIN_VIOLATIONS.get(_item['id'])
    if _plain is None:
        raise SystemExit('НЕТ ПРОСТОГО ОБЪЯСНЕНИЯ ДЛЯ НАРУШЕНИЯ: ' + _item['id'])
    _item['plain'], _item['why'] = _plain


# ---------------------------------------------------------------------------
# Владельцы состояния
# ---------------------------------------------------------------------------

OWNERS = [
    dict(
        id='cash', title='Деньги счёта (balance / reserved / protected)',
        state='balance, reserved, protected на каждом счёте',
        storage='engine.accounts[*] (dict-зеркало) + колонки CashStore (истина)',
        owner_node='money.cash_store', fallback_node='money.cash_store_python',
        write_path='Economy.cash_store → CashStore.set_balance/add_balance/move_balance/'
                   'set_reserved_cash/add_reserved_cash/release_reserved_cash/'
                   'set_protected_cash/add_protected_cash/release_protected_cash',
        readers=['money.transfer', 'money.money_boundary', 'settlement.dividend_boundary',
                 'production.boundary', 'projects.material_boundary'],
        writers=['money.cash_store', 'money.transfer'],
        direct_writes=['v-account-creation'],
        callbacks=[],
        confidence='confirmed',
        notes='Внутри месяца окно владения открыто: запись в dict мимо store падает ValueError. '
              'Вне окна расхождение принимается как импорт и считается в adopt_mismatch.',
        evidence=[
            'tests/test_economy_native_cash_runtime.py::test_runtime_month_has_no_direct_cash_writers',
            'tests/test_economy_native_cash_ownership.py::test_balance_reserved_protected_share_one_owner',
            'tests/test_economy_native_cash_runtime.py::test_dict_write_inside_runtime_is_rejected',
        ],
    ),
    dict(
        id='stock', title='Склад (stock / stock_reserved / values)',
        state='stock, stock_reserved, values (учётная стоимость) по счёту и товару',
        storage='engine.accounts[*]["stock"|"stock_reserved"|"values"] + StockStore',
        owner_node='stock.stock_store', fallback_node='stock.stock_store_python',
        write_path='Economy.stock_store → set_stock/add_stock/set_value/set_reserved/'
                   'add_reserved/release_reserved/reset_reserved/restore_good',
        readers=['production.boundary', 'projects.material_boundary', 'market.order',
                 'consumption.boundary'],
        writers=['stock.stock_kernel', 'stock.stock_store'],
        direct_writes=['v-stock-values-dict-only'],
        callbacks=['v-callback-money'],
        confidence='confirmed',
        notes='Количества и резервы идут через колонки стора; values пишется только зеркалом в dict.',
        evidence=[
            'tests/test_economy_native_stock_ownership.py::test_stock_store_uses_live_dicts',
            'tests/test_economy_native_stock_ownership.py::test_stock_store_release_matches_previous_max_semantics',
        ],
    ),
    dict(
        id='orders', title='Заявка (status / remaining / price / quantity / reserved)',
        state='поля заявки в report["orders"] текущего месяца',
        storage='engine.report["orders"] — живой список словарей на один отчётный месяц',
        owner_node='orders.order_store', fallback_node='orders.order_store_python',
        write_path='Economy.order_store → append/remove/set_remaining/set_reserved/set_status/'
                   'set_price/set_quantity/set_metadata',
        readers=['market.match_boundary', 'market.record_market', 'engine.monthly_hooks',
                 'settlement.dividend_boundary'],
        writers=['market.order', 'market.cancel_order', 'market.trade_boundary'],
        direct_writes=['v-order-metadata-direct', 'v-unmet-solvent-two-writers'],
        callbacks=['v-callback-trade', 'v-match-driver-callbacks'],
        confidence='confirmed',
        notes='Store перепроверяется по тождеству списка: после reset_report ссылка берётся заново.',
        evidence=[
            'tests/test_economy_native_order_ownership.py::test_audit_detects_direct_dict_write',
            'tests/test_economy_native_order_ownership.py::test_trade_writes_both_orders_through_store',
        ],
    ),
    dict(
        id='projects', title='Проекты (project / public_projects)',
        state='account["project"] (инвестиционный) и account["public_projects"] (муниципальные)',
        storage='engine.accounts[*]["project"|"public_projects"] — словари/список на счёте',
        owner_node='projects.project_slot', fallback_node=None,
        write_path='project_slot.attach/detach — только для ссылки на инвестиционный проект; '
                   'поля проекта меняются множеством прямых записей',
        readers=['projects.project_requests', 'projects.wage_boundary',
                 'projects.completion_boundary', 'projects.cancel_boundary',
                 'projects.delivery_boundary'],
        writers=['projects.project_slot', 'projects.wage_boundary', 'projects.start_public'],
        direct_writes=['v-public-projects-direct', 'v-public-projects-wage', 'v-project-fields'],
        callbacks=[],
        confidence='confirmed',
        notes='Владельца нет: это единственный из ключевых доменов без store. attach/detach '
              'закрывает только саму ссылку, не её поля.',
        evidence=[
            'tests/test_economy_native_projects.py',
            'tests/test_economy_owners.py::test_project_material_assignment_keeps_surplus_offer',
        ],
    ),
    dict(
        id='production_results', title='Результаты выпуска',
        state='report["production"|"inputs_used"|"equipment_wear"] и поля предприятия '
              '(output, production_cost, unit_cost, wages, status, production_plan)',
        storage='engine.report текущего месяца + engine.enterprises[*]',
        owner_node='production.boundary', fallback_node=None,
        write_path='production_boundary.run_enterprises → hooks.production/input_used/equipment_worn '
                   'для отчёта; числовые поля предприятия пишутся напрямую',
        readers=['market.report_history', 'settlement.enterprise_boundary', 'save.save_checkpoint'],
        writers=['production.boundary', 'projects.wage_boundary'],
        direct_writes=['v-production-result-direct', 'v-production-plan-direct'],
        callbacks=['v-callback-quote', 'v-callback-money'],
        confidence='confirmed',
        notes='Отчётные строки — хуки, поля предприятия — прямая запись; единого владельца нет.',
        evidence=[
            'tests/test_economy_native_production.py',
            'tests/test_economy_native_plan.py',
        ],
    ),
    dict(
        id='reports_save', title='Отчёты и checkpoint',
        state='EconomyState.reports/last_report, Parquet-таблицы, schema.json',
        storage='память EconomyState + каталог checkpoint (Parquet, zstd)',
        owner_node='save.economy_state', fallback_node=None,
        write_path='EconomyState.save_checkpoint (staging → previous → replace), '
                   'load_checkpoint (проверка schema_version, world fingerprint, keep_reports)',
        readers=['entry.monthly_phase', 'entry.lab', 'save.load_checkpoint'],
        writers=['save.save_checkpoint'],
        direct_writes=['v-trades-from-reports'],
        callbacks=[],
        confidence='confirmed',
        notes='В Ladybug из экономики попадают только Institution и SocialGroup(population, welfare) '
              'через flush_structural; остальное — Parquet по явному сохранению.',
        evidence=[
            'tests/test_economy_state.py::test_checkpoint_round_trip_keeps_state_exactly',
            'tests/test_economy_state.py::test_save_load_continues_next_turn_exactly',
            'tests/test_economy_state.py::test_autosave_is_off_by_default',
        ],
    ),
]


# ---------------------------------------------------------------------------
# Простые объяснения владельцев
# ---------------------------------------------------------------------------

PLAIN_OWNERS = {
    'cash': (
        'Деньги на каждом счёте: сколько есть (balance), сколько уже отложено под заявки '
        '(reserved) и сколько защищено под обязательства (protected).',
        'Внутри месяца деньги меняет только кассир — CashStore. Если кто-то полезет в словарь '
        'напрямую, программа упадёт с ошибкой «изменён в обход владельца». Это сделано специально: '
        'так два разных места не могут одновременно решить, сколько у счёта денег.',
        'Вне месяца (загрузка сейва, откат хода) расхождение разрешено — это честный импорт '
        'старого состояния, и он считается в счётчике adopt_mismatch.'),
    'stock': (
        'Склад каждого счёта: сколько товара лежит (stock), сколько уже обещано под заявки '
        '(stock_reserved) и во сколько обошлась партия (values).',
        'Кладовщик StockStore — единственный хозяин количества и резервов. Стоимость партии '
        'пишется мимо колонок, просто в словарь: это дыра внутри того же хозяина.',
        'Разница важна на практике: количество защищено от двойного списания, а стоимость '
        'можно случайно перезаписать не тем числом.'),
    'orders': (
        'Заявки текущего месяца: кто, что, сколько, по какой цене и в каком статусе.',
        'Регистратор OrderStore держит статус, остаток, цену и резерв. Он же перепроверяет, '
        'что работает с заявками именно этого месяца: после смены отчёта ссылка берётся заново.',
        'Но у заявки есть и «приписки» — номер проекта, отметка о повторной покупке. Их пишут '
        'напрямую, и регистратор о них не знает.'),
    'projects': (
        'Два разных списка на счёте: один инвестиционный проект (project) и список '
        'муниципальных строек (public_projects).',
        'Это единственный из ключевых доменов без хозяина. Есть маленькая дверь project_slot, '
        'но она закрывает только саму ссылку на проект — не его поля.',
        'Поэтому прогресс, статус и материалы стройки меняют сразу несколько мест, и ни одно '
        'не является главным. Здесь же лежит самый заметный риск карты.'),
    'production_results': (
        'Что завод произвёл за месяц: сколько, из чего, за сколько и сколько заплатил рабочим.',
        'Разделено странно: строки для отчёта идут через аккуратные функции-хуки, '
        'а числовые поля предприятия (output, costs, status) пишутся напрямую.',
        'Отсюда же растёт план производства — он тоже живёт прямо в предприятии, без хозяина.'),
    'reports_save': (
        'Отчёты за месяцы и файл сохранения экономики.',
        'Экономика не лежит в базе мира: она в памяти, а на диск попадает в отдельные Parquet-файлы '
        'только по явному сохранению. В базу мира уходят лишь построенные учреждения и население.',
        'Сохранение пишется в временную папку и потом подменяет старую — прерванная запись '
        'не оставит битый сейв. Но список сделок для файла берётся из отчёта, поэтому режим '
        'хранения отчётов влияет на то, что окажется в сейве.'),
}

for _item in OWNERS:
    _plain = PLAIN_OWNERS.get(_item['id'])
    if _plain is None:
        raise SystemExit('НЕТ ПРОСТОГО ОБЪЯСНЕНИЯ ДЛЯ ВЛАДЕЛЬЦА: ' + _item['id'])
    _item['plain_what'], _item['plain_how'], _item['plain_care'] = _plain


# ---------------------------------------------------------------------------
# Сборка и проверки
# ---------------------------------------------------------------------------

def git(*args):
    try:
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                              text=True, encoding='utf-8').stdout.strip()
    except OSError:
        return ''


def build():
    node_ids = {item['id'] for item in NODES}
    if len(node_ids) != len(NODES):
        raise SystemExit('ДУБЛИКАТ ИДЕНТИФИКАТОРА УЗЛА')

    # Уникальность идентификаторов связей проверяется явно: повторяющийся id
    # молча склеивает рёбра в viewer, и карта теряет связи без единой ошибки.
    edge_ids = [item['id'] for item in EDGES]
    if len(set(edge_ids)) != len(edge_ids):
        duplicates = sorted({value for value in edge_ids if edge_ids.count(value) > 1})
        raise SystemExit('ДУБЛИКАТ ИДЕНТИФИКАТОРА СВЯЗИ: ' + ', '.join(duplicates))

    for item in EDGES:
        for side in ('source', 'target'):
            if item[side] not in node_ids:
                raise SystemExit(f'СВЯЗЬ ССЫЛАЕТСЯ НА НЕТ УЗЛА: {item["id"]} -> {item[side]}')

    domain_ids = {item['id'] for item in DOMAINS}
    for item in NODES:
        if item['domain'] not in domain_ids:
            raise SystemExit(f'НЕТ ДОМЕНА: {item["id"]} -> {item["domain"]}')

    scenario_nodes = set()
    for scenario in SCENARIOS:
        for variant in scenario['variants']:
            for item in variant['steps']:
                if item['node'] not in node_ids:
                    raise SystemExit(f'ШАГ ССЫЛАЕТСЯ НА НЕТ УЗЛА: {item["call"]}')
                scenario_nodes.add(item['node'])

    violation_ids = {item['id'] for item in VIOLATIONS}
    for owner in OWNERS:
        for key in ('owner_node', 'fallback_node'):
            value = owner.get(key)
            if value and value not in node_ids:
                raise SystemExit(f'ВЛАДЕЛЕЦ ССЫЛАЕТСЯ НА НЕТ УЗЛА: {owner["id"]} -> {value}')
        for key in ('readers', 'writers'):
            for value in owner[key]:
                if value not in node_ids:
                    raise SystemExit(f'ВЛАДЕЛЕЦ ССЫЛАЕТСЯ НА НЕТ УЗЛА: {owner["id"]} -> {value}')
        for value in owner['direct_writes'] + owner['callbacks']:
            if value not in violation_ids:
                raise SystemExit(f'ВЛАДЕЛЕЦ ССЫЛАЕТСЯ НА НЕТ НАРУШЕНИЯ: {owner["id"]} -> {value}')

    for item in VIOLATIONS:
        for value in item['related_nodes']:
            if value not in node_ids:
                raise SystemExit(f'НАРУШЕНИЕ ССЫЛАЕТСЯ НА НЕТ УЗЛА: {item["id"]} -> {value}')

    # Каждый узел, кроме чисто модульных, обязан быть достижим из сценария,
    # владельца или связи — иначе карта покажет «висячий» узел.
    referenced = set(scenario_nodes)
    for item in EDGES:
        referenced.add(item['source'])
        referenced.add(item['target'])
    for owner in OWNERS:
        referenced.add(owner['owner_node'])
        if owner.get('fallback_node'):
            referenced.add(owner['fallback_node'])
    dangling = sorted(node_ids - referenced)

    extensions = sorted(p.name for p in (ROOT / 'modules/economy/native').glob('*.pyd'))

    payload = dict(
        meta=dict(
            generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
            generator='architecture/generate_architecture.py',
            repository='noveria',
            branch=git('branch', '--show-current'),
            commit=git('rev-parse', 'HEAD'),
            remote_commit=git('rev-parse', 'origin/kimi/step8-hierarchical-routing'),
            python_files=sorted(p for p in ANALYSED_FILES if p.endswith('.py')),
            cython_files=sorted(p for p in ANALYSED_FILES if p.endswith(('.pyx', '.pxd', '.pxi'))),
            evidence_tests=EVIDENCE_TESTS,
            native_extensions_built=bool(extensions),
            native_extension_count=len(extensions),
            scope_note=('Анализировались только вход хода, экономика V3, её native-границы, '
                        'состояние/сохранение и focused-тесты владения. Worldgen, routing, UI и '
                        'остальные тесты не читались.'),
            limitations=[
                'Связи построены по точкам вызова в коде, а не по трассировке выполнения: '
                'динамические вызовы через getattr/словари в модель не попали.',
                'Порядок фаз зафиксирован для planning_mode="v3" и investment_mode="v3"; '
                'ветки lagged_batch и snapshot_batch в сценарий не включены.',
                'Cython-строки указаны по .pyx-исходнику; после пересборки они не меняются.',
                'Python-эталон месяца (monthly_step_python) в сценарии не показан как отдельный '
                'вариант: он используется только как fallback и в differential-тестах.',
            ],
        ),
        domains=DOMAINS,
        nodes=NODES,
        edges=EDGES,
        scenarios=SCENARIOS,
        owners=OWNERS,
        violations=VIOLATIONS,
        source_locations=LOCATION_ORDER,
        legend=dict(
            node_types=[dict(id=key, title=KIND_TITLES[key], explanation=KIND_EXPLANATIONS[key])
                        for key in ('class', 'function', 'store', 'kernel', 'boundary')],
            edge_kinds=[dict(id=key, title=EDGE_KIND_TITLES[key],
                             explanation=EDGE_KIND_EXPLANATIONS[key])
                        for key in ('calls', 'writes', 'owns', 'binds', 'callback')],
            violation_kinds=[dict(id=key, title=VIOLATION_KIND_TITLES[key],
                                  explanation=VIOLATION_KIND_EXPLANATIONS[key])
                             for key in ('direct_write', 'ownerless_state', 'callback',
                                         'boundary_leak')],
            how_to_read=[
                'Кружок — обычная функция или класс Python. Это медленная, но гибкая часть.',
                'Шестиугольник — ядро на Cython. Считает быстро, но мало что решает само.',
                'Ромб — граница: переводит вызовы между Python и Cython и держит запасной вариант.',
                'Скруглённый прямоугольник — хранилище. У него одного есть право менять своё поле.',
                'Зелёная рамка — узел владеет каким-то состоянием.',
                'Красная рамка — где-то рядом есть запись мимо владельца.',
                'Оранжевая стрелка — обратный вызов из быстрого кода в Python. Дорого.',
                'Синяя стрелка — владение: объект держит состояние.',
                'Зелёная стрелка — запись в состояние.',
                'Серая пунктирная — связь помечена как неподтверждённая.',
            ],
            glossary=[
                dict(term='Движок (Economy)',
                     meaning='Один объект, в котором живут все правила экономики и всё её состояние.'),
                dict(term='Месяц / ход',
                     meaning='Один шаг игры: все правила применяются по очереди, от цен до отчёта.'),
                dict(term='Заявка',
                     meaning='«Хочу купить» или «продаю»: количество, цена и сколько ещё не исполнено.'),
                dict(term='Резерв',
                     meaning='Деньги или товар, уже обещанные под заявку. Их нельзя потратить дважды.'),
                dict(term='Хранилище (store)',
                     meaning='Единственный хозяин поля: все изменения идут через его методы.'),
                dict(term='Граница (boundary)',
                     meaning='Прослойка между Python и Cython: переводит вызовы и держит запасную версию.'),
                dict(term='Ядро (kernel)',
                     meaning='Быстрый код на Cython. Считает числа, но не решает и не хранит.'),
                dict(term='Хук',
                     meaning='Функция Python, которую быстрый код зовёт обратно, когда сам не может.'),
                dict(term='Checkpoint',
                     meaning='Файлы сохранения экономики. Пишутся только по явному сохранению.'),
                dict(term='Ladybug',
                     meaning='База мира: страны, провинции, города, клетки. Экономики там больше нет.'),
                dict(term='Parquet',
                     meaning='Формат файлов, в которых лежит экономика между запусками игры.'),
                dict(term='Инвариант',
                     meaning='Правило, которое обязано выполняться всегда: например, денег не бывает меньше нуля.'),
            ],
        ),
        stats=dict(nodes=len(NODES), edges=len(EDGES), domains=len(DOMAINS),
                   owners=len(OWNERS), violations=len(VIOLATIONS),
                   source_locations=len(LOCATION_ORDER), dangling_nodes=dangling),
    )
    return payload


def main():
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    VIEWER_COPY.parent.mkdir(parents=True, exist_ok=True)
    VIEWER_COPY.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    # Сверка: JSON читается обратно и каждая ссылка существует.
    again = json.loads(OUT.read_text(encoding='utf-8'))
    failures = []
    for record in again['source_locations']:
        path = ROOT / record['file']
        if not path.is_file():
            failures.append(f'нет файла: {record["file"]}')
            continue
        total = len(path.read_text(encoding='utf-8').splitlines())
        if not 1 <= record['line'] <= total:
            failures.append(f'строка вне файла: {record["file"]}:{record["line"]}')
    if failures:
        raise SystemExit('ПРОВЕРКА ССЫЛОК ПРОВАЛЕНА:\n' + '\n'.join(failures))

    stats = again['stats']
    print(f'architecture.json: узлов {stats["nodes"]}, связей {stats["edges"]}, '
          f'локаций {stats["source_locations"]}')
    print(f'домены {stats["domains"]}, владельцы {stats["owners"]}, '
          f'нарушения {stats["violations"]}')
    print(f'висячих узлов: {stats["dangling_nodes"] or "нет"}')
    print(f'commit: {again["meta"]["commit"]}  branch: {again["meta"]["branch"]}')
    print(f'записано: {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
