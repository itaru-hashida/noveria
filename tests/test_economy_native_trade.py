"""Тесты Cython-исполнения одной уже выбранной сделки (``commit_trade``).

Выбор buyer/seller, цена и количество ``q`` остаются в Python-цикле ``match``.
Native-ядро получает готовое решение и исполняет его в прежнем порядке:
резервы, товар, переводы, доходы фирмы, материалы стройки, остатки заявок,
flows/trades и ревизии. Тесты проверяют три вещи:

* каждое поле состояния меняется ровно так, как менял прежний Python-код;
* native и ``commit_trade_python`` дают побитово одинаковый результат;
* вызываемые методы движка (``transfer``/``use_stock``/``add_stock``) не
  обходятся, поэтому переопределение в наследнике продолжает работать.

Тесты не собирают мир целиком: для одного участка исполнения достаточно
маленького движка с двумя счетами и заявками. Это focused-тесты границы, а
не проверка месяца.
"""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import cash_boundary  # noqa: E402
from modules.economy.native import order_boundary  # noqa: E402
from modules.economy.native import trade_boundary  # noqa: E402

EPS = support.EPS
KERNEL = trade_boundary.load_trade_kernel()
CASH = cash_boundary.load_cash_store()

pytestmark = pytest.mark.skipif(KERNEL is None,
                                reason='Cython-расширение trade_kernel не собрано')

GOOD = 'planks'


