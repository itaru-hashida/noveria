# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Enterprise reset kernel: месячный numeric-сброс полей предприятий.

Ядро буквально повторяет прежний цикл V3: ``e.pop('production_observation')``
и ``e.update(...)`` с тем же порядком ключей, теми же значениями и теми же
типами (int-нули для целочисленных полей, float-нули и False для остальных,
пустой dict для износа). Работа идёт по уже готовому списку ``existing``:
порядок обхода, RNG, report и хуки остаются за Python, ядро не заводит
собственных структур и не трогает поля вне списка сброса.
"""


def run_reset(object rows):
    """Пройти список предприятий и сбросить месячные numeric-поля."""
    cdef object e
    cdef Py_ssize_t index, count
    cdef list existing = rows

    count = len(existing)
    for index in range(count):
        e = existing[index]
        e.pop('production_observation', None)
        # Порядок ключей — прежний: dict сохраняет его при update, и новые
        # ключи встают в том же порядке, что и у Python-ветки.
        e.update(revenue=0, tax=0, expenses=0, profit=0, output=0, workers=0,
                 wages=0, cost_of_sales=0, production_cost=0, input_limited=False,
                 fixed_cost_due=0, fixed_cost_paid=0, dividends=0.0,
                 working_reserve=0.0, project_reserve=0.0, equipment_wear={})
    return None
