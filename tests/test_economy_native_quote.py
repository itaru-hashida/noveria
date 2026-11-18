"""Focused-тесты ядра котировок и себестоимости.

Ядро работает с ``object``-арифметикой намеренно: цены, остатки и резервы
заявок бывают и int, и float, а встроенные ``min``/``max`` сохраняют тип
первого аргумента при равенстве. Тип результата виден дальше — в ``margin``,
``expected_profit`` проекта и в сумме ``unit_cost``, поэтому проверяется не
только значение, но и тип, порядок обхода и short-circuit.

Эталон — ``quote_boundary``-функции ``*_python``, fallback — те же
эквиваленты при отсутствии собранного расширения.
"""
import copy
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import quote_boundary as boundary  # noqa: E402

pytest.importorskip('modules.economy.native.quote_kernel')

EPS = support.EPS


def test_covered_equipment_quote_skips_reference_and_recovery(monkeypatch):
    def unexpected(*args):
        pytest.fail('Книга покрывает весь диапазон закупки')

    monkeypatch.setattr(boundary.QuoteHooks, 'offers',
                        lambda self: [(3, {'remaining': 4}, None)])
    monkeypatch.setattr(boundary.QuoteHooks, 'reference', unexpected)
    monkeypatch.setattr(boundary.QuoteHooks, 'recovery_rows', unexpected)
    context = boundary.prepare_native_quote_context(
        None, {'id': 'buyer'}, 'equipment', {}, 4, needs_reference=False)
    assert boundary.quoted_cost_native_prepared(context, 4) == (12, 3, True)


@pytest.mark.parametrize('recovery, expected', [((2, 4), 5), ((8, 7), 7)])
def test_prepared_quote_preserves_reference_floor(recovery, expected):
    from modules.economy.native.quote_kernel import quoted_cost_values

    context = (((3, 1),), 5, recovery)
    assert quoted_cost_values(context[0], 2, 5, recovery, EPS) == (
        3 + expected, expected, False)
    assert boundary.quoted_cost_native_prepared(context, 2) == (
        3 + expected, expected, False)


# ---------------------------------------------------------------------------
# Маленький движок и книга заявок
# ---------------------------------------------------------------------------