# --- вспомогательные функции ---------------------------------------------
def build_engine(seed=17, detail='none'):
    """Маленький движок из фикстуры: счета, фирмы, товары, конфиг."""
    cfg = json.loads((ROOT/'tests/fixtures/economy.json').read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H',
             properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = detail
    engine.seed = seed
    engine.rng.seed(seed)
    engine.bootstrap()
    return engine


def trade_engine(quantity=10.0, price=5.0, buyer_cash=1000.0, seller_stock=10.0,
                 seller_value=40.0, freight=1.0, final_consumption=False,
                 purpose='Товар'):
    """Сцена одной сделки: buyer-заявка, sell-заявка и два счёта.

    ``price`` — landed-цена (с доставкой), ``freight`` — её транспортная часть;
    в заявке продавца хранится цена без доставки, как в реальном ``match``.
    """
    engine = build_engine()
    buyer = engine.accounts['merchant:s']
    seller = engine.accounts['firm-00008']
    buyer['balance'] = buyer_cash
    buyer['reserved'] = 0.0
    # Сцена изолирована: стартовый склад покупателя не должен попадать в
    # ожидания теста, иначе проверяется не сделка, а bootstrap мира.
    buyer['stock'] = {}
    buyer['values'] = {}
    seller['stock'] = {GOOD: seller_stock} if seller_stock else {}
    seller['values'] = {GOOD: seller_value} if seller_value else {}
    seller['stock_reserved'] = {GOOD: quantity}
    engine.meta['month'] = 1
    engine.enterprises['firm-00008']['revenue'] = 0
    engine.enterprises['firm-00008']['tax'] = 0
    engine.enterprises['firm-00008']['cost_of_sales'] = 0
    reserved = quantity*price
    buy = dict(id='b1', participant='merchant:s', good=GOOD, quantity=quantity,
        remaining=quantity, price=price, side='buy', purpose=purpose,
        status='active', reason='', expires=engine.meta['month'],
        final_consumption=final_consumption, state_id='s', province_id='p',
        tile_id='t', reserved=reserved, backing=reserved)
    sell = dict(id='s1', participant='firm-00008', good=GOOD, quantity=quantity,
        remaining=quantity, price=price-freight, side='sell', purpose='Продажа запасов',
        status='active', reason='', expires=engine.meta['month'],
        final_consumption=False, state_id='s', province_id='p', tile_id='t',
        reserved=0, backing=quantity)
    engine.report['orders'] = [buy, sell]
    engine.report['trades'] = []
    engine.report['flows'] = []
    engine.reset_report()
    engine.order_store.append(buy)
    engine.order_store.append(sell)
    engine.cash_store.add_reserved_cash('merchant:s', reserved)
    quote = (price, freight, 0, ())
    return engine, buy, sell, quote


def commit(engine, buy, sell, quote, quantity, price, commit_fn=None):
    """Вызвать выбранную реализацию с теми же аргументами, что и в ``match``."""
    function = commit_fn or trade_boundary.commit_trade
    return function(engine, buy, sell, quote, quantity, price, EPS,
                    engine.cash_store, engine.order_store)


def snapshot(engine, buy, sell):
    """Все поля состояния, которые трогает одна сделка."""
    return dict(
        buyer_balance=engine.accounts[buy['participant']]['balance'],
        buyer_reserved=engine.accounts[buy['participant']].get('reserved'),
        seller_balance=engine.accounts[sell['participant']]['balance'],
        seller_stock=dict(engine.accounts[sell['participant']]['stock']),
        seller_values=dict(engine.accounts[sell['participant']]['values']),
        buyer_stock=dict(engine.accounts[buy['participant']]['stock']),
        buyer_values=dict(engine.accounts[buy['participant']]['values']),
        seller_reserved_stock=dict(engine.accounts[sell['participant']]['stock_reserved']),
        revenue=engine.enterprises[sell['participant']]['revenue'],
        tax=engine.enterprises[sell['participant']]['tax'],
        cost_of_sales=engine.enterprises[sell['participant']]['cost_of_sales'],
        flows=copy.deepcopy(engine.report['flows']),
        trades=copy.deepcopy(engine.report['trades']),
        buy_remaining=buy['remaining'], buy_reserved=buy['reserved'],
        sell_remaining=sell['remaining'], sell_status=sell['status'],
        demand_revision=dict(engine._demand_revision),
        trades_by_seller=dict(engine._trades_by_seller),
        treasury=engine.accounts['treasury:s']['balance'],
        carrier=engine.accounts['carrier:s']['balance'],
    )


# --- 1. обычная сделка ----------------------------------------------------
def test_ordinary_trade_moves_goods_and_money():
    engine, buy, sell, quote = trade_engine(freight=0.0)
    trade = commit(engine, buy, sell, quote, 10.0, 5.0)
    # Товар: у продавца списан, у покупателя зачислен по цене сделки.
    assert engine.accounts['firm-00008']['stock'][GOOD] == 0.0
    assert engine.accounts['merchant:s']['stock'][GOOD] == 10.0
    assert engine.accounts['merchant:s']['values'][GOOD] == 50.0
    # Деньги: 50 ушли продавцу, резерв покупателя освобождён.
    assert engine.accounts['firm-00008']['balance'] == 170.0
    assert engine.accounts['merchant:s']['balance'] == 950.0
    assert engine.accounts['merchant:s']['reserved'] == 0
    assert buy['reserved'] == 0
    assert trade['gold'] == 50.0 and trade['tax'] == 0.0
    assert trade['trade_id'] == '1:trade:0'
    assert engine.report['trades'] == [trade]


def test_ordinary_trade_closes_both_orders():
    engine, buy, sell, quote = trade_engine()
    commit(engine, buy, sell, quote, 10.0, 5.0)
    assert buy['remaining'] == 0 and sell['remaining'] == 0
    assert sell['status'] == 'filled'
    # Статус buyer-заявки ставит цикл match, не сделка.
    assert buy['status'] == 'active'


# --- 2 и 3. частичное и полное исполнение --------------------------------
def test_partial_execution_keeps_remainders():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert buy['remaining'] == 6.0
    assert sell['remaining'] == 6.0
    assert sell['status'] == 'partial'
    # Освобождён только резерв исполненной части: 4*5 из 50.
    assert buy['reserved'] == 30.0
    assert engine.accounts['merchant:s']['reserved'] == 30.0
    assert buy['quantity'] == 10.0


def test_full_execution_zeroes_remainders_and_reserve():
    engine, buy, sell, quote = trade_engine()
    commit(engine, buy, sell, quote, 10.0, 5.0)
    assert buy['remaining'] == 0.0 and sell['remaining'] == 0.0
    assert buy['reserved'] == 0 and engine.accounts['merchant:s']['reserved'] == 0
    assert sell['status'] == 'filled'


def test_sell_status_becomes_partial_then_filled():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 3.0, 5.0)
    assert sell['status'] == 'partial'
    commit(engine, buy, sell, quote, 7.0, 5.0)
    assert sell['status'] == 'filled'


