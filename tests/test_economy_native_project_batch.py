"""Контракт одного pack/call/apply и запрет Python callbacks в native batch."""
import copy

import pytest

from modules.economy import Economy
from modules.economy.native import update_prices_boundary as boundary

serial_step = pytest.importorskip('modules.economy.native.serial_step')
from test_economy_native_update import engine


@pytest.mark.parametrize('landed_signal', [None, 0.0, 7.25])
def test_compact_reference_offers_preserve_single_participant_exclusion(landed_signal):
    class Context:
        engine = type('Engine', (), {'goods': {'g': {'price': 9.0}}})()

        def book(self, tile, good):
            assert (tile, good) == ('tile', 'g')
            return [
                (1.0, dict(id='01', participant='a', remaining=0.0, status='open')),
                (2.0, dict(id='02', participant='a', remaining=1.0, status='open')),
                (2.5, dict(id='03', participant='a', remaining=1.0, status='open')),
                (3.0, dict(id='04', participant='b', remaining=1.0, status='open')),
                (4.0, dict(id='05', participant='c', remaining=1.0, status='expired')),
                (5.0, dict(id='06', participant='c', remaining=1.0, status='open')),
            ]

        def signal(self, state, province, good):
            assert (state, province, good) == ('s', 'p', 'g')
            return {'landed_price': landed_signal}

    ctx = Context()
    top = boundary._top_live_reference_offers(ctx, 'tile', 'g')
    assert top == [(2.0, 'a'), (3.0, 'b')]
    for excluded in ('a', 'b', 'c', 'other'):
        expected = next((price for price, sell in ctx.book('tile', 'g')
                         if sell['participant'] != excluded and sell['remaining'] > boundary.EPS
                         and sell['status'] not in ('cancelled', 'expired')),
                        landed_signal or ctx.engine.goods['g']['price'])
        actual = boundary._reference_price_from_top(
            ctx, 's', 'p', 'g', excluded, top)
        assert actual == expected


@pytest.mark.parametrize('review', ['full', 'targeted', 'skipped'])
def test_project_batch_one_boundary_without_python_decision_callbacks(monkeypatch, review):
    reference = engine()
    original = Economy.project_requests
    counts = dict(pack=0, native=0, apply=0, material=0)
    inside_native = False

    for name, key in [('pack_project_requests', 'pack'), ('apply_project_requests', 'apply')]:
        function = getattr(boundary, name)

        def counted(*args, _function=function, _key=key, **kwargs):
            counts[_key] += 1
            if _key == 'pack':
                source = args[0]
                before = copy.deepcopy((source.accounts, source.meta, source.report))
                rng_before = source.rng.getstate()
                result = _function(*args, **kwargs)
                assert before == (source.accounts, source.meta, source.report)
                assert rng_before == source.rng.getstate()
                assert 'ctx' not in result[2]
                return result
            return _function(*args, **kwargs)

        monkeypatch.setattr(boundary, name, counted)

    native_entry = serial_step.native_project_requests

    def counted_native(*args):
        nonlocal inside_native
        counts['native'] += 1
        inside_native = True
        try:
            return native_entry(*args)
        finally:
            inside_native = False

    monkeypatch.setattr(serial_step, 'native_project_requests', counted_native)

    def replay(self, sells):
        self.cfg['investment_interval'] = 1 if review == 'full' else 1000
        self._investment_dirty_goods = {next(iter(self.goods))} if review == 'targeted' else set()
        native, native_sells = copy.deepcopy((self, sells))
        original(self, sells)

        def forbidden(*args, **kwargs):
            pytest.fail('Native batch вызвал Python decision callback')

        monkeypatch.setattr(serial_step, 'evaluate_project_candidate', forbidden)
        for name in ('project_requests', 'project_candidate', 'profile',
                     'upstream_expansion_candidate', 'register_investment_project'):
            monkeypatch.setattr(native, name, forbidden)
        material = native.project_material_requests

        def material_requests(*args):
            assert not inside_native
            counts['material'] += 1
            return material(*args)

        monkeypatch.setattr(native, 'project_material_requests', material_requests)
        boundary.run_native_project_requests(native, native_sells)
        assert reference.accounts == native.accounts
        assert reference.enterprises == native.enterprises
        assert reference.meta == native.meta
        assert reference.report == native.report
        assert sells == native_sells
        assert reference.rng.getstate() == native.rng.getstate()
        assert reference.report['investment_review']['mode'] == review
        self.validate()
        native.validate()

    monkeypatch.setattr(Economy, 'project_requests', replay)
    reference.step()
    assert counts == dict(pack=1, native=1, apply=1, material=1)


