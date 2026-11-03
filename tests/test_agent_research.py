import copy
import json
import subprocess
import sys

import pytest

from scripts.research_agents import ROOT, ObservedEconomy, Probe, capture, opportunity, paired_test
from scripts.summarize_agent_research import holm, recovery
from test_economy_v3 import engine, firm, clear


def test_observer_preserves_accounts_firms_metadata_and_rng(engine):
    observed = copy.deepcopy(engine)
    observed.__class__ = ObservedEconomy
    observed.probe = Probe('control')
    for month in range(1, 4):
        observed.probe.begin(month)
        engine.step()
        observed.step()
        capture(observed)
        for key in ('accounts', 'enterprises', 'meta'):
            assert json.dumps(getattr(engine, key), sort_keys=True) == json.dumps(getattr(observed, key), sort_keys=True)
        assert engine.rng.getstate() == observed.rng.getstate()


def market(engine, wood=2):
    clear(engine)
    engine.__class__ = ObservedEconomy
    engine.probe = Probe('control')
    for a in engine.accounts.values():
        a.update(stock={}, values={})
    bakery = firm(engine)
    a = engine.accounts[bakery['id']]
    a.update(balance=1000, protected=1.2, payroll_protected=1.2)
    engine.add_stock(a['id'], 'wood', wood, wood*1.1)
    engine.add_stock('merchant:s', 'flour', 10, 10)
    sell = engine.order('merchant:s', 'flour', 10, 'sell', 2, 'Продажа')
    buy = engine.order(a['id'], 'flour', 10, 'buy', 1, 'Сырьё')
    record = dict(plan=20, inputs={'flour': .8, 'wood': .1}, prices={'flour': 1, 'wood': 1.1},
        input_orders=[buy['id']], wage_unit=.06, net_price=1.89)
    engine.probe.plans[a['id']] = record
    return buy, {'flour': [sell]}, record


def test_profitable_refusal_has_a_physical_and_financial_witness(engine):
    buy, sells, record = market(engine)
    engine.match(buy, sells)
    witness = engine.probe.decisions[-1]['witness']
    assert buy['remaining'] == 10
    assert witness['quantity'] == 10
    assert witness['conditional_surplus'] == pytest.approx(1.5)
    assert not engine.probe.decisions[-1]['strict_missed']


def test_missing_complement_is_not_called_a_feasible_missed_production(engine):
    buy, sells, record = market(engine, wood=0)
    assert opportunity(engine, buy, sells, record) is None


def test_input_experiment_respects_budget_and_frozen_margin(engine):
    buy, sells, record = market(engine)
    engine.probe.variant = 'input_bid'
    before = engine.gold()
    engine.match(buy, sells)
    assert buy['status'] == 'filled'
    assert buy['price'] == 2
    assert engine.gold() == pytest.approx(before)
    assert engine.accounts[buy['participant']]['protected'] == 1.2
    assert engine.probe.decisions[-1]['changed']


def test_unprofitable_supplier_is_not_a_regret_witness(engine):
    buy, sells, record = market(engine)
    sells['flour'][0]['price'] = 3
    assert opportunity(engine, buy, sells, record) is None


def test_three_seeds_cannot_produce_small_two_sided_sign_flip_p_value():
    result = paired_test([1, 1, 1])
    assert result['n'] == 3
    assert result['p_two_sided'] == .25


def test_cash_experiment_keeps_working_capital_and_conserves_money(engine):
    clear(engine)
    engine.__class__ = ObservedEconomy
    engine.probe = Probe('cash_return')
    producer = firm(engine, 'fish')
    producer['profit'] = 0
    a = engine.accounts[producer['id']]
    a['balance'] = 1000
    reserve = engine.working_cash_reserve(producer, {})
    before = engine.gold()
    engine.pay_dividends({})
    assert a['balance'] == pytest.approx(reserve)
    assert engine.gold() == pytest.approx(before)
    assert engine.probe.cash_return > 0


def test_recovery_is_right_censored_instead_of_reported_as_zero_lag():
    control = [dict(macro={'household_bread': 100}) for _ in range(60)]
    shock = [dict(macro={'household_bread': 50}) for _ in range(60)]
    result = recovery(control, shock)
    assert result['censored']
    assert result['confirmed_lag'] is None
    assert result['horizon'] == 48


def test_recovery_requires_three_consecutive_restored_windows():
    control = [dict(macro={'household_bread': 100}) for _ in range(60)]
    shock = [dict(macro={'household_bread': 0 if 6 <= i < 12 else 100}) for i in range(60)]
    result = recovery(control, shock)
    assert result['first_crossing_lag'] == 3
    assert result['confirmed_lag'] == 5
    assert not result['censored']


def test_multiple_testing_adjustment_does_not_select_the_smallest_p():
    assert holm({'primary': .25, 'secondary': .5}) == {'primary': .5, 'secondary': .5}


def test_fresh_cli_can_prove_observer_parity_without_external_artifacts(tmp_path):
    output = tmp_path/'fresh'
    subprocess.run([sys.executable, str(ROOT/'scripts/research_agents.py'),
        '--seed', '17', '--warmup', '0', '--months', '1', '--out', str(output)],
        cwd=ROOT, check=True, capture_output=True, text=True)
    metadata = json.loads((output/'metadata.json').read_text())
    assert metadata['status'] == 'completed' and metadata['completed'] == 1
    assert metadata['observer_state_parity']
    assert (output/'checkpoint.pickle').is_file()


def test_promoted_policy_is_not_presented_as_a_new_experiment(tmp_path):
    output = tmp_path/'duplicate-policy'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/research_agents.py'),
        '--seed', '17', '--warmup', '0', '--months', '1', '--variant', 'input_bid',
        '--out', str(output)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'уже включена' in result.stderr
    assert not output.exists()


def test_control_observer_records_repricing_by_the_promoted_engine(engine):
    buy, sells, record = market(engine)
    producer = engine.enterprises[buy['participant']]
    producer['production_plan'] = dict(month=engine.meta['month'], output=record['plan'],
        input_ratios=record['inputs'], input_prices=record['prices'],
        expected_net_price=record['net_price'], wage_unit=record['wage_unit'],
        input_orders=record['input_orders'])
    buy['adaptive_input_budget'] = True
    engine.match(buy, sells)
    assert buy['status'] == 'filled'
    assert engine.probe.decisions[-1]['changed']
