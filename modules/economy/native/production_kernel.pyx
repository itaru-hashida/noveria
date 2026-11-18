# cython: language_level=3
"""Расчёт выпуска по ограничениям одного предприятия."""

def calculate_output(dict constraints, object planned, object eps):
    """Вернуть выпуск, причину ограничения и признак нехватки сырья."""
    cdef object reason = min(constraints, key=constraints.get)
    cdef object quantity = max(0, constraints[reason])
    cdef object key, value
    for key, value in constraints.items():
        if key.startswith('Сырьё') and value+eps < planned:
            return quantity, reason, True
    return quantity, reason, False
