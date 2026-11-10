"""Регрессии ожиданий и бюджета экспериментального планировщика."""
import numpy as np
import pytest

from modules.economy import Economy
from shadow import agent_planner, agent_snapshot


@pytest.fixture
def market(service):
    service.initialize_economy()
    engine = Economy.load(service.db)
    arrays, meta = agent_snapshot.build(engine, {f: 10000 for f in engine.enterprises})
    parameters = tuple(engine.cfg[k] for k in
                       ('transport_rate', 'wage', 'consumption_tax', 'markup', 'enterprise_overhead_rate'))
    return engine, arrays, meta, parameters


def test_history_survives_empty_order_book(market):
    _, arrays, meta, parameters = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_stock'][agent] = 0
    arrays['agent_available_stock'][agent] = 0
    arrays['agent_history_count'][agent] = 1
    arrays['agent_history'][agent, 0] = (20, 20, 0)
    arrays['agent_forecast'][agent] = 20
    trace = {}
    intents = agent_planner.plan(arrays, agent, *parameters, trace=trace)
    assert trace['observed'] == 0
    assert trace['forecast'] == pytest.approx(20)
    assert trace['target'] > 0
    assert any(i[0] == 'buy' and i[2] > 0 for i in intents)
    assert intents[0][5] == trace['target']


def test_budget_protects_payroll_and_respects_existing_inputs(market):
    _, arrays, meta, parameters = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_stock'][agent] = 0
    arrays['agent_available_stock'][agent] = 0
    arrays['agent_budget'][agent] = 2.0
    arrays['agent_forecast'][agent] = 100
    arrays['agent_history_count'][agent] = 1
    arrays['agent_history'][agent, 0] = (100, 100, 0)
    trace = {}
    intents = agent_planner.plan(arrays, agent, *parameters, trace=trace)
    spend = sum(i[2]*i[3] for i in intents if i[0] == 'buy')
    assert spend+trace['wage_reserve']+trace['overhead'] <= 2.0+1e-7
    assert intents[0][5] == trace['target'] < trace['desired']


def test_input_shortage_preserves_forecast_but_unsold_stock_stops_output(market):
    _, arrays, meta, _ = market
    agent = meta['firm_good'].index('flour')
    arrays['agent_history_count'][agent] = 1
    arrays['agent_history'][agent, 0] = (0, 0, 1)
    arrays['agent_forecast'][agent] = 20
    assert agent_planner.expectations(arrays, agent, 0) == pytest.approx(20)
    arrays['agent_history'][agent, 0, 2] = 0
    assert agent_planner.expectations(arrays, agent, 0) < 20


def test_new_firm_uses_public_history(market):
    _, arrays, meta, parameters = market
    agent = meta['firm_good'].index('flour')
    good = int(arrays['agent_good'][agent])
    arrays['agent_history_count'][agent] = 0
    arrays['agent_stock'][agent] = 0
    arrays['agent_available_stock'][agent] = 0
    arrays['public_share'][good] = 10
    trace = {}
    agent_planner.plan(arrays, agent, *parameters, trace=trace)
    assert trace['forecast'] == 10
    assert trace['target'] > 0
