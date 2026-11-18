# cython: language_level=3
"""Расчёт ещё не списанного количества по исполненной заявке."""

from builtins import max as _PY_MAX


def calculate_consumption(object ordered, object remaining, object consumed):
    cdef object filled = ordered-remaining
    return filled, _PY_MAX(0, filled-consumed)