def test_project_batch_queue_resume_duplicate_invalid_and_waiting(monkeypatch):
    reference = engine()
    original = Economy.project_requests

    def replay(self, sells):
        self.cfg['investment_interval'] = 1000
        self._investment_dirty_goods = set()
        investors = [a for a in self.accounts.values()
                     if a['kind'] in ('private', 'merchant', 'treasury', 'municipal')
                     and not a.get('project')][:2]
        assert len(investors) == 2
        ready_good = next(g for g, recipe in self.goods.items() if not recipe['inputs'])
        waiting_good = next(g for g, recipe in self.goods.items() if recipe['inputs'])

        def project(good, capacity):
            return dict(good=good, capacity=capacity, location=self.location('p'),
                materials={}, working_cash=self.cfg['startup_cash'], expected_price=self.goods[good]['price'],
                expected_profit=1, expected_investment=1, expected_unit_cost=1,
                expansion=False, created=self.meta['month'], chain_requirements={},
                project_id='queue-' + good, parent_project_id=None,
                derived_demand=None, dependency_expires=None)

        ready = dict(created=self.meta['month'], investor=investors[0]['id'],
                     project=project(ready_good, self.cfg['minimum_capacity']))
        waiting = dict(created=self.meta['month'], investor=investors[1]['id'],
                       project=project(waiting_good, 1e18))
        self.meta['investment_queue'] = [dict(investor='missing'), ready,
                                          copy.deepcopy(ready), waiting]
        native, native_sells = copy.deepcopy((self, sells))
        original(self, sells)
        boundary.run_native_project_requests(native, native_sells)
        assert self.accounts == native.accounts
        assert self.meta == native.meta
        assert self.report == native.report
        assert self.rng.getstate() == native.rng.getstate()
        assert self.meta['investment_queue'] == [waiting]
        assert sum(row.get('outcome') == 'resumed'
                   for row in self.report['investment_decisions']) == 1
        self.validate()
        native.validate()

    monkeypatch.setattr(Economy, 'project_requests', replay)
    reference.step()


@pytest.mark.parametrize('resource_capacity, upstream_started', [(6, False), (20, True)])
def test_project_batch_upstream_reservations_cache_and_diagnostics(resource_capacity, upstream_started):
    reference = engine()
    reference.meta.update(month=1, report_detail='deep')
    reference.cfg.update(investment_interval=1, extraction_per_tile=resource_capacity)
    reference.reset_report()
    # Поток существующего зерна позволяет мукомольный проект, но физического
    # предложения нет. Единственный upstream имеет либо нулевой, либо свободный
    # ресурсный предел; несколько инвесторов повторяют один кэшированный поиск.
    for firm in reference.enterprises.values():
        if firm['good'] != 'grain':
            firm['status'] = 'closed'
    reference.order('merchant:s', 'flour', 50, 'buy', 10, 'Тестовый спрос')
    reference._investment_dirty_goods = set()
    sells = {}
    native, native_sells = copy.deepcopy((reference, sells))
    reference.project_requests(sells)
    boundary.run_native_project_requests(native, native_sells)
    assert reference.accounts == native.accounts
    assert reference.meta == native.meta
    assert reference.report == native.report
    assert reference.rng.getstate() == native.rng.getstate()
    assert reference._upstream_candidate_diagnostics == native._upstream_candidate_diagnostics
    assert reference.meta['investment_queue']
    decisions = reference.report['investment_decisions']
    upstream = [row for row in decisions if row.get('dependency_for') == 'flour']
    assert bool(upstream) is upstream_started
    if upstream_started:
        assert len(upstream) == 1
    else:
        assert any(row['event_type'] == 'investment.upstream_unavailable'
                   for row in reference.report['events'])
    reference.validate()
    native.validate()


def test_canonical_exact_comparison_keeps_ulp_and_large_integer_differences():
    import math
    from work.project_batch_compare import exact

    assert exact({'value': 1}, {'value': 1.0}) is None
    assert exact(1.0, math.nextafter(1.0, 2.0)) is not None
    assert exact(2**53+1, float(2**53+1)) is not None
    assert exact(True, 1) is not None
