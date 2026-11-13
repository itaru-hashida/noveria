"""Минимальные тесты фасада EconomyCore на fake economy.

Фасад не содержит экономической логики: проверяем только делегирование,
property и безопасные fallback'и snapshot/drain_events.
"""
from modules.economy.core_api import EconomyCore


class FakeEconomy:
    """Минимальная заглушка Economy для проверки делегирования."""

    def __init__(self, month=0, report=None, events=None):
        self.meta = {'month': month}
        self.report = {'orders': []} if report is None else report
        self.calls = []
        if events is not None:
            self.events = events
        self.result = 'step-result'

    def step(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_step_delegates_kwargs():
    economy = FakeEconomy()
    core = EconomyCore(economy)
    result = core.step(enterprise_planner=None, planning_mode='lagged_batch',
                       investment_mode='v3', investment_workers=3, native_mode='off')
    assert result == 'step-result'
    assert economy.calls == [{
        'enterprise_planner': None,
        'planning_mode': 'lagged_batch',
        'investment_mode': 'v3',
        'investment_workers': 3,
        'native_mode': 'off',
    }]


def test_economy_returns_original_object():
    economy = FakeEconomy()
    assert EconomyCore(economy).economy is economy


def test_snapshot_returns_month_and_report():
    report = {'orders': [1, 2, 3], 'rules_version': 3}
    core = EconomyCore(FakeEconomy(month=7, report=report))
    snapshot = core.snapshot()
    assert snapshot['month'] == 7
    assert snapshot['report'] == report
    assert snapshot['report'] is not report, 'snapshot не должен отдавать сам report'


def test_snapshot_fallback_without_meta_and_report():
    economy = FakeEconomy()
    del economy.meta
    del economy.report
    snapshot = EconomyCore(economy).snapshot()
    assert snapshot == {'month': None, 'report': {}}


def test_drain_events_empty_without_queue():
    assert EconomyCore(FakeEconomy()).drain_events() == []


def test_drain_events_returns_and_clears_events():
    economy = FakeEconomy(events=[{'kind': 'month'}, {'kind': 'trade'}])
    core = EconomyCore(economy)
    assert core.drain_events() == [{'kind': 'month'}, {'kind': 'trade'}]
    assert economy.events == []
    assert core.drain_events() == []
