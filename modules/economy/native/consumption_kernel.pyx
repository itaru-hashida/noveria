# cython: language_level=3
"""Расчёт потребления по потребности и доступному остатку."""


def calculate_consumption(object needed, object available):
    return min(needed, available)
