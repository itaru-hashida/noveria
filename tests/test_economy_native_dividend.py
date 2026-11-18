"""Focused-тесты ядра дивидендов: точное совпадение с Python-эталоном.

Ядро считает ``object``-арифметикой намеренно: ``capacity*labor*wage``,
``profit`` и ``available`` бывают и int, и float, а ``min``/``max`` возвращают
один из аргументов вместе с его типом. Проверяется не только значение, но и
тип, порядок обхода предприятий и получателей, порядок операций и
однократность side effects.

Три группы тестов:

  * числовая часть границы (``working_reserve``/``cash_after_commitments``)
    против эталона — значения, типы, порядок слагаемых;
  * полный ``pay_dividends`` против того же метода с подменённым на эталон
    ядром — деньги, отчёт, события и RNG;
  * exact replay двух seed'ов и fallback без .pyd.
"""
import contextlib
import copy
import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import dividend_boundary  # noqa: E402

FIXTURE = ROOT/'tests/fixtures/economy.json'
EPS = support.EPS


# ---------------------------------------------------------------------------
# Маленький движок на общей фикстуре
# ---------------------------------------------------------------------------

def build_engine(seed=17):
    """Движок на фикстуре: одно государство, домохозяйство и два предприятия."""
    cfg = json.loads(FIXTURE.read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['grain', 'wood', 'ore', 'flax', 'fish'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = 'standard'
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def add_household(engine, identity, population=100000, balance=100000.0):
    """Копия домохозяйства фикстуры под новым идентификатором.

    Новая группа населения обязательна: ``population`` читается через
    ``group_id``, и копия без своей группы делила бы население с ``h``.
    """
    row = copy.deepcopy(engine.accounts['h'])
    row['id'] = identity
    row['group_id'] = identity
    row['balance'] = balance
    engine.accounts[identity] = row
    engine.entities[identity] = dict(id=identity, kind='SocialGroup', name=identity,
        properties=dict(engine.entities['h']['properties'], population=population))
    return row


def firm(engine, identity='firm-00001'):
    return engine.enterprises[identity]


def fund(engine, identity='firm-00001', balance=10000.0):
    """Задать предприятию кассу через cash_store: единственный ledger."""
    engine.cash_store.set_balance(identity, balance)
    return engine.accounts[identity]


# ---------------------------------------------------------------------------
# Снимки и точное сравнение
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS = ('balance', 'reserved', 'working_reserve', 'project_reserve')


def rng_digest(engine):
    return hashlib.sha256(json.dumps(engine.rng.getstate(), sort_keys=True,
                                     ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def paid_flows(engine):
    """Только дивидендные переводы: в flows есть записи bootstrap."""
    return [flow for flow in engine.report['flows']
            if flow['reason'] in ('Доход населения от бизнеса', 'Дивиденды')]


def snapshot(engine):
    """Полный снимок того, что pay_dividends вправе менять."""
    return {
        'accounts': {aid: {key: a.get(key) for key in ACCOUNT_FIELDS}
                     for aid, a in engine.accounts.items()},
        'enterprises': {e['id']: {key: e.get(key) for key in
            ('dividends', 'dividend_month', 'working_reserve', 'project_reserve',
             'dividend_available_cash')} for e in engine.enterprises.values()},
        'dividends': copy.deepcopy(engine.report['dividends']),
        'diagnostics': [r for r in engine.report['diagnostics'] if r.get('kind') == 'dividend'],
        'flows': copy.deepcopy(paid_flows(engine)),
        'events': copy.deepcopy(engine.report['events']),
        'rng': rng_digest(engine),
    }


def assert_same(left, right, path='state'):
    """Точное сравнение: значения, типы и порядок ключей."""
    if isinstance(left, dict) and isinstance(right, dict):
        assert list(left.keys()) == list(right.keys()), path+'.keys'
        for key in left:
            assert_same(left[key], right[key], path+'['+repr(key)+']')
        return
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        assert type(left) is type(right), path+'.type'
        assert len(left) == len(right), path+'.length'
        for index, (a, b) in enumerate(zip(left, right)):
            assert_same(a, b, path+'['+str(index)+']')
        return
    if isinstance(left, bool) or isinstance(right, bool):
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        # Тип обязателен: min/max сохраняют тип выбранного аргумента.
        assert type(left) is type(right), path+'.type'
        assert left == right, path
        return
    assert type(left) is type(right), path+'.type'
    assert left == right, path


def clone(engine):
    return copy.deepcopy(engine)


def copy_sells(engine, sells):
    """Книга офферов в копии движка: ссылки на её заявки, а не на прежние."""
    by_id = {order['id']: order for order in engine.report['orders']}
    return {good: [by_id[order['id']] for order in orders]
            for good, orders in sells.items()}


@contextlib.contextmanager
def python_reference():
    """Подменить ядро эталоном на время полного вызова pay_dividends."""
    original = dividend_boundary.run_pay_dividends
    dividend_boundary.run_pay_dividends = dividend_boundary.pay_dividends_python
    try:
        yield
    finally:
        dividend_boundary.run_pay_dividends = original


def method_pair(engine, sells):
    """Полный pay_dividends: эталонное ядро против native-ядра."""
    reference, native = clone(engine), clone(engine)
    ref_sells, nat_sells = copy_sells(reference, sells), copy_sells(native, sells)
    with python_reference():
        reference.pay_dividends(ref_sells)
    native.pay_dividends(nat_sells)
    return reference, native


def assert_method_matches(engine, sells=None):
    reference, native = method_pair(engine, sells or {})
    assert_same(snapshot(reference), snapshot(native))
    return reference, native


# ---------------------------------------------------------------------------
# working cash reserve
# ---------------------------------------------------------------------------

def test_working_reserve_matches_reference():
    engine = build_engine()
    targets = [firm(engine), firm(engine, 'firm-00002')]
    hooks = dividend_boundary.DividendHooks(engine, {})
    for enterprise in targets:
        assert dividend_boundary.working_reserve(hooks, enterprise) == \
            dividend_boundary.working_reserve_python(hooks, enterprise)


def test_working_reserve_type_is_preserved():
    """capacity float * labor float: резерв остаётся float, без double-усечения."""
    engine = build_engine()
    enterprise = firm(engine)
    hooks = dividend_boundary.DividendHooks(engine, {})
    value = dividend_boundary.working_reserve(hooks, enterprise)
    assert value == dividend_boundary.working_reserve_python(hooks, enterprise)
    assert type(value) is float
    assert value == 240.0*0.4*0.12


def test_working_reserve_counts_missing_inputs():
    """Склад покрывает вход: резерв равен только оплате труда."""
    engine = build_engine()
    enterprise = firm(engine)
    hooks = dividend_boundary.DividendHooks(engine, {})
    # grain на складе 576 при потребности 240*1.2 = 288: недостачи нет.
    assert dividend_boundary.working_reserve(hooks, enterprise) == 240.0*0.4*0.12


def test_working_reserve_grows_when_stock_is_empty():
    engine = build_engine()
    enterprise = firm(engine)
    account = engine.accounts[enterprise['id']]
    account['stock']['grain'] = 0.0
    account['values']['grain'] = 0.0
    hooks = dividend_boundary.DividendHooks(engine, {})
    value = dividend_boundary.working_reserve(hooks, enterprise)
    assert value == dividend_boundary.working_reserve_python(hooks, enterprise)
    # 288 недостающего зерна по 1.0 плюс оплата труда.
    assert value > 240.0*0.4*0.12


# ---------------------------------------------------------------------------
# available cash
# ---------------------------------------------------------------------------

def test_cash_after_commitments_uses_max_of_protected_and_reserves():
    assert dividend_boundary.cash_after_commitments(100.0, 10.0, 50.0, 20.0, 5.0) == 40.0
    assert dividend_boundary.cash_after_commitments(100.0, 10.0, 5.0, 20.0, 5.0) == 65.0


def test_cash_after_commitments_never_negative():
    assert dividend_boundary.cash_after_commitments(10.0, 100.0, 0.0, 0.0, 0.0) == 0
    assert dividend_boundary.cash_after_commitments(10.0, 0.0, 100.0, 0.0, 0.0) == 0


def test_cash_after_commitments_keeps_int_when_clipped():
    """Отсечение нулём возвращает int 0, а не 0.0: тип наблюдаем в отчёте."""
    assert type(dividend_boundary.cash_after_commitments(0, 0, 0, 0, 0)) is int
    assert dividend_boundary.cash_after_commitments(100, 0, 0, 0, 0) == 100


def test_cash_after_commitments_matches_reference():
    engine = build_engine()
    hooks = dividend_boundary.DividendHooks(engine, {})
    for balance, reserved, protected, project, working in (
            (100.0, 10.0, 50.0, 20.0, 5.0), (0.0, 0.0, 0.0, 0.0, 0.0),
            (10, 3, 2, 1, 4), (999.5, 0.5, 0.0, 1.25, 2.5)):
        assert dividend_boundary.cash_after_commitments(
            balance, reserved, protected, project, working) == \
            dividend_boundary.cash_after_commitments_python(
                balance, reserved, protected, project, working)


# ---------------------------------------------------------------------------
# Пустой список владельцев и базовые случаи
# ---------------------------------------------------------------------------

def test_no_households_pays_nothing():
    """Получателей нет: население нулевое, выплат и строк отчёта нет."""
    engine = build_engine()
    fund(engine)
    firm(engine)['profit'] = 500.0
    engine.accounts['h']['kind'] = 'merchant'
    reference, native = assert_method_matches(engine)
    assert native.report['dividends'] == []
    assert paid_flows(native) == []


def test_single_household_receives_everything():
    """Один получатель: вся сумма уходит ему одному, без деления."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    rows = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001']
    assert len(rows) == 1
    amount = rows[0]['dividends']
    assert amount > 0
    assert native.accounts['h']['balance'] == 100000.0+amount


def test_multiple_households_split_by_population():
    """Три домохозяйства: доли пропорциональны населению, порядок сохранён."""
    engine = build_engine()
    add_household(engine, 'h2', population=50000)
    add_household(engine, 'h3', population=25000)
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 1000.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    amount = [r for r in native.report['dividends']
              if r['enterprise'] == 'firm-00001'][0]['dividends']
    assert amount > 0
    total = 100000+50000+25000
    assert native.accounts['h']['balance'] == 100000.0+amount*100000/total
    assert native.accounts['h2']['balance'] == 100000.0+amount*50000/total
    assert native.accounts['h3']['balance'] == 100000.0+amount*25000/total


def test_recipient_order_follows_accounts_dict():
    """Порядок получателей — порядок словаря счетов, а не сортировка."""
    engine = build_engine()
    add_household(engine, 'h0', population=1000)
    add_household(engine, 'h2', population=1000)
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    seen = []
    original = engine.transfer

    def traced(sender, recipient, amount, reason):
        seen.append(recipient)
        return original(sender, recipient, amount, reason)

    engine.transfer = traced
    try:
        engine.pay_dividends({})
    finally:
        engine.transfer = original
    households = [aid for aid, a in engine.accounts.items() if a['kind'] == 'household']
    # Предприятия идут парами (по одному проходу на фирму), внутри прохода
    # получатели обходятся в порядке словаря счетов.
    assert seen[:len(households)] == households
    assert seen == households*len(engine.enterprises)
    assert seen != sorted(seen), 'порядок должен быть вставки, а не сортировки'


def test_zero_profit_pays_nothing():
    """Нулевая прибыль: предел ноль, перевода нет, строка отчёта есть."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    for enterprise in engine.enterprises.values():
        enterprise['profit'] = 0
    reference, native = assert_method_matches(engine)
    assert native.report['dividends'], 'строка отчёта пишется и при нуле'
    assert all(row['dividends'] == 0 for row in native.report['dividends'])
    assert paid_flows(native) == []


def test_negative_profit_pays_nothing():
    """Отрицательная прибыль: max(0, profit) не даёт отрицательного предела."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    for enterprise in engine.enterprises.values():
        enterprise['profit'] = -500.0
    reference, native = assert_method_matches(engine)
    assert all(row['profit_limit'] == 0 for row in native.report['dividends'])
    assert all(row['dividends'] == 0 for row in native.report['dividends'])


def test_positive_profit_limited_by_share():
    """Предел — share*profit, если кассы хватает."""
    engine = build_engine()
    fund(engine, balance=100000.0)
    engine.enterprises['firm-00001']['profit'] = 1000.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    row = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    share = native.cfg['business_income_share']
    assert row['profit_limit'] == share*1000.0
    assert row['dividends'] == row['profit_limit']


def test_insufficient_cash_limits_dividends():
    """Кассы меньше предела: выплата равна доступной кассе."""
    engine = build_engine()
    fund(engine, balance=100.0)
    engine.enterprises['firm-00001']['profit'] = 100000.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    row = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    assert row['dividends'] == row['available_cash']
    assert row['dividends'] < row['profit_limit']


# ---------------------------------------------------------------------------
# Несколько счетов и порядок
# ---------------------------------------------------------------------------

def test_multiple_enterprises_pay_in_dict_order():
    """Предприятия обходятся в порядке словаря, выплаты не перемешиваются."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    fund(engine, 'firm-00002', balance=10000.0)
    for enterprise in engine.enterprises.values():
        enterprise['profit'] = 1000.0
    # Сдвигаем месяц, чтобы сработал обход, а не пропуск уже оплаченного.
    engine.meta['month'] = 5
    for enterprise in engine.enterprises.values():
        enterprise.pop('dividend_month', None)
    seen = []
    original = engine.transfer

    def traced(sender, recipient, amount, reason):
        if sender not in seen:
            seen.append(sender)
        return original(sender, recipient, amount, reason)

    engine.transfer = traced
    try:
        engine.pay_dividends({})
    finally:
        engine.transfer = original
    assert seen == [e['id'] for e in engine.enterprises.values()]


def test_enterprise_is_paid_once_per_month():
    """Повторный вызов в том же месяце не платит второй раз."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    engine.pay_dividends({})
    after_first = engine.accounts['h']['balance']
    rows_first = len(engine.report['dividends'])
    engine.pay_dividends({})
    assert engine.accounts['h']['balance'] == after_first, 'дивиденды выплачены дважды'
    assert len(engine.report['dividends']) == rows_first


def test_dividend_month_is_recorded():
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.pay_dividends({})
    for enterprise in engine.enterprises.values():
        assert enterprise['dividend_month'] == engine.meta['month']


def test_closed_enterprise_is_skipped():
    """Закрытое предприятие: выплаты нет, строка отчёта не пишется."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['status'] = 'closed'
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    assert all(r['enterprise'] != 'firm-00001' for r in native.report['dividends'])
    assert native.accounts['h']['balance'] == 100000.0


# ---------------------------------------------------------------------------
# Порядок суммирования и тип
# ---------------------------------------------------------------------------

def test_population_sum_order_is_sequential():
    """Доли считаются по последовательно просуммированному населению.

    ``sum`` над генератором и явный цикл дают одинаковый результат, а деление
    выполняется уже от этой суммы — без промежуточного округления.
    """
    engine = build_engine()
    values = [0.1, 0.2, 0.3]
    for index, value in enumerate(values):
        identity = 'h%d' % index
        engine.entities[identity] = dict(id=identity, kind='SocialGroup',
            properties={'owner_id': 'p', 'population': value})
        row = copy.deepcopy(engine.accounts['h'])
        row['id'] = identity
        row['group_id'] = identity
        engine.accounts[identity] = row
    fund(engine, balance=100000.0)
    engine.enterprises['firm-00001']['profit'] = 1000.0
    engine.enterprises['firm-00002']['profit'] = 0
    before = {identity: engine.accounts[identity]['balance']
              for identity in ('h', 'h0', 'h1', 'h2')}
    engine.pay_dividends({})
    row = [r for r in engine.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    amount = row['dividends']
    assert amount > 0
    # h — тоже получатель, поэтому его население входит в общую сумму.
    total = 0.0
    for account in engine.accounts.values():
        if account['kind'] == 'household':
            total = total+engine.population(account)
    for index, value in enumerate(values):
        identity = 'h%d' % index
        expected = before[identity]+amount*value/total
        assert engine.accounts[identity]['balance'] == expected
    h_share = amount*engine.population(engine.accounts['h'])/total
    assert engine.accounts['h']['balance'] == before['h']+h_share


def test_share_arithmetic_keeps_int_amount():
    """int-предел и int-касса: выплата остаётся int, доли — float."""
    engine = build_engine()
    engine.cfg['business_income_share'] = 1
    engine.cash_store.set_balance('firm-00001', 10000)
    engine.enterprises['firm-00001']['profit'] = 500
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    row = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    assert type(row['dividends']) is int
    assert row['dividends'] == 500


def test_float_profit_stays_float():
    engine = build_engine()
    fund(engine, balance=100000.0)
    engine.enterprises['firm-00001']['profit'] = 333.33
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    row = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    assert type(row['dividends']) is float
    assert row['dividends'] == 333.33*native.cfg['business_income_share']


def test_kernel_pays_through_transfer_only():
    """Ядро не двигает деньги само: каждая выплата — вызов прежнего transfer."""
    engine = build_engine()
    add_household(engine, 'h2', population=50000)
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    calls = []
    original = engine.transfer

    def traced(sender, recipient, amount, reason):
        calls.append((sender, recipient, amount, reason))
        return original(sender, recipient, amount, reason)

    engine.transfer = traced
    try:
        engine.pay_dividends({})
    finally:
        engine.transfer = original
    households = [aid for aid, a in engine.accounts.items() if a['kind'] == 'household']
    assert len(calls) == 4, 'два предприятия × два получателя'
    assert [c[1] for c in calls] == households*2
    assert {c[3] for c in calls} == {'Доход населения от бизнеса'}


def test_transfer_call_count_matches_reference():
    """Число вызовов transfer совпадает с эталоном на ненулевом сценарии."""
    engine = build_engine()
    add_household(engine, 'h2', population=10000)
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 250.0

    def count(target):
        calls = []
        original = target.transfer

        def traced(sender, recipient, amount, reason):
            calls.append(recipient)
            return original(sender, recipient, amount, reason)

        target.transfer = traced
        try:
            target.pay_dividends({})
        finally:
            target.transfer = original
        return calls

    reference, native = clone(engine), clone(engine)
    ref_calls = count(reference)
    nat_calls = count(native)
    assert nat_calls == ref_calls
    assert len(nat_calls) == 4, 'два предприятия × два получателя'
    assert_same(snapshot(reference), snapshot(native))


def test_report_and_events_are_written_once():
    """Строка отчёта и диагностика пишутся ровно по одной на предприятие."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    for enterprise in engine.enterprises.values():
        enterprise['profit'] = 100.0
    engine.pay_dividends({})
    rows = engine.report['dividends']
    diagnostics = [r for r in engine.report['diagnostics'] if r.get('kind') == 'dividend']
    assert len(rows) == len(engine.enterprises)
    assert len(diagnostics) == len(rows)
    for row, diagnostic in zip(rows, diagnostics):
        assert diagnostic['kind'] == 'dividend'
        assert diagnostic['enterprise'] == row['enterprise']


def test_report_row_shape_matches_reference():
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    assert list(native.report['dividends']) == list(reference.report['dividends'])


# ---------------------------------------------------------------------------
# Эталон против ядра
# ---------------------------------------------------------------------------

def test_reference_equals_native_on_empty_book():
    engine = build_engine()
    fund(engine, balance=5000.0)
    for enterprise in engine.enterprises.values():
        enterprise['profit'] = 400.0
    reference, native = assert_method_matches(engine)
    assert native.report['dividends']
    assert native.report['flows'] == reference.report['flows']


def test_reference_equals_native_with_holds():
    """Резервы и protected сокращают доступную кассу одинаково в обеих ветках."""
    engine = build_engine()
    account = fund(engine, balance=5000.0)
    account['reserved'] = 1000.0
    account['protected'] = 500.0
    engine.enterprises['firm-00001']['profit'] = 1000.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    row = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    assert row['available_cash'] == 5000.0-1000.0-max(500.0, row['working_reserve'])


def test_reference_equals_native_for_legacy_branch():
    """Без PROFIT_DISTRIBUTION_REVISION выплата идёт владельцу, как раньше."""
    engine = build_engine()
    engine.PROFIT_DISTRIBUTION_REVISION = 0
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    reference, native = assert_method_matches(engine)
    row = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    owner = native.enterprises['firm-00001']['owner_id']
    assert row['recipient_kind'] == native.accounts[owner]['kind']
    assert paid_flows(native) == paid_flows(reference)
    assert paid_flows(native), 'выплата владельцу обязана быть'
    assert all(flow['reason'] == 'Дивиденды' for flow in paid_flows(native))


# ---------------------------------------------------------------------------
# fallback без .pyd
# ---------------------------------------------------------------------------

def test_boundary_exposes_native_when_built():
    built = hasattr(dividend_boundary.load_dividend_kernel(), 'calculate_dividend')
    assert dividend_boundary.NATIVE_KERNEL is built
    if built:
        assert dividend_boundary.calculate_dividend.__module__.endswith('dividend_kernel')
        assert dividend_boundary.pay_dividends.__module__.endswith('dividend_boundary')
    else:
        assert dividend_boundary.calculate_dividend is dividend_boundary.calculate_dividend_python


def test_native_entry_points_are_compiled():
    kernel = dividend_boundary.load_dividend_kernel()
    if not hasattr(kernel, 'calculate_dividend'):
        pytest.skip('Cython-расширение dividend_kernel не собрано')
    assert dividend_boundary.calculate_dividend is kernel.calculate_dividend
    assert not hasattr(kernel, 'pay_dividends')


def test_load_returns_none_when_extension_absent(monkeypatch):
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('dividend_kernel'):
            raise ImportError("cannot import name 'dividend_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert dividend_boundary.load_dividend_kernel() is None


def test_fallback_without_extension(monkeypatch):
    """Без .pyd граница обязана взять Python-ветку и дать тот же результат."""
    engine = build_engine()
    add_household(engine, 'h2', population=25000)
    fund(engine, balance=8000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 200.0
    reference, _ = method_pair(engine, {})
    expected = snapshot(reference)

    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name.endswith('dividend_kernel'):
            raise ImportError("cannot import name 'dividend_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    importlib.reload(dividend_boundary)
    try:
        assert dividend_boundary.NATIVE_KERNEL is False
        assert dividend_boundary.calculate_dividend is dividend_boundary.calculate_dividend_python
        assert dividend_boundary.pay_dividends.__module__.endswith('dividend_boundary')
        fallback = clone(engine)
        fallback.pay_dividends({})
        assert_same(expected, snapshot(fallback))
    finally:
        monkeypatch.undo()
        importlib.reload(dividend_boundary)


# ---------------------------------------------------------------------------
# deepcopy
# ---------------------------------------------------------------------------

def test_deepcopy_isolated_from_original():
    engine = build_engine()
    fund(engine, balance=5000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    before = copy.deepcopy({aid: row['balance'] for aid, row in engine.accounts.items()})
    clone_engine = copy.deepcopy(engine)
    clone_engine.pay_dividends({})
    assert {aid: row['balance'] for aid, row in engine.accounts.items()} == before, \
        'выплата в копии изменила оригинал'
    left, right = copy.deepcopy(engine), copy.deepcopy(engine)
    left.pay_dividends({})
    right.pay_dividends({})
    assert_same(snapshot(left), snapshot(right))


# ---------------------------------------------------------------------------
# forced-case и RNG
# ---------------------------------------------------------------------------

def test_forced_case_pays_dividends():
    """Явный сценарий: прибыль есть, касса есть, домохозяйство получает деньги."""
    engine = build_engine()
    fund(engine, balance=50000.0)
    engine.enterprises['firm-00001']['profit'] = 1000.0
    engine.enterprises['firm-00002']['profit'] = 0
    before = engine.accounts['h']['balance']
    reference, native = assert_method_matches(engine)
    row = [r for r in native.report['dividends'] if r['enterprise'] == 'firm-00001'][0]
    assert row['dividends'] > 0
    assert native.accounts['h']['balance'] == before+row['dividends']
    flows = paid_flows(native)
    assert flows, 'перевод обязан быть записан'
    assert flows[0]['gold'] == row['dividends']


def test_dividends_do_not_touch_cash_ledger_twice():
    """Сумма золота сохраняется: выплата — один перевод, а не два."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    engine.enterprises['firm-00002']['profit'] = 0
    before = sum(a['balance'] for a in engine.accounts.values())
    engine.pay_dividends({})
    after = sum(a['balance'] for a in engine.accounts.values())
    assert after == before, 'золото должно сохраняться'


def test_rng_is_not_touched():
    """Числовой срез не тратит RNG: порядок случайности остаётся за Python."""
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    before = rng_digest(engine)
    engine.pay_dividends({})
    assert rng_digest(engine) == before


def test_rng_equal_between_branches():
    engine = build_engine()
    fund(engine, balance=10000.0)
    engine.enterprises['firm-00001']['profit'] = 500.0
    reference, native = assert_method_matches(engine)
    assert rng_digest(reference) == rng_digest(native)


def test_v3_has_single_call_site():
    """В pay_dividends один вызов границы, а не вторая копия числового цикла."""
    source = inspect.getsource(Economy.pay_dividends)
    assert '_dividend.pay_dividends(self, sells)' in source
    module_source = (ROOT/'modules/economy/v3.py').read_text(encoding='utf-8')
    assert module_source.count('_dividend.pay_dividends(') == 1
    assert 'self.working_cash_reserve(e, sells)' not in module_source


# ---------------------------------------------------------------------------
# Exact replay: три месяца, точные digest'ы и RNG
# ---------------------------------------------------------------------------

REPLAY_17 = (
    ('841f2a5eddc798a94257375d7ba345e56568282750f8e25a34b2304861b41a8b',
     '27faddba45de04cfc862fa928089a2623cbfc1a0a9134cc8bbdc5bd3f14c053d'),
    ('5a12242069537e8b3280e928461cbb58d21da3122d71fd34bd93f2becb82c06e',
     '6762622231de9e3aa7f5baba2a5042010a7acb8df096ddfbb2061595395615f9'),
    ('03c1ad58356aa19ec6fda56643da7018027f562db5d642d4aafdab6ceed6c9af',
     '382b2b0219d08cffccf9b8a101e35d092cf5773359f641cdb9ee101052de1377'),
)

REPLAY_73129 = (
    ('6895f3193e5d48b5cc1d88e6a4d9dfc32af67ab4c14805680176c223bb61cc5a',
     'dff18cf6396d1177e4feb0efa4a7c0c96e00a07667a159e75b909b6fa5bd8c49'),
    ('053059ce0cada486de276f16a496d645d2937f62a36e544d049a40e6d2581e1b',
     'b97fb4ac40fd8f327e7aef9dd4d68823319231a60334c035c386d48007f8bc5a'),
    ('e9a575a7edba0c06214c09759dd611ea6dceccdc0d9902196b9b79b593a50198',
     'e9038abab22c8500b3ca957eb670c7eb82d85d373c135221c947df09c282e32b'),
)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def replay_digest(engine):
    """Digest состояния и дивидендных строк за месяц."""
    return digest({
        'accounts': {a['id']: a for a in engine.accounts.values()},
        'enterprises': {e['id']: e for e in engine.enterprises.values()},
        'meta': {k: v for k, v in engine.meta.items() if k != 'market_history'},
        'dividends': engine.report.get('dividends'),
        'diagnostics': [r for r in engine.report.get('diagnostics', [])
                        if r.get('kind') == 'dividend'],
        'flows': engine.report.get('flows'),
        'events': engine.report.get('events'),
    })


def run_replay(seed, months=3):
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    rows = []
    with workdir('test_dividend_replay_%d' % seed) as directory:
        db, service = sr.build_world(seed, directory, report_detail='standard')
        try:
            for index in range(months):
                service.advance_turn(command_id='dividend-replay-%d-%d' % (seed, index),
                                     source='audit')
                engine = service.economy_state().engine
                rows.append((replay_digest(engine), rng_digest(engine)))
        finally:
            db.close()
    return rows


@pytest.fixture(scope='module')
def replay_seed_17():
    return run_replay(17)


@pytest.fixture(scope='module')
def replay_seed_73129():
    return run_replay(73129)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_17(replay_seed_17, index):
    state, rng = replay_seed_17[index]
    assert (state, rng) == REPLAY_17[index], 'месяц %d' % (index+1)


@pytest.mark.parametrize('index', (0, 1, 2))
def test_exact_replay_seed_73129(replay_seed_73129, index):
    state, rng = replay_seed_73129[index]
    assert (state, rng) == REPLAY_73129[index], 'месяц %d' % (index+1)


def test_replay_is_deterministic(replay_seed_17, replay_seed_73129):
    for rows in (replay_seed_17, replay_seed_73129):
        assert len(rows) == 3
        assert all(rng for _, rng in rows)
    assert run_replay(17) == list(replay_seed_17)


def test_replay_actually_pays_dividends():
    """Exact replay не должен проходить на пустом множестве выплат."""
    sys.path.insert(0, str(ROOT/'audit'))
    import scenario_runner as sr
    from audit.profile_run import workdir

    counts = {'rows': 0, 'paid': 0, 'gold': 0.0}
    original = Economy.pay_dividends

    def traced(self, sells):
        result = original(self, sells)
        rows = self.report['dividends']
        counts['rows'] += len(rows)
        paid = [row for row in rows if row['dividends'] > 0]
        counts['paid'] += len(paid)
        counts['gold'] += sum(row['dividends'] for row in paid)
        return result

    Economy.pay_dividends = traced
    try:
        with workdir('test_dividend_branch') as directory:
            db, service = sr.build_world(73129, directory, report_detail='standard')
            try:
                for index in range(3):
                    service.advance_turn(command_id='dividend-branch-%d' % index,
                                         source='audit')
            finally:
                db.close()
    finally:
        Economy.pay_dividends = original
    assert counts['rows'] > 0, 'строки отчёта не писались'
    assert counts['paid'] > 0, 'дивиденды ни разу не выплачивались'
    assert counts['gold'] > 0