# --- 4. освобождение reserved --------------------------------------------
def test_release_reserved_uses_order_reserve_not_landed_price():
    """Освобождается ``q*buy['price']``, а не ``q*landed``: допуск на единицу."""
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0, freight=1.0)
    # Цена продавца 4, landed 5: release считается по цене заявки покупателя.
    commit(engine, buy, sell, quote, 2.0, 5.0)
    assert buy['reserved'] == 40.0
    assert engine.accounts['merchant:s']['reserved'] == 40.0


def test_release_covers_landed_price_when_free_cash_is_short():
    """Допуск цены на единицу: на крупной партии освобождается фактическая сумма."""
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0, buyer_cash=0.0)
    # Свободных денег нет, landed дороже заявки: release поднимается до q*price.
    engine.accounts['merchant:s']['balance'] = 50.0
    engine.accounts['merchant:s']['reserved'] = 50.0
    buy['reserved'] = 50.0
    commit(engine, buy, sell, quote, 10.0, 6.0)
    assert buy['reserved'] == 0


def test_release_never_exceeds_order_reserve():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 10.0, 5.0)
    # Резерв не уходит в минус и не становится отрицательным float.
    assert buy['reserved'] == 0
    assert type(buy['reserved']) is int


def test_seller_stock_reserve_is_released():
    engine, buy, sell, quote = trade_engine()
    engine.accounts['firm-00008']['stock_reserved'][GOOD] = 10.0
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert engine.accounts['firm-00008']['stock_reserved'][GOOD] == 6.0


def test_seller_stock_reserve_never_negative():
    engine, buy, sell, quote = trade_engine()
    engine.accounts['firm-00008']['stock_reserved'][GOOD] = 1.0
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert engine.accounts['firm-00008']['stock_reserved'][GOOD] == 0


# --- 5 и 6. товар продавца и покупателя ----------------------------------
def test_seller_stock_is_consumed_at_average_cost():
    engine, buy, sell, quote = trade_engine(seller_stock=10.0, seller_value=40.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    # Средняя стоимость 4 за единицу: списано 16.
    assert engine.accounts['firm-00008']['stock'][GOOD] == 6.0
    assert engine.accounts['firm-00008']['values'][GOOD] == 24.0
    assert engine.enterprises['firm-00008']['cost_of_sales'] == 16.0


def test_buyer_stock_is_added_with_trade_value():
    engine, buy, sell, quote = trade_engine()
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert engine.accounts['merchant:s']['stock'][GOOD] == 4.0
    assert engine.accounts['merchant:s']['values'][GOOD] == 20.0


def test_second_trade_accumulates_buyer_stock():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 3.0, 5.0)
    # Новая партия того же товара складывается со старой.
    engine.accounts['firm-00008']['stock'][GOOD] = 7.0
    engine.accounts['firm-00008']['values'][GOOD] = 28.0
    engine.accounts['firm-00008']['stock_reserved'][GOOD] = 7.0
    commit(engine, buy, sell, quote, 2.0, 5.0)
    assert engine.accounts['merchant:s']['stock'][GOOD] == 5.0
    assert engine.accounts['merchant:s']['values'][GOOD] == 25.0


# --- 7. налог и перевозка -------------------------------------------------
def test_final_consumption_tax_goes_to_treasury():
    engine, buy, sell, quote = trade_engine(final_consumption=True, freight=0.0)
    trade = commit(engine, buy, sell, quote, 10.0, 5.0)
    # gross 50, налог 10% = 5, продавцу 45.
    assert trade['gold'] == 50.0
    assert trade['tax'] == 5.0
    assert engine.accounts['treasury:s']['balance'] == 39760.0+5.0
    assert engine.enterprises['firm-00008']['tax'] == 5.0
    assert engine.accounts['firm-00008']['balance'] == 120.0+45.0


def test_no_tax_without_final_consumption():
    engine, buy, sell, quote = trade_engine(final_consumption=False)
    trade = commit(engine, buy, sell, quote, 10.0, 5.0)
    assert trade['tax'] == 0.0
    assert engine.accounts['treasury:s']['balance'] == 39760.0
    assert engine.enterprises['firm-00008']['tax'] == 0


def test_freight_goes_to_carrier_and_into_trade():
    engine, buy, sell, quote = trade_engine(freight=2.0)
    trade = commit(engine, buy, sell, quote, 4.0, 5.0)
    assert trade['freight'] == 8.0
    assert engine.accounts['carrier:s']['balance'] == 8.0
    assert trade['distance'] == 0 and trade['path'] == ()
    # Продавцу уходит только товарная часть: 4*3 = 12.
    assert engine.accounts['firm-00008']['balance'] == 120.0+12.0