def make_engine(seed=17):
    """Синтетический мир на общей фикстуре."""
    import json
    cfg = json.loads((ROOT/'tests/fixtures/economy.json').read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000}),
        dict(id='h2', kind='SocialGroup', name='H2', properties={'owner_id': 'p', 'population': 50000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['grain', 'wood', 'ore', 'flax', 'fish'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=1, config=cfg))
    engine.meta['report_detail'] = 'standard'
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def sell(seller='a', price=2.0, remaining=10, quantity=None, status='active',
         province='p', tile='t'):
    """Живой оффер: ядру нужны id, participant, price, remaining, status."""
    return dict(id='sell-'+seller, participant=seller, good='grain',
                price=price, remaining=remaining,
                quantity=remaining if quantity is None else quantity,
                status=status, province_id=province, tile_id=tile)


def typed(value):
    """Значение и его тип: 5 и 5.0 различаются так же, как в V3."""
    return (type(value).__name__, value)


# ---------------------------------------------------------------------------
# quoted_cost: обход книги
# ---------------------------------------------------------------------------

def test_quoted_cost_covers_order_from_one_offer():
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': [sell(price=2.0, remaining=10)]}
    assert boundary.quoted_cost(engine, buyer, 'grain', 4, sells) == (8.0, 2.0, True)


def test_quoted_cost_splits_across_offers_in_book_order():
    engine = make_engine()
    buyer = engine.accounts['h']
    # Книга отсортирована по (landed_price, sell_id): сначала дешёвый оффер.
    sells = {'grain': [sell('a', 2.0, 4), sell('b', 3.0, 10)]}
    value, ceiling, exact = boundary.quoted_cost(engine, buyer, 'grain', 6, sells)
    assert (value, ceiling, exact) == (14.0, 3.0, True)

def test_quoted_cost_ceiling_is_the_maximum_price_paid():
    engine = make_engine()
    buyer = engine.accounts['h']
    # Книга отсортирована по landed price: сначала дешёвый оффер.
    sells = {'grain': [sell('a', 5.0, 2), sell('b', 2.0, 4)]}
    value, ceiling, exact = boundary.quoted_cost(engine, buyer, 'grain', 3, sells)
    assert value == 6.0 and ceiling == 2.0 and exact is True
    engine.__dict__.pop('_quoted_cost_cache', None)
    value, ceiling, exact = boundary.quoted_cost(engine, buyer, 'grain', 6, sells)
    assert value == 4*2.0+2*5.0 and ceiling == 5.0 and exact is True


def test_quoted_cost_falls_back_to_the_reference_price():
    """Книга кончилась: остаток оценивается по reference_price."""
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': [sell('a', 2.0, 1)]}
    engine.reference_price = lambda b, g, s: 7.5
    value, ceiling, exact = boundary.quoted_cost(engine, buyer, 'grain', 3, sells)
    assert (value, ceiling, exact) == (2.0+2*7.5, 7.5, False)


def test_quoted_cost_matches_python_reference():
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': [sell('a', 2.0, 3), sell('b', 2.0, 3), sell('c', 4.0, 5)]}
    for quantity in (0.5, 1, 3, 6, 6.5, 100):
        assert typed(boundary.quoted_cost(engine, buyer, 'grain', quantity, sells)[0]) \
            == typed(boundary.quoted_cost_python(engine, buyer, 'grain', quantity, sells)[0])


def test_quoted_cost_keeps_epsilon_threshold():
    """Остаток ниже EPS считается покрытым, выше — нет."""
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': [sell('a', 2.0, 1)]}
    engine.reference_price = lambda b, g, s: 1.0
    assert boundary.quoted_cost(engine, buyer, 'grain', 1+EPS/2, sells)[2] is True
    assert boundary.quoted_cost(engine, buyer, 'grain', 1+EPS*10, sells)[2] is False


def test_quoted_cost_uses_recovery_quotes_below_reference():
    """Ориентир восстановления поднимает цену, но не опускает её."""
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': []}
    firm = dict(id='other', tile_id='t')
    engine._recovery_quotes = {'grain': [(firm, 3.0)]}
    engine.reference_price = lambda b, g, s: 2.0
    engine.landed = lambda t, bt, g, p: (p, 0.0, 1, ('t',))
    value, ceiling, exact = boundary.quoted_cost(engine, buyer, 'grain', 2, sells)
    assert value == 6.0 and ceiling == 3.0 and exact is False


def test_quoted_cost_ignores_own_recovery_quote():
    """Своя же цена восстановления не считается предложением товара."""
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': []}
    engine._recovery_quotes = {'grain': [(dict(id=buyer['id'], tile_id='t'), 3.0)]}
    engine.reference_price = lambda b, g, s: 2.0
    engine.landed = lambda t, bt, g, p: (p, 0.0, 1, ('t',))
    assert boundary.quoted_cost(engine, buyer, 'grain', 2, sells)[0] == 4.0


def test_quoted_cost_short_circuits_when_book_covers_quantity():
    """Покрытый объём не зовёт reference_price; landed остаётся за книгой.

    ``landed`` вызывается построением книги (цена доставки каждого оффера),
    но не путём quoted_cost: ориентир восстановления при покрытом объёме не
    считается вовсе.
    """
    engine = make_engine()
    buyer = engine.accounts['h']
    calls = []
    engine.reference_price = lambda b, g, s: calls.append('reference') or 1.0
    engine.landed = lambda *a: (a[3], 0.0, 0, ['t'])
    sells = {'grain': [sell('a', 2.0, 10)]}
    assert boundary.quoted_cost(engine, buyer, 'grain', 5, sells)[2] is True
    assert calls == []


# ---------------------------------------------------------------------------
# reference_price
# ---------------------------------------------------------------------------

def test_reference_price_takes_the_first_live_offer():
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': [sell('a', 2.0, 0), sell('b', 3.0, 5)]}
    assert boundary.reference_price(engine, buyer, 'grain', sells) == 3.0


def test_reference_price_uses_stale_offer_while_it_has_remaining():
    """Курсор книги не перепрыгивает оффер, у которого ещё есть остаток."""
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': [sell('a', 2.0, 1), sell('b', 3.0, 5)]}
    assert boundary.reference_price(engine, buyer, 'grain', sells) == 2.0


def test_reference_price_without_offers_uses_the_market_signal():
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': []}
    engine.meta['market_history'] = [dict(markets=[dict(
        state_id=buyer['state_id'], province_id=engine.market_province(buyer),
        good='grain', volume=10, requested=12, unmet_solvent=0, trade_gold=30,
        freight=5, limit_value=5)])]
    assert boundary.reference_price(engine, buyer, 'grain', sells) == 3.5


def test_reference_price_falls_back_to_the_content_price():
    """Нет ни книги, ни истории рынка: берётся цена контента и её тип."""
    engine = make_engine()
    buyer = engine.accounts['h']
    expected = engine.goods['grain']['price']
    assert typed(boundary.reference_price(engine, buyer, 'grain', {})) == typed(expected)


def test_reference_price_matches_python_reference():
    engine = make_engine()
    buyer = engine.accounts['h']
    for sells in ({}, {'grain': [sell('a', 2.5, 4)]},
                  {'grain': [sell('a', 2.5, 0), sell('b', 4.0, 1)]}):
        assert typed(boundary.reference_price(engine, buyer, 'grain', sells)) == \
            typed(boundary.reference_price_python(engine, dict(buyer), 'grain', sells))


# ---------------------------------------------------------------------------
# unit_cost: компенсированные суммы
# ---------------------------------------------------------------------------

def test_unit_cost_sums_labor_inputs_and_wear():
    """Износ берётся через equipment_wear: норма умножается на wear_rate."""
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    recipe = dict(engine.goods[firm['good']])
    # Износ есть только у товаров с durable.wear_rate: выбираем такой, чтобы
    # проверялась вся формула, а не нулевое слагаемое.
    tool = next((good for good, item in engine.goods.items()
                 if item.get('durable', {}).get('wear_rate')), None)
    if tool is None:
        # Ни один товар фикстуры не изнашивается: задаём ставку товару, которого
        # нет во входах рецепта и который не является самим продуктом, иначе
        # цена износа совпала бы с ценой входа или перезаписалась рецептом.
        tool = next(good for good in engine.goods
                    if good not in ('grain', 'wood') and good != firm['good'])
        engine.goods[tool] = dict(engine.goods[tool], durable={'wear_rate': 0.25})
    engine.goods[firm['good']] = dict(recipe, inputs={'grain': 2.0, 'wood': 1.0},
                                      equipment={tool: 0.5}, labor=0.25)
    try:
        prices = {'grain': 1.5, 'wood': 2.0}
        engine.reference_price = lambda e, good, sells: prices.get(good, 4.0)
        wear = engine.equipment_wear(engine.goods[firm['good']], 1)[tool]
        assert wear > 0
        # Суммы raw/wear компенсированные, как встроенный sum: итог сверяется
        # с эталоном, а не с наивной формулой (последний бит у них разный).
        native = boundary.unit_cost(engine, firm, {})
        engine.__dict__.pop('_unit_cost_cache', None)
        assert native == boundary.unit_cost_python(engine, firm, {})
        naive = 0.25*engine.cfg['wage']+2.0*1.5+1.0*2.0+wear*4.0
        assert native == pytest.approx(naive, rel=1e-15)
    finally:
        engine.goods[firm['good']] = recipe


def test_unit_cost_uses_compensated_summation():
    """Последний бит: наивное сложение расходится с встроенным sum."""
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    recipe = dict(engine.goods[firm['good']])
    engine.goods[firm['good']] = dict(recipe, inputs={'grain': 0.1, 'wood': 0.2},
                                      equipment={}, labor=0.0)
    try:
        engine.reference_price = lambda e, good, sells: 21.599999999999994
        native = boundary.unit_cost(engine, firm, {})
        engine.__dict__.pop('_unit_cost_cache', None)
        reference = boundary.unit_cost_python(engine, firm, {})
        assert native == reference
    finally:
        engine.goods[firm['good']] = recipe


def test_unit_cost_keeps_int_result_for_integral_inputs():
    """Целая цена, целые нормы и целый труд дают int, как и раньше."""
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    recipe = dict(engine.goods[firm['good']])
    engine.goods[firm['good']] = dict(recipe, inputs={'grain': 2}, equipment={}, labor=0)
    try:
        engine.reference_price = lambda e, good, sells: 3
        wage = engine.cfg['wage']
        engine.cfg['wage'] = 1
        try:
            assert typed(boundary.unit_cost(engine, firm, {})) == ('int', 6)
        finally:
            engine.cfg['wage'] = wage
    finally:
        engine.goods[firm['good']] = recipe


def test_unit_cost_caches_per_sells_and_quote_version():
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    inputs = list(engine.goods[firm['good']]['inputs'])
    calls = []
    engine.reference_price = lambda e, good, sells: calls.append(good) or 1.0
    first = boundary.unit_cost(engine, firm, {})
    assert len(calls) == len(inputs)
    second = boundary.unit_cost(engine, firm, {})
    assert first == second and len(calls) == len(inputs)
    engine.mark_market_quotes_dirty(firm['good'])
    boundary.unit_cost(engine, firm, {})
    assert len(calls) == 2*len(inputs)


def test_unit_cost_matches_python_reference():
    engine = make_engine()
    for firm in engine.enterprises.values():
        left = boundary.unit_cost(engine, firm, {})
        engine.__dict__.pop('_unit_cost_cache', None)
        right = boundary.unit_cost_python(engine, firm, {})
        assert typed(left) == typed(right)


# ---------------------------------------------------------------------------
# volume_input_prices
# ---------------------------------------------------------------------------

def test_volume_input_prices_uses_quoted_cost_for_missing_material():
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    recipe = dict(engine.goods[firm['good']])
    engine.goods[firm['good']] = dict(recipe, inputs={'grain': 2.0}, equipment={})
    try:
        account = engine.accounts[firm['id']]
        account['stock'] = {'grain': 2.0}
        engine.free_stock = lambda account_id, good: 2.0
        calls = []
        engine.quoted_cost = lambda a, g, q, s: calls.append((g, q)) or (12.0, 2.0, True)
        engine.reference_price = lambda e, g, s: 99.0
        prices = boundary.volume_input_prices(engine, firm, 4.0, {})
        # missing = 4*2-2 = 6, а не среднее по всему объёму: 12/6 = 2.0.
        assert prices == {'grain': 2.0}
        assert calls == [('grain', 6.0)]
    finally:
        engine.goods[firm['good']] = recipe


def test_volume_input_prices_uses_reference_price_for_covered_material():
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    recipe = dict(engine.goods[firm['good']])
    engine.goods[firm['good']] = dict(recipe, inputs={'grain': 2.0}, equipment={})
    try:
        engine.free_stock = lambda account_id, good: 20.0
        engine.reference_price = lambda e, g, s: 4.5
        engine.quoted_cost = lambda *a: pytest.fail('quoted_cost не должен вызываться')
        assert boundary.volume_input_prices(engine, firm, 4.0, {}) == {'grain': 4.5}
    finally:
        engine.goods[firm['good']] = recipe


def test_volume_input_prices_keeps_recipe_order():
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    recipe = dict(engine.goods[firm['good']])
    engine.goods[firm['good']] = dict(recipe, inputs={'wood': 1.0, 'grain': 1.0},
                                      equipment={})
    try:
        engine.free_stock = lambda account_id, good: 0.0
        engine.quoted_cost = lambda a, g, q, s: (q*2.0, 2.0, True)
        assert list(boundary.volume_input_prices(engine, firm, 1.0, {})) == \
            ['wood', 'grain']
    finally:
        engine.goods[firm['good']] = recipe


def test_volume_input_prices_matches_python_reference():
    engine = make_engine()
    for firm in engine.enterprises.values():
        assert boundary.volume_input_prices(engine, firm, 5.0, {}) == \
            boundary.volume_input_prices_python(engine, firm, 5.0, {})


# ---------------------------------------------------------------------------
# input_bid_ceiling
# ---------------------------------------------------------------------------

def plan_firm(engine, plan):
    """Предприятие с готовым планом: проверки потолка читают его поля."""
    firm = next(iter(engine.enterprises.values()))
    firm['production_plan'] = plan
    return firm


def ceiling_buy(firm, good='grain'):
    return dict(id='buy-1', participant=firm['id'], good=good, side='buy',
                purpose='Сырьё', remaining=10.0, adaptive_input_budget=True)


def base_plan(**overrides):
    plan = dict(month=1, output=10.0, wage_unit=1.0, fixed_unit=0.5,
                input_ratios={'grain': 2.0, 'wood': 1.0},
                input_prices={'grain': 2.0, 'wood': 3.0},
                input_orders=['buy-1'], expected_net_price=12.0)
    plan.update(overrides)
    return plan


def test_input_bid_ceiling_subtracts_other_inputs_and_fixed_cost():
    engine = make_engine()
    firm = plan_firm(engine, base_plan())
    # other = 1.0+0.5 + 1.0*3.0 = 4.5; (12-4.5)/2 = 3.75
    assert boundary.input_bid_ceiling(engine, ceiling_buy(firm)) == 3.75


def test_input_bid_ceiling_uses_committed_order_prices():
    """Заявка с более высокой ценой поднимает оценку входа, как и раньше."""
    engine = make_engine()
    firm = plan_firm(engine, base_plan(input_orders=['buy-1', 'buy-2']))
    engine.report['orders'].append(dict(id='buy-1', good='grain', price=9.0,
                                        side='buy', participant=firm['id']))
    engine.report['orders'].append(dict(id='buy-2', good='wood', price=5.0,
                                        side='buy', participant=firm['id']))
    # other = 1.5 + 1.0*max(3.0, 5.0) = 6.5; (12-6.5)/2 = 2.75
    assert boundary.input_bid_ceiling(engine, ceiling_buy(firm)) == 2.75


def test_input_bid_ceiling_skips_the_bought_good_itself():
    """Своя закупка не входит в прочие затраты, даже если по ней есть заявка."""
    engine = make_engine()
    firm = plan_firm(engine, base_plan(input_orders=['buy-1']))
    engine.report['orders'].append(dict(id='buy-1', good='grain', price=90.0,
                                        side='buy', participant=firm['id']))
    assert boundary.input_bid_ceiling(engine, ceiling_buy(firm)) == 3.75


def test_input_bid_ceiling_ignores_orders_outside_the_plan():
    engine = make_engine()
    firm = plan_firm(engine, base_plan())
    engine.report['orders'].append(dict(id='buy-9', good='wood', price=50.0,
                                        side='buy', participant=firm['id']))
    assert boundary.input_bid_ceiling(engine, ceiling_buy(firm)) == 3.75


def test_input_bid_ceiling_is_zero_when_costs_exceed_the_price():
    engine = make_engine()
    firm = plan_firm(engine, base_plan(expected_net_price=1.0))
    assert boundary.input_bid_ceiling(engine, ceiling_buy(firm)) == 0


def test_input_bid_ceiling_rejects_orders_of_other_purposes():
    engine = make_engine()
    firm = plan_firm(engine, base_plan())
    for overrides in (dict(side='sell'), dict(purpose='Базовые нужды'),
                      dict(adaptive_input_budget=False), dict(final_consumption=True)):
        buy = ceiling_buy(firm)
        buy.update(overrides)
        assert boundary.input_bid_ceiling(engine, buy) is None, overrides


def test_input_bid_ceiling_rejects_stale_or_incomplete_plans():
    engine = make_engine()
    for overrides in (dict(month=0), dict(output=0.0),
                      dict(input_orders=[]), dict(input_ratios={'grain': 0.0, 'wood': 1.0}),
                      dict(input_prices={'grain': 2.0}),
                      dict(expected_net_price=None), dict(wage_unit=None)):
        firm = plan_firm(engine, base_plan(**overrides))
        buy = ceiling_buy(firm)
        if overrides.get('expected_net_price', 1) is None:
            firm['production_plan'].pop('expected_net_price')
        if overrides.get('wage_unit', 1) is None:
            firm['production_plan'].pop('wage_unit')
        assert boundary.input_bid_ceiling(engine, buy) is None, overrides


def test_input_bid_ceiling_matches_python_reference():
    engine = make_engine()
    firm = plan_firm(engine, base_plan())
    engine.report['orders'].append(dict(id='buy-1', good='grain', price=9.0,
                                        side='buy', participant=firm['id']))
    engine.report['orders'].append(dict(id='buy-2', good='wood', price=5.0,
                                        side='buy', participant=firm['id']))
    assert boundary.input_bid_ceiling(engine, ceiling_buy(firm)) == \
        boundary.input_bid_ceiling_python(engine, ceiling_buy(firm))


# ---------------------------------------------------------------------------
# reprice_input_order: числа ядра, мутации границы
# ---------------------------------------------------------------------------

def reprice_engine(quote=4.0, ceiling=5.0, price=2.0, reserved=0.0):
    """Движок с одной buy-заявкой и управляемой кассой."""
    engine = make_engine()
    firm = plan_firm(engine, base_plan())
    buy = dict(id='buy-1', participant=firm['id'], good='grain', side='buy',
               purpose='Сырьё', remaining=10.0, price=price, reserved=reserved,
               backing=reserved, adaptive_input_budget=True, status='active')
    engine.report['orders'].append(buy)
    engine.input_bid_ceiling = lambda order: ceiling
    engine.quoted_cost = lambda a, g, q, s: (0.0, quote, True)
    engine.free_cash = lambda account_id: 100.0
    return engine, firm, buy


def test_reprice_input_order_raises_price_to_the_quote():
    engine, firm, buy = reprice_engine(quote=4.0, ceiling=5.0, price=2.0)
    boundary.reprice_input_order(engine, buy, {})
    assert buy['price'] == 4.0
    assert buy['reserved'] == 20.0
    assert buy['backing'] == buy['reserved']
    assert buy['initial_input_limit'] == 2.0
    assert engine._demand_revision['grain'] == 1


def test_reprice_input_order_caps_at_the_ceiling():
    engine, firm, buy = reprice_engine(quote=9.0, ceiling=5.0, price=2.0)
    boundary.reprice_input_order(engine, buy, {})
    assert buy['price'] == 5.0


def test_reprice_input_order_caps_at_the_affordable_budget():
    """Бюджет ниже текущей цены не понижает её: поднятие не происходит."""
    engine, firm, buy = reprice_engine(quote=9.0, ceiling=9.0, price=2.0)
    engine.free_cash = lambda account_id: 15.0
    boundary.reprice_input_order(engine, buy, {})
    assert buy['price'] == 2.0 and buy['reserved'] == 0.0
    # Бюджет выше текущей цены, но ниже котировки: цена упирается в бюджет.
    engine, firm, buy = reprice_engine(quote=9.0, ceiling=9.0, price=2.0)
    engine.free_cash = lambda account_id: 25.0
    boundary.reprice_input_order(engine, buy, {})
    assert buy['price'] == 2.5


def test_reprice_input_order_keeps_the_price_when_not_worth_raising():
    engine, firm, buy = reprice_engine(quote=2.0, ceiling=5.0, price=2.0)
    boundary.reprice_input_order(engine, buy, {})
    assert buy['price'] == 2.0 and buy['reserved'] == 0.0
    assert engine._demand_revision['grain'] == 0


def test_reprice_input_order_skips_orders_without_ceiling():
    engine, firm, buy = reprice_engine()
    engine.input_bid_ceiling = lambda order: None
    boundary.reprice_input_order(engine, buy, {})
    assert buy['price'] == 2.0


def test_reprice_input_order_skips_spent_orders():
    engine, firm, buy = reprice_engine()
    buy['remaining'] = 0.0
    boundary.reprice_input_order(engine, buy, {})
    assert buy['price'] == 2.0


def test_reprice_input_order_matches_python_reference():
    for quote, ceiling, price, free in ((4.0, 5.0, 2.0, 100.0), (9.0, 5.0, 2.0, 100.0),
                                        (9.0, 9.0, 2.0, 25.0), (2.0, 5.0, 2.0, 100.0),
                                        (9.0, 9.0, 2.0, 15.0)):
        engine, firm, buy = reprice_engine(quote=quote, ceiling=ceiling, price=price)
        engine.free_cash = lambda account_id, value=free: value
        native = copy.deepcopy(engine)
        reference = copy.deepcopy(engine)
        boundary.reprice_input_order(native, native.report['orders'][-1], {})
        boundary.reprice_input_order_python(reference, reference.report['orders'][-1], {})
        left = native.report['orders'][-1]
        right = reference.report['orders'][-1]
        assert typed(left['price']) == typed(right['price']), (quote, 'price')
        assert typed(left['reserved']) == typed(right['reserved']), (quote, 'reserved')
        assert left['backing'] == right['backing']
        assert ('initial_input_limit' in left) == ('initial_input_limit' in right)
        if 'initial_input_limit' in left:
            assert typed(left['initial_input_limit']) == typed(right['initial_input_limit'])


# ---------------------------------------------------------------------------
# RNG, живые заявки и кэши
# ---------------------------------------------------------------------------

def test_rng_state_is_not_touched():
    engine = make_engine()
    firm = next(iter(engine.enterprises.values()))
    before = engine.rng.getstate()
    sells = {'grain': [sell('a', 2.0, 5)]}
    for _ in range(3):
        boundary.reference_price(engine, firm, 'grain', sells)
        boundary.quoted_cost(engine, firm, 'grain', 3, sells)
        boundary.volume_input_prices(engine, firm, 2.0, sells)
    assert engine.rng.getstate() == before


def test_live_offers_are_not_mutated():
    engine = make_engine()
    buyer = engine.accounts['h']
    order = sell('a', 2.0, 5)
    sells = {'grain': [order]}
    before = dict(order)
    boundary.quoted_cost(engine, buyer, 'grain', 8, sells)
    assert order == before


def test_quoted_cost_cache_is_reused_and_invalidated():
    engine = make_engine()
    buyer = engine.accounts['h']
    source = [sell('a', 2.0, 5)]
    sells = {'grain': source}
    engine.reference_price = lambda b, g, s: 100.0
    first = boundary.quoted_cost(engine, buyer, 'grain', 10, sells)
    entries = list(engine._quoted_cost_cache.values())
    assert entries and entries[0][1] == first
    # Тот же список и та же версия — попадание без пересчёта.
    calls = []
    engine.reference_price = lambda b, g, s: calls.append('reference') or 100.0
    assert boundary.quoted_cost(engine, buyer, 'grain', 10, sells) == first
    assert calls == []
    # Смена версии котировок — промах и пересчёт.
    engine.mark_market_quotes_dirty('grain')
    assert boundary.quoted_cost(engine, buyer, 'grain', 10, sells) == first
    assert calls == ['reference']


def test_caches_are_not_populated_when_quote_cache_is_off():
    engine = make_engine()
    engine.QUOTE_CACHE = False
    buyer = engine.accounts['h']
    sells = {'grain': [sell('a', 2.0, 5)]}
    boundary.reference_price(engine, buyer, 'grain', sells)
    boundary.quoted_cost(engine, buyer, 'grain', 10, sells)
    assert '_reference_price_cache' not in engine.__dict__
    assert '_quoted_cost_cache' not in engine.__dict__


# ---------------------------------------------------------------------------
# Fallback без .pyd
# ---------------------------------------------------------------------------

def test_fallback_switches_to_the_python_equivalent(monkeypatch):
    """Без .pyd граница привязывает Python-эквиваленты, а не падает."""
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    engine = make_engine()
    buyer = engine.accounts['h']
    sells = {'grain': [sell('a', 2.0, 3), sell('b', 3.0, 3)]}
    native_result = boundary.quoted_cost(engine, buyer, 'grain', 4, sells)
    monkeypatch.setattr(boundary, 'run_quoted', boundary._quoted_python)
    monkeypatch.setattr(boundary, 'run_reference', boundary._reference_python)
    monkeypatch.setattr(boundary, 'run_sum', boundary._sum_python)
    monkeypatch.setattr(boundary, 'run_volume', boundary._volume_python)
    monkeypatch.setattr(boundary, 'run_ceiling', boundary._ceiling_python)
    monkeypatch.setattr(boundary, 'run_reprice', boundary._reprice_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    engine.__dict__.pop('_quoted_cost_cache', None)
    assert boundary.NATIVE_KERNEL is False
    assert boundary.quoted_cost(engine, buyer, 'grain', 4, sells) == native_result
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_load_kernel_returns_none_when_extension_is_missing(monkeypatch):
    monkeypatch.setattr(boundary, 'load_quote_kernel', lambda: None)
    assert boundary.load_quote_kernel() is None


def test_boundary_exposes_both_implementations():
    for name in ('reference_price', 'quoted_cost', 'unit_cost',
                 'volume_input_prices', 'input_bid_ceiling', 'reprice_input_order',
                 'reference_price_python', 'quoted_cost_python', 'unit_cost_python',
                 'volume_input_prices_python', 'input_bid_ceiling_python',
                 'reprice_input_order_python', 'run_quoted', 'run_reference',
                 'run_sum', 'run_volume', 'run_ceiling', 'run_reprice',
                 '_quoted_python', '_reference_python', '_sum_python',
                 '_volume_python', '_ceiling_python', '_reprice_python'):
        assert callable(getattr(boundary, name)), name


def test_v3_has_no_inline_quote_arithmetic():
    """У правил котировок один call site: тела V3 больше не считают сами."""
    body = inspect_source('modules/economy/v3.py')
    for name in ('reference_price', 'quoted_cost', 'unit_cost',
                 'volume_input_prices', 'input_bid_ceiling', 'reprice_input_order'):
        start = body.index('    def '+name+'(')
        stop = body.index('\n    def ', start+1)
        chunk = body[start:stop]
        assert '_quote.' in chunk, name
        assert 'ceiling = max(ceiling' not in chunk, name
        assert 'for price, sell, _ in' not in chunk, name


def inspect_source(relative):
    return (ROOT/relative).read_text(encoding='utf-8')
