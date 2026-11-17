"""record_market: числовая агрегация книги заявок и сделок в Cython.

Блок 3 переноса: числовой цикл ``V3.record_market`` (requested, limit_value,
unmet_solvent, reserved_unmet по заявкам; volume, trade_gold, freight по
сделкам; отметка ``o['unmet_solvent']`` на живой заявке) переехал в
``report_history_kernel``.

Осталось в Python и не переносилось: сборка ``meta['market_history']``,
поиск повторного дефицита и ``meta['price_feedback']`` (нужен
``copy.deepcopy`` заявки), а также снимок заявки для public_orders — он
приходит хуком.

Тест держит значения, типы (int-нули прежнего defaultdict), порядок ключей и
отметок, мутации заявок, отсутствие RNG и fallback без собранного ``.pyd``.
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy.native import report_history_boundary as boundary  # noqa: E402

EPS = 1e-7
FIELDS = boundary.MARKET_FIELDS


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def snapshot(order):
    return dict(id=order['id'], good=order['good'], remaining=order['remaining'])


def buy(order_id, good='bread', state='s', province='p', tile='t', price=2.0,
        quantity=10.0, remaining=4.0, reserved=6.0, status='open', **extra):
    row = dict(id=order_id, side='buy', participant='a', good=good,
               state_id=state, province_id=province, tile_id=tile, price=price,
               quantity=quantity, remaining=remaining, reserved=reserved,
               status=status)
    row.update(extra)
    return row


def trade(seller='f', good='bread', buyer_state='s', buyer_province='p',
          quantity=3.0, gold=6.0, freight=1.0):
    return dict(seller=seller, good=good, buyer_state=buyer_state,
                buyer_province=buyer_province, buyer_province_id=buyer_province,
                quantity=quantity, gold=gold, freight=freight)


def run_native(orders, trades):
    return boundary.record_market_rows(orders, trades, FIELDS, EPS, snapshot)


def run_python(orders, trades):
    return boundary.record_market_rows_python(orders, trades, FIELDS, EPS, snapshot)


def test_native_matches_python_reference():
    orders = [buy('o1'), buy('o2', good='fish', province='q', price=3.0),
              buy('o3', remaining=0.0, reserved=0.0)]
    trades = [trade(), trade(good='fish', buyer_province='q', quantity=2.0)]
    native = run_native(copy.deepcopy(orders), copy.deepcopy(trades))
    reference = run_python(copy.deepcopy(orders), copy.deepcopy(trades))
    assert native == reference


def test_rows_start_with_int_zero_like_the_previous_defaultdict():
    """Накопители заводятся int-нулём; тип меняется только арифметикой."""
    rows, _ = run_native([], [])
    assert rows == {}
    rows, _ = run_native([buy('o1', quantity=10, price=2, remaining=4, reserved=6)], [])
    row = rows[('s', 'p', 'bread')]
    assert type(row['volume']) is int
    assert type(row['trade_gold']) is int
    assert type(row['freight']) is int
    assert type(row['requested']) is int
    assert type(row['reserved_unmet']) is int


def test_empty_trade_row_keeps_int_zeros():
    rows, _ = run_native([], [])
    assert rows == {}


def test_row_field_order_matches_the_previous_defaultdict():
    rows, _ = run_native([buy('o1')], [trade()])
    row = rows[('s', 'p', 'bread')]
    assert list(row) == list(FIELDS)


def test_key_order_follows_first_appearance():
    orders = [buy('o1', good='b'), buy('o2', good='a')]
    rows, _ = run_native(orders, [trade(good='c')])
    assert list(rows) == [('s', 'p', 'b'), ('s', 'p', 'a'), ('s', 'p', 'c')]


def test_cancelled_order_is_skipped_unless_superseded():
    plain = buy('o1', status='cancelled')
    superseded = buy('o2', status='cancelled', superseded_by_top_up=True,
                     quantity=10.0, remaining=4.0)
    rows, public = run_native([plain, superseded], [])
    assert ('s', 'p', 'bread') in rows
    # Отменённая без superseded не попала в строки; её unmet_solvent не тронут.
    assert 'unmet_solvent' not in plain
    assert 'unmet_solvent' in superseded
    # remaining у superseded равен нулю, поэтому в public_orders её нет.
    assert public == []


def test_cancelled_superseded_order_uses_spent_quantity():
    superseded = buy('o1', status='cancelled', superseded_by_top_up=True,
                     quantity=10.0, remaining=4.0)
    rows, _ = run_native([superseded], [])
    assert rows[('s', 'p', 'bread')]['requested'] == 6.0


def test_superseded_order_uses_spent_quantity_and_zero_remaining():
    order = buy('o1', quantity=10.0, remaining=4.0, superseded_by_top_up=True)
    rows, public = run_native([order], [])
    row = rows[('s', 'p', 'bread')]
    assert row['requested'] == 6.0
    # remaining=0 у superseded: в public_orders такая заявка не попадает.
    assert public == []


def test_sell_orders_are_not_market_rows():
    order = buy('o1')
    order['side'] = 'sell'
    rows, public = run_native([order], [])
    assert rows == {}
    assert public == []


def test_limit_value_sums_price_times_quantity():
    rows, _ = run_native([buy('o1', price=2.5, quantity=4.0),
                          buy('o2', price=1.5, quantity=2.0)], [])
    assert rows[('s', 'p', 'bread')]['limit_value'] == 2.5*4.0+1.5*2.0


def test_unmet_solvent_uses_min_of_remaining_and_reserved_over_price():
    rows, _ = run_native([buy('o1', price=2.0, remaining=4.0, reserved=6.0)], [])
    assert rows[('s', 'p', 'bread')]['unmet_solvent'] == min(4.0, 6.0/2.0)


def test_live_order_gets_unmet_solvent_mark():
    order = buy('o1', price=2.0, remaining=4.0, reserved=6.0)
    run_native([order], [])
    assert order['unmet_solvent'] == 3.0


def test_reserved_unmet_sums_reserved_without_clamping():
    rows, _ = run_native([buy('o1', reserved=6.0), buy('o2', reserved=0.5)], [])
    assert rows[('s', 'p', 'bread')]['reserved_unmet'] == 6.5


def test_trade_sums_volume_gold_and_freight():
    rows, _ = run_native([], [trade(quantity=3.0, gold=6.0, freight=1.0),
                              trade(quantity=2.0, gold=4.0, freight=0.5)])
    row = rows[('s', 'p', 'bread')]
    assert row['volume'] == 5.0
    assert row['trade_gold'] == 10.0
    assert row['freight'] == 1.5


def test_orders_and_trades_share_the_same_row():
    rows, _ = run_native([buy('o1', price=2.0, quantity=4.0)],
                         [trade(quantity=2.0)])
    row = rows[('s', 'p', 'bread')]
    assert row['requested'] == 4.0
    assert row['volume'] == 2.0


def test_public_contains_snapshot_for_live_remaining_only():
    orders = [buy('o1', remaining=3.0), buy('o2', remaining=0.0),
              buy('o3', remaining=1.0, status='filled')]
    _, public = run_native(orders, [])
    assert [row['id'] for row in public] == ['o1', 'o3']


def test_empty_book_gives_empty_result():
    rows, public = run_native([], [])
    assert rows == {}
    assert public == []


def test_rng_is_not_touched():
    import random
    rng = random.Random(17)
    before = rng.getstate()
    run_native([buy('o1')], [trade()])
    assert rng.getstate() == before


def test_input_orders_are_only_marked_not_replaced():
    """Мутация — только поле unmet_solvent, прочие поля заявки не меняются."""
    order = buy('o1')
    kept = {key: value for key, value in order.items()}
    run_native([order], [])
    for key, value in kept.items():
        assert order[key] == value, key
    assert set(order) == set(kept) | {'unmet_solvent'}


# --- Реальный движок -------------------------------------------------------

@pytest.fixture(scope='module')
def world():
    tmp = workdir('test_record_market')
    with tmp as directory:
        db, service = sr.build_world(17, directory, report_detail='standard')
        try:
            engine = service.economy_state().engine
            for month in range(3):
                engine.step(native_mode='off')
            return engine
        finally:
            db.close()


def test_native_matches_python_on_a_real_month(world):
    orders = copy.deepcopy(world.report['orders'])
    trades = copy.deepcopy(world.report['trades'])
    native_orders, native_public = run_native(copy.deepcopy(orders), trades)
    python_orders, python_public = run_python(copy.deepcopy(orders), trades)
    assert native_orders == python_orders
    assert native_public == python_public
    assert digest(native_public) == digest(python_public)


def test_native_matches_python_on_live_marks(world):
    orders = copy.deepcopy(world.report['orders'])
    trades = copy.deepcopy(world.report['trades'])
    left = copy.deepcopy(orders)
    right = copy.deepcopy(orders)
    boundary.record_market_rows(left, trades, FIELDS, EPS, snapshot)
    boundary.record_market_rows_python(right, trades, FIELDS, EPS, snapshot)
    assert left == right


# --- Fallback без .pyd -----------------------------------------------------

def test_record_market_falls_back_to_python(monkeypatch):
    assert boundary.NATIVE_KERNEL is True, 'ожидалось собранное ядро'
    orders = [buy('o1')]
    native = run_native(copy.deepcopy(orders), [trade()])
    monkeypatch.setattr(boundary, 'record_market_rows',
                        boundary.record_market_rows_python)
    monkeypatch.setattr(boundary, 'NATIVE_KERNEL', False)
    assert run_native(copy.deepcopy(orders), [trade()]) == native
    monkeypatch.undo()
    assert boundary.NATIVE_KERNEL is True


def test_boundary_exposes_both_implementations():
    for name in ('record_market_rows', 'record_market_rows_python',
                 'MARKET_FIELDS'):
        assert getattr(boundary, name) is not None, name