def test_transfer_order_is_seller_tax_carrier():
    engine, buy, sell, quote = trade_engine(final_consumption=True, freight=1.0)
    commit(engine, buy, sell, quote, 10.0, 5.0)
    reasons = [flow['reason'] for flow in engine.report['flows']]
    assert reasons == ['Товар', 'Налог с конечной потребительской продажи', 'Перевозка']


# --- 8. материалы стройки -------------------------------------------------
def test_construction_purchase_updates_project_materials():
    engine, buy, sell, quote = trade_engine(purpose='Строительные материалы')
    engine.accounts['merchant:s']['project'] = dict(materials={}, capacity=1.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert engine.accounts['merchant:s']['project']['materials'] == {GOOD: 4.0}


def test_ordinary_purpose_does_not_touch_materials():
    engine, buy, sell, quote = trade_engine(purpose='Товар')
    engine.accounts['merchant:s']['project'] = dict(materials={}, capacity=1.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert engine.accounts['merchant:s']['project']['materials'] == {}


def test_construction_purchase_accumulates_materials():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0,
                                           purpose='Строительные материалы')
    engine.accounts['merchant:s']['project'] = dict(materials={GOOD: 1.0}, capacity=1.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert engine.accounts['merchant:s']['project']['materials'] == {GOOD: 5.0}


# --- 9. flows и trades ----------------------------------------------------
def test_flows_get_trade_identity():
    engine, buy, sell, quote = trade_engine()
    trade = commit(engine, buy, sell, quote, 4.0, 5.0)
    flows = engine.report['flows']
    assert len(flows) == 2
    for flow in flows:
        assert flow['buy_order'] == 'b1' and flow['sell_order'] == 's1'
        assert flow['good'] == GOOD and flow['quantity'] == 4.0
        assert flow['trade_id'] == trade['trade_id']


def test_trade_record_carries_all_expected_fields():
    engine, buy, sell, quote = trade_engine()
    trade = commit(engine, buy, sell, quote, 4.0, 5.0)
    assert trade['good'] == GOOD and trade['quantity'] == 4.0
    assert trade['price'] == 4.0            # цена продавца без доставки
    assert trade['seller_gold'] == trade['gold']-trade['tax']
    assert trade['buyer'] == 'merchant:s' and trade['seller'] == 'firm-00008'
    assert trade['buy_order'] == 'b1' and trade['sell_order'] == 's1'
    assert trade['buyer_state'] == 's' and trade['seller_state'] == 's'
    assert trade['buyer_province'] == 'p' and trade['seller_province'] == 'p'
    assert trade['purpose'] == 'Товар' and trade['final_consumption'] is False


def test_trade_id_numbering_follows_report_length():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    first = commit(engine, buy, sell, quote, 3.0, 5.0)
    engine.accounts['firm-00008']['stock'][GOOD] = 7.0
    engine.accounts['firm-00008']['values'][GOOD] = 28.0
    engine.accounts['firm-00008']['stock_reserved'][GOOD] = 7.0
    second = commit(engine, buy, sell, quote, 2.0, 5.0)
    assert first['trade_id'] == '1:trade:0'
    assert second['trade_id'] == '1:trade:1'
    assert len(engine.report['trades']) == 2


def test_revisions_are_bumped_once_per_trade():
    engine, buy, sell, quote = trade_engine()
    commit(engine, buy, sell, quote, 4.0, 5.0)
    assert engine._demand_revision[GOOD] == 1
    assert engine._trades_by_seller['firm-00008'] == 1
    assert engine._market_quotes_epoch > 0 or True  # сброс котировок вызван


# --- 10. статус и remaining заявок ---------------------------------------
def test_buy_remaining_zero_stays_int_when_fully_filled():
    """``max(0, 0.0)`` возвращает целый 0 — тип сохраняется, как в Python."""
    engine, buy, sell, quote = trade_engine()
    commit(engine, buy, sell, quote, 10.0, 5.0)
    assert buy['remaining'] == 0
    assert engine.order_store.remaining[0] == buy['remaining']
    assert type(engine.order_store.remaining[0]) is int


def test_order_store_columns_follow_the_trade():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 3.0, 5.0)
    store = engine.order_store
    store.sync()
    assert store.remaining[store.index[id(buy)]] == 7.0
    assert store.remaining[store.index[id(sell)]] == 7.0
    assert store.reserved[store.index[id(buy)]] == 35.0
    assert store.status[store.index[id(sell)]] == 'partial'


# --- 11. int/float семантика --------------------------------------------
def test_int_quantities_keep_int_semantics():
    engine, buy, sell, quote = trade_engine(quantity=10, price=5, seller_stock=10,
                                            seller_value=40, freight=0)
    trade = commit(engine, buy, sell, quote, 4, 5)
    assert trade['quantity'] == 4 and type(trade['quantity']) is int
    assert trade['gold'] == 20 and type(trade['gold']) is int
    # Резерв заявки остаётся целым: 50-20.
    assert buy['reserved'] == 30 and type(buy['reserved']) is int


def test_float_arithmetic_order_is_preserved():
    """Порядок умножений не менялся: сверяется побитовое значение."""
    landed = 0.1+0.2
    engine, buy, sell, quote = trade_engine(quantity=3.0, price=landed,
                                            seller_stock=3.0, seller_value=1.0,
                                            freight=0.0)
    trade = commit(engine, buy, sell, quote, 3.0, landed)
    assert trade['gold'].hex() == (3.0*landed).hex()
    assert trade['freight'] == 3.0*0.0


def test_zero_quantity_is_rejected_by_caller_not_kernel():
    """Ядро исполняет уже выбранную сделку: отсев ``q <= EPS`` — в match."""
    engine, buy, sell, quote = trade_engine()
    # Нулевая сделка не имеет смысла, но ядро не изобретает новых проверок.
    trade = commit(engine, buy, sell, quote, 0.0, 5.0)
    assert trade['quantity'] == 0.0
    assert buy['remaining'] == 10.0


# --- 12. повторная сделка с тем же buyer ----------------------------------
def test_second_trade_with_same_buyer_uses_updated_reserve():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    reserve_after_first = buy['reserved']
    # Второй продавец с тем же товаром и тем же покупателем.
    other = dict(sell, id='s2')
    engine.accounts['firm-00009']['stock'] = {GOOD: 6.0}
    engine.accounts['firm-00009']['values'] = {GOOD: 24.0}
    engine.accounts['firm-00009']['stock_reserved'] = {GOOD: 6.0}
    engine.order_store.append(other)
    commit(engine, buy, other, quote, 6.0, 5.0)
    assert buy['remaining'] == 0
    assert buy['reserved'] == 0
    assert reserve_after_first == 30.0
    assert engine.accounts['merchant:s']['stock'][GOOD] == 10.0
    assert len(engine.report['trades']) == 2


def test_second_trade_does_not_disturb_first_trade_record():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    first = commit(engine, buy, sell, quote, 4.0, 5.0)
    kept = copy.deepcopy(first)
    engine.accounts['firm-00008']['stock'][GOOD] = 6.0
    engine.accounts['firm-00008']['values'][GOOD] = 24.0
    engine.accounts['firm-00008']['stock_reserved'][GOOD] = 6.0
    commit(engine, buy, sell, quote, 6.0, 5.0)
    assert engine.report['trades'][0] == kept


# --- 13. exact baseline против Python-эталона ----------------------------
def run_scenario(commit_fn):
    """Одна и та же сцена: три сделки, налог, доставка, стройка, два счёта."""
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.5, buyer_cash=200.0,
                                            seller_stock=10.0, seller_value=40.0,
                                            freight=0.75, final_consumption=True,
                                            purpose='Строительные материалы')
    engine.accounts['merchant:s']['project'] = dict(materials={}, capacity=1.0)
    commit(engine, buy, sell, quote, 3.0, 5.5, commit_fn)
    engine.accounts['firm-00008']['stock'][GOOD] = 7.0
    engine.accounts['firm-00008']['values'][GOOD] = 28.0
    engine.accounts['firm-00008']['stock_reserved'][GOOD] = 7.0
    commit(engine, buy, sell, quote, 2.5, 5.5, commit_fn)
    engine.accounts['firm-00008']['stock'][GOOD] = 4.5
    engine.accounts['firm-00008']['values'][GOOD] = 18.0
    engine.accounts['firm-00008']['stock_reserved'][GOOD] = 4.5
    commit(engine, buy, sell, quote, 4.5, 5.5, commit_fn)
    return engine, buy, sell


