"""Явная загрузка экспериментального Cython-расширения."""


def load_serial_step():
    """Вернуть модуль или объяснить, как собрать отсутствующее расширение."""
    try:
        from . import serial_step
    except ModuleNotFoundError as exc:
        if exc.name != f'{__name__}.serial_step':
            raise
        raise RuntimeError(
            'Cython-расширение serial_step не собрано. Выполните: '
            'python -m pip install -r requirements-native.txt && '
            'cd modules/economy/native && python setup.py build_ext --inplace'
        ) from exc
    return serial_step
