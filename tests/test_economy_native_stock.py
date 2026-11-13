"""Focused-тесты stock-ядра: точное совпадение с семантикой V3.

Ядро работает с object-арифметикой намеренно: остаток V3 бывает и int, и
float, и тип результата виден в отчёте (встроенный sum в Python 3.12
компенсирует погрешность только при float-старте). Поэтому проверяется не
только значение, но и тип остатка/стоимости и порядок операций.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.economy import Economy  # noqa: E402
from modules.economy import support  # noqa: E402
from modules.economy.native import stock_boundary  # noqa: E402

EPS = support.EPS


class Fake:
    """Минимальный носитель состояния: ядру нужны только accounts и индекс."""

    def __init__(self, stock=None, values=None, totals=None):
        self.accounts = {'acc': dict(stock=dict(stock or {}), values=dict(values or {}))}
        if totals is not None:
            self._physical_stock_totals = totals

    def state(self):
        return self.accounts['acc']


def apply_both(stock, values, add=None, use=None, totals=None):
    """Прогнать add/use на эталоне и на ядре, вернуть обе пары состояний."""
    results = []
    for add_fn, use_fn in ((stock_boundary.add_stock_python, stock_boundary.use_stock_python),
                           (stock_boundary.add_stock, stock_boundary.use_stock)):
        owner = Fake(stock, values)
        if totals is not None:
            owner._physical_stock_totals = {key: dict(value) for key, value in totals.items()}
        returned = None
        error = None
        try:
            if add is not None:
                add_fn(owner, 'acc', add[0], add[1], add[2])
            if use is not None:
                returned = use_fn(owner, 'acc', use[0], use[1])
        except ValueError as exc:
            error = str(exc)
        results.append(dict(state=owner.state(), totals=getattr(owner, '_physical_stock_totals', None),
                            returned=returned, error=error))
    return results


def assert_same(left, right):
    assert left['error'] == right['error']
    assert left['state'] == right['state']
    # Тип каждого остатка и стоимости обязан совпадать, а не только значение:
    # int + int -> int, float + int -> float.
    for field in ('stock', 'values'):
        for good, value in left['state'][field].items():
            assert type(right['state'][field][good]) is type(value), (field, good)
    if left['totals'] is not None:
        assert left['totals'] == right['totals']
    if left['returned'] is not None or right['returned'] is not None:
        assert right['returned'] == left['returned']
        assert type(right['returned']) is type(left['returned'])


STOCK_CASES = [
    # (stock, values, add, use)  — add=(good, quantity, value), use=(good, quantity)
    ({}, {}, ('wood', 5, 10), ('wood', 2)),
    ({'wood': 0}, {'wood': 0}, ('wood', 5, 10), ('wood', 2)),
    ({'wood': 10}, {'wood': 100}, ('wood', 5, 10), ('wood', 3)),
    ({'wood': 10.0}, {'wood': 100.0}, ('wood', 0.5, 1.0), ('wood', 0.5)),
    ({'wood': 3}, {'wood': 7}, ('wood', 0, 0), ('wood', 3)),
    ({'wood': 3}, {'wood': 7}, ('wood', 1.5, 2.5), ('wood', 1.5)),
    ({'wood': 2.5}, {'wood': 9.0}, ('wood', 1, 3), ('wood', 0.25)),
    # Списание всей доступной величины: min(quantity, available) возвращает
    # available, поэтому тип остатка диктует тип результата.
    ({'wood': 4}, {'wood': 8}, None, ('wood', 4.0)),
    ({'wood': 4.0}, {'wood': 8.0}, None, ('wood', 4)),
    (None, None, None, None),
]


@pytest.mark.parametrize('stock,values,add,use', STOCK_CASES)
def test_native_matches_python_reference(stock, values, add, use):
    reference, native = apply_both(stock or {}, values or {}, add, use)
    assert_same(reference, native)


def test_empty_slot_types_follow_v3():
    # add_stock на пустом складе: 0 (int, из get-дефолта) + 5 -> int.
    reference, native = apply_both({}, {}, ('wood', 5, 10), None)
    assert native['state']['stock']['wood'] == 5
    assert type(native['state']['stock']['wood']) is int
    assert type(native['state']['values']['wood']) is int
    # float-количество сохраняет float.
    reference, native = apply_both({}, {}, ('wood', 5.0, 10.0), None)
    assert type(native['state']['stock']['wood']) is float


def test_int_stock_stays_int_across_repeated_operations():
    owner = Fake({'wood': 1}, {'wood': 2})
    for _ in range(5):
        stock_boundary.add_stock(owner, 'acc', 'wood', 1, 2)
        stock_boundary.use_stock(owner, 'acc', 'wood', 1)
    assert owner.state()['stock']['wood'] == 1
    assert type(owner.state()['stock']['wood']) is int
    # Стоимость становится float: use_stock считает values*quantity/available,
    # а это истинное деление. Так же ведёт себя и Python-эталон.
    assert owner.state()['values']['wood'] == 2.0
    assert type(owner.state()['values']['wood']) is float


def test_use_stock_multiplies_then_divides():
    # Порядок важен для последних битов: values*q/available, а не values/available*q.
    stock, values, quantity = 3.0, 10.0, 2.0
    owner = Fake({'wood': stock}, {'wood': values})
    value = stock_boundary.use_stock(owner, 'acc', 'wood', quantity)
    # Значение обязано совпасть побитово с буквальной формулой V3.
    assert value == values*quantity/stock


def test_use_stock_rejects_overdraw_and_negative():
    owner = Fake({'wood': 2}, {'wood': 4})
    with pytest.raises(ValueError, match='Двойное расходование товара'):
        stock_boundary.use_stock(owner, 'acc', 'wood', 3)
    with pytest.raises(ValueError, match='Двойное расходование товара'):
        stock_boundary.use_stock(owner, 'acc', 'wood', -1)
    # Допуск EPS: чуть больше остатка в пределах EPS допустимо.
    owner = Fake({'wood': 2.0}, {'wood': 4.0})
    value = stock_boundary.use_stock(owner, 'acc', 'wood', 2.0 + EPS/2)
    assert value == 4.0
    assert owner.state()['stock']['wood'] == 0


def test_physical_stock_totals_updated_like_v3():
    totals = {'wood': dict(quantity=10.0, count=2)}
    reference, native = apply_both({'wood': 4}, {'wood': 8}, ('wood', 2, 4), ('wood', 1),
                                   totals=totals)
    assert_same(reference, native)
    assert native['totals']['wood']['quantity'] == 10.0+2-1
    # count реагирует на переход через EPS в обе стороны.
    totals = {'wood': dict(quantity=0.0, count=1)}
    owner = Fake({'wood': 1}, {'wood': 2})
    owner._physical_stock_totals = {key: dict(value) for key, value in totals.items()}
    stock_boundary.use_stock(owner, 'acc', 'wood', 1)
    assert owner._physical_stock_totals['wood']['count'] == 0


def test_totals_absent_is_not_an_error():
    # Индекс строится лениво: до первого physical_stock_summary его нет,
    # и операция обязана просто не трогать ничего.
    owner = Fake({'wood': 1}, {'wood': 2})
    assert not hasattr(owner, '_physical_stock_totals')
    stock_boundary.add_stock(owner, 'acc', 'wood', 1, 2)
    assert not hasattr(owner, '_physical_stock_totals')
    assert owner.state()['stock']['wood'] == 2


def test_economy_stock_methods_delegate_to_kernel():
    """Методы Economy/EconomyServices идут через границу, а не своим кодом."""
    assert support.EconomyServices.add_stock is not None
    engine = object.__new__(Economy)
    engine.accounts = {'acc': dict(stock={}, values={})}
    engine.__dict__.pop('_physical_stock_totals', None)
    engine.add_stock('acc', 'wood', 3, 6)
    assert engine.accounts['acc']['stock']['wood'] == 3
    value = engine.use_stock('acc', 'wood', 1)
    assert value == 2
    assert engine.accounts['acc']['stock']['wood'] == 2


def test_boundary_exposes_native_when_built():
    built = stock_boundary.load_stock_kernel() is not None
    assert stock_boundary.NATIVE_KERNEL is built
    if built:
        assert stock_boundary.add_stock.__module__.endswith('stock_kernel')
    else:
        assert stock_boundary.add_stock is stock_boundary.add_stock_python


def test_native_entry_points_are_the_compiled_ones():
    """Граница обязана привязать именно скомпилированные функции.

    Загрузчик однажды использовал __name__ вместо __package__ и молча уходил
    в Python-ветку: значения совпадали, поэтому обычные тесты этого не видели.
    Проверяем имена функций, а не только результат.
    """
    kernel = stock_boundary.load_stock_kernel()
    if kernel is None:
        pytest.skip('Cython-расширение stock_kernel не собрано')
    assert stock_boundary.add_stock is kernel.add_stock
    assert stock_boundary.use_stock is kernel.use_stock
    assert stock_boundary.add_stock is not stock_boundary.add_stock_python
    assert stock_boundary.use_stock is not stock_boundary.use_stock_python


def test_load_returns_none_when_extension_absent(monkeypatch):
    """Отсутствие собранного ядра обязано давать None, а не падать.

    Пакет modules.economy.native существует, поэтому импорт подмодуля без
    расширения бросает ImportError, а не ModuleNotFoundError. Ошибка здесь
    ломала бы весь импорт economy на машине без собранного Cython.
    """
    import importlib

    name = 'modules.economy.native.stock_kernel'
    real_import = importlib.import_module

    def fake_import(module_name, package=None):
        if module_name == name or module_name.endswith('stock_kernel'):
            raise ImportError("cannot import name 'stock_kernel'")
        return real_import(module_name, package)

    monkeypatch.setattr(importlib, 'import_module', fake_import)
    assert stock_boundary.load_stock_kernel() is None


def test_native_kernel_int_and_float_parity_on_real_engine():
    """На реальном движке: одинаковые значения и типы для int-ветки V3."""
    cfg = json.loads((ROOT/'tests/fixtures/economy.json').read_text(encoding='utf-8'))
    entities = [dict(id='s', kind='State', name='S', properties={'capital_id': 'c'}),
        dict(id='p', kind='Province', name='P', properties={'capital_id': 'c'}),
        dict(id='c', kind='City', name='C', properties={'province_id': 'p', 'tile_id': 't'}),
        dict(id='h', kind='SocialGroup', name='H', properties={'owner_id': 'p', 'population': 100000})]
    links = [dict(source='s', target=i, kind='owns') for i in ('p', 'c')]
    tile = dict(id='t', x=0, y=0, passable=True, cost=1, road=True, province_id='p',
        resources=['зерно', 'лес', 'железо', 'рыба'], terrain='plain')
    engine = Economy(dict(entities=entities, links=links), [tile],
        meta=dict(id='game', version=1, month=0, config=cfg))
    engine.meta['report_detail'] = 'none'
    engine.seed = 17
    engine.rng.seed(17)
    engine.bootstrap()
    account = engine.accounts['merchant:s']
    good = next(iter(engine.goods))
    # Целочисленная ветка: остаток приходит из bootstrap целым/дробным.
    account['stock'][good] = 10
    account['values'][good] = 20
    engine.add_stock(account['id'], good, 5, 10)
    assert account['stock'][good] == 15
    assert type(account['stock'][good]) is int
    value = engine.use_stock(account['id'], good, 5)
    assert account['stock'][good] == 10
    assert type(account['stock'][good]) is int
    assert value == 10