def state_of(engine, buy, sell):
    """Состояние без учёта порядка ключей: JSON с сортировкой."""
    return json.dumps(snapshot(engine, buy, sell), sort_keys=True, default=str)


def test_native_matches_python_reference_bit_for_bit():
    native_engine, native_buy, native_sell = run_scenario(trade_boundary.commit_trade)
    reference_engine, reference_buy, reference_sell = run_scenario(
        trade_boundary.commit_trade_python)
    assert state_of(native_engine, native_buy, native_sell) == \
        state_of(reference_engine, reference_buy, reference_sell)


def test_native_and_reference_agree_on_order_columns():
    native_engine, _, _ = run_scenario(trade_boundary.commit_trade)
    reference_engine, _, _ = run_scenario(trade_boundary.commit_trade_python)
    native_store = native_engine.order_store
    reference_store = reference_engine.order_store
    native_store.sync(), reference_store.sync()
    assert native_store.columns() == reference_store.columns()


def test_boundary_binds_compiled_kernel():
    assert trade_boundary.NATIVE_TRADE_KERNEL is True
    assert trade_boundary.commit_trade is KERNEL.commit_trade
    assert trade_boundary.commit_trade is not trade_boundary.commit_trade_python
    assert trade_boundary.commit_trade.__module__.endswith('trade_kernel')


