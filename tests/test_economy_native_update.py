import copy
import json
from pathlib import Path

import pytest

from modules.economy import Economy

pytest.importorskip('modules.economy.native.serial_step')
from modules.economy.native.update_prices_boundary import run_native_preparation, run_update_prices


def engine():
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    cfg = json.loads(Path('tests/fixtures/economy.json').read_text(encoding='utf-8'))
    result = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    result.meta['report_detail'] = 'none'
    result.seed = 17
    result.rng.seed(17)
    result.bootstrap()
    return result


def assert_same_state(reference, native):
    assert reference.accounts == native.accounts
    assert reference.enterprises == native.enterprises
    assert reference.meta == native.meta
    assert reference.report == native.report
    assert reference.rng.getstate() == native.rng.getstate()
    reference.validate()
    native.validate()


def test_update_prices_native_journal_and_full_step_match_v3():
    reference = engine()
    # Первый ход создаёт реальные history/public_orders/price_feedback для ветвей цены.
    reference.step()
    native = copy.deepcopy(reference)

    reference.update_prices()
    journal = run_update_prices(native)
    assert journal
    assert_same_state(reference, native)


def test_recovery_quotes_native_journal_matches_v3_and_preserves_recipe_order():
    reference = engine()
    reference.step()
    native = copy.deepcopy(reference)

    reference.update_prices()
    reference.prepare_recovery_quotes()
    journal = run_native_preparation(native)

    assert journal
    assert reference._recovery_quotes == native._recovery_quotes
    assert_same_state(reference, native)
    data = native.native_preparation['data']
    goods = list(native.goods)
    for product, recipe in native.goods.items():
        index = goods.index(product)
        start, stop = data['recovery_input_offset'][index:index+2]
        assert list(data['recovery_input_good'][start:stop]) == [
            goods.index(good) for good in recipe['inputs']]


def test_two_complete_native_steps_match_v3():
    reference, native = engine(), engine()
    for _ in range(2):
        reference.step()
        native.step(native_mode='cython')
        assert_same_state(reference, native)
