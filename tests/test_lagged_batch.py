"""Регрессии последовательного lagged_batch без параллельного исполнения."""
import copy
from collections import defaultdict

import pytest

from modules.economy.support import EPS
from shadow.lagged_batch import apply_plans, build_snapshot, plan_all, publish_sales


def initialized_engine(service):
    service.initialize_economy()
    return service.economy_state().engine


def engine_state(engine):
    return (
        copy.deepcopy(engine.accounts),
        copy.deepcopy(engine.enterprises),
        copy.deepcopy(engine.meta),
        copy.deepcopy(engine.report),
        engine.rng.getstate(),
    )


def snapshot_state(snapshot):
    return (
        {name: array.tobytes() for name, array in snapshot.arrays.items()},
        copy.deepcopy(snapshot.meta),
        snapshot.parameters,
        snapshot.eligible,
    )


def test_plan_does_not_change_engine_or_snapshot(service):
    engine = initialized_engine(service)
    snapshot = build_snapshot(engine)
    before_engine = engine_state(engine)
    before_snapshot = snapshot_state(snapshot)

    plans = plan_all(snapshot)

    assert plans
    assert engine_state(engine) == before_engine
    assert snapshot_state(snapshot) == before_snapshot
    assert all(not array.flags.writeable for array in snapshot.arrays.values())


def test_same_snapshot_and_firm_traversal_give_same_plans(service):
    engine = initialized_engine(service)
    snapshot = build_snapshot(engine)

    first = plan_all(snapshot)
    second = plan_all(snapshot)
    reversed_order = tuple(reversed(snapshot.eligible))
    reversed_plans = plan_all(snapshot, reversed_order)

    assert first == second
    assert first == reversed_plans
    assert list(reversed_plans) == list(reversed_order)


def test_apply_respects_cash_stock_and_actual_clearing(service):
    engine = initialized_engine(service)
    engine.meta['month'] = 1
    engine.reset_report()
    snapshot = build_snapshot(engine)
    plans = plan_all(snapshot)
    opening_cash = {firm_id: engine.accounts[firm_id]['balance']
                    for firm_id in snapshot.eligible}
    opening_stock = {firm_id: dict(engine.accounts[firm_id]['stock'])
                     for firm_id in snapshot.eligible}

    sells = publish_sales(engine, plans, defaultdict(list))
    apply_plans(engine, plans)

    for firm_id in snapshot.eligible:
        account = engine.accounts[firm_id]
        assert account['reserved']+account['protected'] <= opening_cash[firm_id]+EPS
        for good, quantity in account['stock_reserved'].items():
            assert quantity <= opening_stock[firm_id].get(good, 0.0)+EPS
    for firm_id, item in plans.items():
        offers = [order for order in sells[item.good]
                  if order['participant'] == firm_id]
        assert sum(order['quantity'] for order in offers) <= \
            opening_stock[firm_id].get(item.good, 0.0)+EPS

    for order in sorted((row for row in engine.report['orders'] if row['side'] == 'buy'),
                        key=lambda row: row['id']):
        engine.match(order, sells)
    engine.validate()
    assert all(account['balance'] >= -EPS for account in engine.accounts.values())
    assert all(quantity >= -EPS for account in engine.accounts.values()
               for quantity in account['stock'].values())


def test_household_requests_see_and_buy_published_opening_stock(service, monkeypatch):
    engine = initialized_engine(service)
    observed = []
    original = engine.household_requests

    def observe_firm_offers(households, needs, purpose, sells):
        if purpose == 'Базовые нужды' and not observed:
            offers = [order for rows in sells.values() for order in rows
                      if order['participant'] in engine.enterprises]
            observed.extend(offers)
            assert offers
            assert all(order['quantity'] <= engine.accounts[order['participant']]['stock'].get(
                order['good'], 0.0)+EPS for order in offers)
        return original(households, needs, purpose, sells)

    monkeypatch.setattr(engine, 'household_requests', observe_firm_offers)
    report = engine.step(planning_mode='lagged_batch')

    assert observed
    assert any(trade['seller'] in engine.enterprises
               and engine.accounts[trade['buyer']]['kind'] == 'household'
               for trade in report['trades'])
    published = {(order['participant'], order['good']) for order in observed}
    for firm_id, good in published:
        assert sum(order['side'] == 'sell' and order['participant'] == firm_id
                   and order['good'] == good for order in report['orders']) == 1


def test_full_step_materializes_only_lagged_firm_intents(service, monkeypatch):
    engine = initialized_engine(service)

    def legacy_path_is_disabled(*args, **kwargs):
        raise AssertionError('lagged_batch не должен входить в старый путь фирм')

    monkeypatch.setattr(engine, 'input_requests', legacy_path_is_disabled)
    monkeypatch.setattr(engine, 'recover_enterprises', legacy_path_is_disabled)
    report = engine.step(planning_mode='lagged_batch')

    for firm_id, firm in engine.enterprises.items():
        if firm['status'] == 'closed':
            continue
        product_offers = [order for order in report['orders']
                          if order['side'] == 'sell' and order['participant'] == firm_id
                          and order['good'] == firm['good']]
        assert len(product_offers) <= 1

    firm_buys = [order for order in report['orders']
                 if order['side'] == 'buy' and order['participant'] in engine.enterprises]
    production_buy_ids = {order['id'] for order in firm_buys
                          if order['purpose'] in ('Сырьё', 'Оборудование')}
    planned_input_ids = {order_id for firm in engine.enterprises.values()
                         for order_id in firm.get('production_plan', {}).get('input_orders', [])}
    assert production_buy_ids == planned_input_ids
    assert all(order['id'] in planned_input_ids or order['purpose'] == 'Строительные материалы'
               for order in firm_buys)
    assert not any(order.get('adaptive_input_budget') for order in report['orders'])
    assert not any(order.get('reason') == 'Сырьё возвращено в производство'
                   for order in report['orders'])


def test_twelve_months_validate_and_keep_other_participants(service, monkeypatch):
    engine = initialized_engine(service)

    def recovery_is_disabled(*args, **kwargs):
        raise AssertionError('lagged_batch не должен вызывать recover_enterprises')

    monkeypatch.setattr(engine, 'recover_enterprises', recovery_is_disabled)
    for _month in range(12):
        opening_stock = {firm_id: dict(account['stock'])
                         for firm_id, account in engine.accounts.items()
                         if firm_id in engine.enterprises}
        report = engine.step(planning_mode='lagged_batch')
        assert report['planning_mode'] == 'lagged_batch'
        assert all(report['invariants'].values())
        assert any(order['participant'] not in engine.enterprises
                   for order in report['orders'])
        for order in report['orders']:
            if order['side'] != 'sell' or order['participant'] not in opening_stock:
                continue
            assert order['quantity'] <= opening_stock[order['participant']].get(
                order['good'], 0.0)+EPS


def test_lagged_mode_rejects_agent_turn_callback(service):
    engine = initialized_engine(service)
    with pytest.raises(ValueError, match='несовместим'):
        engine.step(enterprise_planner=lambda *_args: None,
                    planning_mode='lagged_batch')
