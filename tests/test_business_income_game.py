import copy

import pytest

from modules.economy import Economy
from scripts.research_money_cycle import MoneyCycleEconomy
from test_economy_v3 import engine, firm, clear


def test_default_pays_households_and_keeps_legal_owner(engine):
    clear(engine);f=firm(engine,'fish');f['profit']=100
    engine.accounts[f['id']]['balance']=1000
    owner=engine.accounts[f['owner_id']];before_owner=owner['balance'];before=engine.accounts['h']['balance']
    gold=engine.gold();engine.pay_dividends({})
    assert engine.accounts['h']['balance']-before==100
    assert owner['balance']==before_owner and f['owner_id']==owner['id']
    assert engine.report['dividends'][-1]['recipient_kind']=='household'
    assert engine.gold()==pytest.approx(gold)
    engine.pay_dividends({});assert engine.accounts['h']['balance']-before==100


def test_game_matches_independent_income_experiment(engine):
    expected=copy.deepcopy(engine);expected.__class__=MoneyCycleEconomy;expected.variant='profit_income'
    for _ in range(12):
        engine.step();expected.step()
        assert engine.accounts==expected.accounts
        assert engine.enterprises==expected.enterprises
        a,b=copy.deepcopy(engine.meta),copy.deepcopy(expected.meta)
        a.pop('profit_distribution_revision');b.pop('profit_distribution_revision')
        assert a==b and engine.rng.getstate()==expected.rng.getstate()


def test_business_income_share_is_validated(engine):
    engine.cfg['business_income_share']=float('nan')
    with pytest.raises(ValueError):engine.activate_rules()


def test_default_keeps_project_and_working_reserves(engine):
    from test_economy_owners import pending_project

    clear(engine)
    f = firm(engine, 'fish')
    f['profit'] = 100
    account = engine.accounts[f['id']]
    pending_project(engine, account, cash=150)
    required = engine.project_cash_reserve(f['id'], {}) + engine.working_cash_reserve(f, {})
    account.update(balance=required + 7, protected=0)
    engine.pay_dividends({})
    assert f['dividends'] == pytest.approx(7)
    assert account['balance'] == pytest.approx(required)


@pytest.mark.parametrize('profit, expected', [(100, 1), (-1, 0)])
def test_default_respects_protected_money_and_profit(engine, profit, expected):
    clear(engine)
    f = firm(engine, 'fish')
    f['profit'] = profit
    account = engine.accounts[f['id']]
    account.update(balance=1000, protected=999)
    engine.pay_dividends({})
    assert f['dividends'] == expected
    assert account['balance'] == 1000 - expected
