import pytest

from core.economy_convergence import assess_convergence
from core.lab_store import LabStore


@pytest.mark.parametrize('scenario,expected', [
    ('stationary', 'Активное стационарное состояние'),
    ('cycle', 'Устойчивый повторяющийся цикл'),
    ('stopped', 'Экономическая депрессия'),
    ('drift', 'Продолжающаяся адаптация'),
    ('stuck', 'Возможный алгоритмический тупик'),
    ('price_drift', 'Продолжающаяся адаптация'),
])
def test_convergence_distinguishes_activity_cycles_and_drift(tmp_path, scenario, expected):
    with LabStore(tmp_path/'analysis.sqlite', create=True) as store:
        for month in range(181):
            activity = 0 if scenario in ('stopped', 'stuck') else 10+(2*(month % 7) if scenario == 'cycle' else 0)
            shift = month if scenario == 'drift' else 5*(month % 7) if scenario == 'cycle' else 0
            price = 1+month*.01 if scenario == 'price_drift' else 1
            data = dict(overview=[dict(output=activity, consumed=activity, wages=activity, turnover=activity*price,
                household_gold=1000-shift, enterprise_gold=1000+shift, treasury_gold=0,
                private_gold=0, merchant_gold=0, stock=10, pending_projects=0)],
                accounts=[dict(id='h', balance=1000-shift), dict(id='e', balance=1000+shift)],
                production=[dict(good='bread', output=activity, consumed=activity, revenue=activity*price, sold=activity, stock=10)],
                enterprises=[dict(id='e', reachable_demand=10 if scenario == 'stuck' else 0,
                    expected_margin=1, output=activity, production_limit='План')])
            store.append_month(month, data, {})
        result = assess_convergence(store)
        assert result['state'] == expected
        if scenario == 'cycle':
            assert result['period'] == 7
        assert result['windows'] == [[1, 60], [61, 120], [121, 180]]


def test_short_history_is_not_equilibrium(tmp_path):
    with LabStore(tmp_path/'analysis.sqlite', create=True) as store:
        assert assess_convergence(store)['state'] == 'Недостаточно истории'
