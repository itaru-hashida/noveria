import pytest

from test_economy_v3 import engine, clear


def market(engine, stock=10, protected=0, distance=0):
    clear(engine)
    for a in engine.accounts.values():
        a['stock'] = {}
    engine.cfg['basic_needs'] = {'bread': .0001, 'fish': .0001}
    engine.accounts['h'].update(balance=10, protected=protected)
    engine.add_stock('merchant:s', 'bread', stock, stock)
    if distance:
        engine.graph.add_edge('remote', 't', weight=distance)
        engine.paths.clear()
        engine.accounts['merchant:s']['tile_id'] = 'remote'
    sell = engine.order('merchant:s', 'bread', stock, 'sell', 1, 'Продажа запасов')
    sells = {'bread': [sell]} if sell else {}
    households = [engine.accounts['h']]
    engine.household_requests(households, engine.cfg['basic_needs'], 'Базовые нужды', sells)
    for buy in engine.shuffled([o for o in engine.report['orders'] if o['side'] == 'buy']):
        engine.match(buy, sells)
    return households, sells


def test_freed_fish_budget_buys_bread_without_overbuying_or_new_gold(engine):
    hs, sells = market(engine, stock=20)
    initial_bread = engine.accounts['h']['stock']['bread']
    assert initial_bread < 10
    gold = engine.gold()
    engine.household_top_up(hs, sells)
    assert engine.accounts['h']['stock']['bread'] == pytest.approx(10)
    assert engine.accounts['h']['balance'] == pytest.approx(0)
    assert engine.gold() == pytest.approx(gold)
    assert engine.accounts['merchant:s']['stock']['bread'] == pytest.approx(10)
    assert engine.report['household_top_up']['by_good']['bread'] == pytest.approx(10-initial_bread)
    assert engine.report['household_top_up']['gold'] == pytest.approx(10-initial_bread)
    engine.record_market()
    row = next(r for r in engine.meta['market_history'][-1]['markets'] if r['good'] == 'bread')
    assert row['volume'] == pytest.approx(10)
    assert row['requested'] == pytest.approx(10)
    assert not any(o['good'] == 'bread' for o in engine.meta['public_orders'])


def test_top_up_pays_delivery_and_preserves_protected_cash(engine):
    hs, sells = market(engine, protected=2, distance=10)
    engine.household_top_up(hs, sells)
    assert engine.accounts['h']['balance'] == pytest.approx(2)
    assert engine.accounts['h']['stock']['bread'] == pytest.approx(8/1.08)
    assert sum(t['freight'] for t in engine.report['trades']) > 0


def test_top_up_cannot_buy_unoffered_stock_or_future_income(engine):
    hs, sells = market(engine, stock=2)
    engine.add_stock('merchant:s', 'bread', 20, 20)
    engine.household_top_up(hs, sells)
    assert engine.accounts['h']['stock']['bread'] == pytest.approx(2)
    assert engine.accounts['merchant:s']['stock']['bread'] == pytest.approx(20)
    assert engine.report['household_top_up']['quantity'] == 0
    engine.record_market()
    bread = next(r for r in engine.meta['market_history'][-1]['markets'] if r['good'] == 'bread')
    assert bread['volume'] == pytest.approx(2)
    assert bread['unmet_solvent'] > 0
    assert sum(o['reserved'] for o in engine.meta['public_orders']) <= engine.accounts['h']['balance'] + 1e-8


def test_step_uses_top_up_before_production_and_records_invariants(engine):
    seen = []
    original = engine.produce
    def produce(*args):
        seen.append('household_top_up' in engine.report)
        return original(*args)
    engine.produce = produce
    report = engine.step()
    assert seen == [True]
    assert all(report['invariants'].values())
    assert engine.meta['household_top_up_revision'] == 1


def test_top_up_keeps_poor_buyer_price_signal_after_cash_is_spent(engine):
    hs, sells = market(engine, stock=20)
    engine.household_top_up(hs, sells)
    engine.record_market()
    assert engine.accounts['h']['balance'] == pytest.approx(0)
    feedback = engine.meta['price_feedback']
    assert [o['good'] for o in feedback] == ['fish']
    assert feedback[0]['budget_price'] > 0
    assert feedback[0]['superseded_by_top_up']
    assert not engine.meta['public_orders']
    fish = next(f for f in engine.enterprises.values() if f['good'] == 'fish')
    fish.update(price=10, sales_history=[dict(sold=10), dict(sold=5)])
    engine.update_prices()
    assert fish['price'] == pytest.approx(10*(1-engine.rules['price_step']))


def test_rebuilt_orders_do_not_duplicate_price_feedback(engine):
    hs, sells = market(engine, stock=2)
    engine.household_top_up(hs, sells)
    engine.record_market()
    keys = [(o['participant'], o['good']) for o in engine.meta['price_feedback']]
    assert set(keys) == {('h', 'bread'), ('h', 'fish')}
    assert len(keys) == len(set(keys))
