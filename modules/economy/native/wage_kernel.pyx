# cython: language_level=3
"""Числовые проверки и расчёты оплаты труда публичной стройки."""


def supplies_ready(object required, object held, object eps):
    for good, quantity in required.items():
        if not held.get(good, 0)+eps >= quantity:
            return False
    return True


def calculate_workers(object maximum, object remaining, object labor, object cash, object wage):
    return min(maximum, remaining, labor, cash/wage)


def calculate_wages(object workers, object wage, object multiplier):
    return workers*wage*multiplier


def calculate_lodging(object workers, object allowance):
    return workers*allowance