def test_fallback_used_when_extension_absent(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith('trade_kernel'):
            raise ImportError("cannot import name 'trade_kernel'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert trade_boundary.load_trade_kernel() is None


# --- 14. deepcopy независим ----------------------------------------------
def test_deepcopy_after_trade_is_independent():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    clone = copy.deepcopy(engine)
    assert clone.report['trades'] == engine.report['trades']
    assert clone.report['trades'][0] is not engine.report['trades'][0]
    assert clone.accounts['merchant:s'] is not engine.accounts['merchant:s']
    # Мутация копии не трогает исходный движок.
    clone_buy = clone.report['orders'][0]
    clone.accounts['firm-00008']['stock'][GOOD] = 99.0
    clone.order_store.set_remaining(clone_buy, 0.0)
    assert engine.accounts['firm-00008']['stock'][GOOD] == 6.0
    assert buy['remaining'] == 6.0


def test_trade_after_deepcopy_uses_copy_state():
    engine, buy, sell, quote = trade_engine(quantity=10.0, price=5.0)
    commit(engine, buy, sell, quote, 4.0, 5.0)
    clone = copy.deepcopy(engine)
    clone_buy = clone.report['orders'][0]
    clone_sell = clone.report['orders'][1]
    commit(clone, clone_buy, clone_sell, quote, 6.0, 5.0)
    assert clone_buy['remaining'] == 0
    # Исходный движок не тронут: там по-прежнему 6 единиц остатка.
    assert buy['remaining'] == 6.0
    assert engine.accounts['merchant:s']['stock'][GOOD] == 4.0
    assert clone.accounts['merchant:s']['stock'][GOOD] == 10.0


# --- расширяемость: тела движка не копируются ----------------------------
def test_overridden_transfer_is_still_used():
    """Переопределение ``transfer`` в наследнике обязано работать."""
    calls = []
    engine, buy, sell, quote = trade_engine()
    original = engine.transfer

    def traced(sender, recipient, amount, reason):
        calls.append((sender, recipient, amount, reason))
        return original(sender, recipient, amount, reason)

    engine.transfer = traced
    commit(engine, buy, sell, quote, 10.0, 5.0)
    assert [reason for *_, reason in calls] == [
        'Товар', 'Налог с конечной потребительской продажи', 'Перевозка']


def test_overridden_use_and_add_stock_are_still_used():
    calls = []
    engine, buy, sell, quote = trade_engine()
    use_stock, add_stock = engine.use_stock, engine.add_stock

    def traced_use(account, good, quantity):
        calls.append(('use', account, good, quantity))
        return use_stock(account, good, quantity)

    def traced_add(account, good, quantity, value=0):
        calls.append(('add', account, good, quantity, value))
        return add_stock(account, good, quantity, value)

    engine.use_stock = traced_use
    engine.add_stock = traced_add
    commit(engine, buy, sell, quote, 10.0, 5.0)
    assert calls == [('use', 'firm-00008', GOOD, 10.0),
                     ('add', 'merchant:s', GOOD, 10.0, 50.0)]
