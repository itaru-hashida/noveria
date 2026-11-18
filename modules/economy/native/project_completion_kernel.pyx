# cython: language_level=3
"""Числовые проверки готовности проекта и пересчёт инвестиций."""


def materials_ready(object bill, object materials, object eps):
    for good, quantity in bill.items():
        if materials.get(good, 0)+eps < quantity:
            return False
    return True


def cash_ready(object free, object protected, object needed, object eps):
    return not (free+protected+eps < needed)


def capacity_values(object previous, object capacity, object profit,
                    object construction, object working):
    return round(previous, 12), profit*capacity/previous, construction*capacity/previous+working
