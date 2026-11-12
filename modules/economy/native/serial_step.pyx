# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Техническая проверка сборки; игровая логика сюда ещё не перенесена."""


def weighted_sum(double[::1] values, double[::1] weights):
    """Проверить один вызов с двумя непрерывными массивами float64."""
    cdef Py_ssize_t i, size = values.shape[0]
    cdef double total = 0.0
    if size != weights.shape[0]:
        raise ValueError('Длины массивов должны совпадать')
    for i in range(size):
        total += values[i]*weights[i]
    return total
