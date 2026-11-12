import numpy as np
import pytest

from modules.economy import Economy


def test_native_weighted_sum_smoke():
    native = pytest.importorskip('modules.economy.native.serial_step')
    values = np.array([1.5, 2.0, -0.25], dtype=np.float64)
    weights = np.array([2.0, 0.5, 4.0], dtype=np.float64)
    assert native.weighted_sum(values, weights) == 3.0
    with pytest.raises(ValueError, match='Длины массивов'):
        native.weighted_sum(values, weights[:2])


def test_requested_native_mode_never_falls_back_to_v3(monkeypatch):
    import modules.economy.native as native

    def missing():
        raise RuntimeError('Cython-расширение serial_step не собрано')

    monkeypatch.setattr(native, 'load_serial_step', missing)
    engine = object.__new__(Economy)
    with pytest.raises(RuntimeError, match='не собрано'):
        engine.step(native_mode='cython')


def test_native_skeleton_stops_before_state_mutation(monkeypatch):
    import modules.economy.native as native

    monkeypatch.setattr(native, 'load_serial_step', lambda: object())
    engine = object.__new__(Economy)
    with pytest.raises(NotImplementedError, match='ещё не согласован'):
        engine.step(native_mode='cython')
    assert not engine.__dict__
