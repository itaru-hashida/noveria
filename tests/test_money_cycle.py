import copy

import pytest

from scripts.research_money_cycle import MoneyCycleEconomy, record, weighted_median
from test_economy_v3 import engine, firm, clear
from scripts.experiment_owner_profit import OwnerProfitEconomy
from test_economy_owners import pending_project


def policy(engine, variant):
    engine.__class__=MoneyCycleEconomy
    engine.variant=variant
    return engine


def test_control_observer_changes_no_state_or_rng(engine):
    engine.PROFIT_DISTRIBUTION_REVISION=0
    observed=policy(copy.deepcopy(engine),'control')
    for _ in range(4):
        engine.step();observed.step();record(observed)
        for key in ('accounts','enterprises','meta'):
            assert getattr(engine,key)==getattr(observed,key)
        assert engine.rng.getstate()==observed.rng.getstate()


def test_profit_income_distributes_profit_not_existing_wealth_and_is_idempotent(engine):
    policy(engine,'profit_income');clear(engine)
    f=firm(engine,'fish');f['profit']=100
    a=engine.accounts[f['id']];a['balance']=1000
    owner=engine.accounts[f['owner_id']];before_owner=owner['balance']
    gold=engine.gold();household=engine.accounts['h'];before=household['balance']
    engine.pay_dividends({})
    assert household['balance']-before==100
    assert a['balance']==900 and owner['balance']==before_owner
    assert f['dividends']==100
    assert engine.gold()==pytest.approx(gold)
    engine.pay_dividends({})
    assert household['balance']-before==100


def test_profit_income_respects_protected_cash_and_does_not_pay_losses(engine):
    policy(engine,'profit_income');clear(engine)
    f=firm(engine,'fish');f['profit']=100
    a=engine.accounts[f['id']];a.update(balance=1000,protected=999)
    engine.pay_dividends({})
    assert f['dividends']==1 and a['balance']==999
    engine.meta['month']+=1;f['profit']=-1
    engine.pay_dividends({})
    assert f['dividends']==0 and a['balance']==999


def test_profit_income_keeps_unprotected_project_and_operating_reserves(engine):
    policy(engine,'profit_income');clear(engine)
    f=firm(engine,'fish');f['profit']=100
    a=engine.accounts[f['id']]
    pending_project(engine,a,cash=150)
    required=engine.project_cash_reserve(f['id'],{})+engine.working_cash_reserve(f,{})
    a.update(balance=required+7,protected=0)
    engine.pay_dividends({})
    assert f['dividends']==pytest.approx(7)
    assert a['balance']==pytest.approx(required)


def test_weighted_median_represents_quantity_not_one_rich_bid():
    assert weighted_median([(1,100),(100,1)])==1
    assert weighted_median([(1,1),(100,100)])==100
    assert weighted_median([]) is None


def test_budget_prices_step_down_even_when_previous_sales_positive(engine):
    policy(engine,'budget_prices');clear(engine)
    f=firm(engine,'bread');f.update(price=2,sales_history=[dict(sold=1)])
    engine.basic_signals=[dict(tile='t',good='bread',missing=100,budget_price=.5),
        dict(tile='t',good='bread',missing=1,budget_price=10)]
    engine.update_prices()
    assert f['price']==pytest.approx(2*(1-engine.rules['price_step']))
    assert engine.budget_price_changes>0


def test_budget_prices_ignore_unreachable_and_nonbasic_goods(engine):
    policy(engine,'budget_prices');clear(engine)
    f=firm(engine,'bread');f.update(price=2,sales_history=[dict(sold=1)])
    grain=firm(engine,'grain');grain.update(price=3,sales_history=[dict(sold=1)])
    engine.graph.add_node('isolated')
    engine.basic_signals=[dict(tile='isolated',good='bread',missing=100,budget_price=.01),
        dict(tile='t',good='grain',missing=100,budget_price=.01)]
    engine.update_prices()
    assert f['price']==2 and grain['price']==3


def test_budget_prices_respect_existing_price_floor(engine):
    policy(engine,'budget_prices');clear(engine)
    floor=engine.rules['minimum_price']
    f=firm(engine,'bread');f.update(price=floor*1.01,sales_history=[dict(sold=1)])
    engine.basic_signals=[dict(tile='t',good='bread',missing=100,budget_price=0)]
    engine.update_prices()
    assert floor <= f['price'] <= floor*1.01


def test_income_affordability_does_not_count_initial_cash_as_income(engine):
    policy(engine,'control')
    engine.cfg.update(public_spending=0,carrier_distribution=0)
    engine.step();r=record(engine)
    assert r['income_coverage_at_quotes']<1
    assert r['funded_basic_share']==pytest.approx(1)
    assert r['household_cash']>sum(r['household_incoming'].values())


def test_owner_ablation_changes_payout_rate_but_keeps_recipient_and_config(engine):
    engine.__class__=OwnerProfitEconomy;engine.variant='owner_profit';clear(engine)
    f=firm(engine,'fish');f['profit']=100;engine.accounts[f['id']]['balance']=1000
    owner=engine.accounts[f['owner_id']];before=owner['balance']
    share=engine.cfg['dividend_profit_share']
    engine.pay_dividends({})
    assert owner['balance']-before==100
    assert engine.cfg['dividend_profit_share']==share
