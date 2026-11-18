# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Enterprise settlement kernel: числовой цикл оброка и прибыли.

Ядро буквально повторяет прежний цикл ``Economy.settle`` по предприятиям:
``due`` получает хук, ``paid`` считает ``min`` тем же порядком, затем получает
подготовленный tuple входов и возвращает чистый tuple числового результата.
Живые dict предприятий ядро не пишет: ordered financial fields применяет
Python-хук до lifecycle-ветки, а ``unviable_months`` — только для active
предприятия после проверки closed. Подготовка active-counter отдельно
сохраняет прежний short-circuit до чтения ``available_from`` и
``unviable_months``.

Перевод оброка, возврат капитала и закрытие предприятия — вызовы переданных
хуков: деньги двигает прежний ``Economy.transfer`` через ``cash_store``,
отчёты, заявки и события остаются Python.

Границы среза:

  * список предприятий готовит ``v3.settle``: порядок обхода, RNG и второй
    цикл (carrier/treasury distribute) ядро не трогает;
  * расчёт оброка — хук ``overhead``: денежная политика остаётся у движка;
  * закрытие и возврат капитала — хуки: заявки, склад и lifecycle-отчёт
    остаются за движком;
  * второго ledger нет: ядро пишет только числовые поля существующих dict-ов
    предприятий.

Арифметика намеренно ``object``: месячные накопления (``revenue``, ``tax``,
``cost_of_sales``) бывают и int, и float. Cython порождает те же
``PyNumber_*``, что и интерпретатор, поэтому значения и типы не приводятся
и не теряют последний бит. Порядок сложений в ``expenses`` и ``profit``
сохранён буквально. Подготовка читает owner state до расчёта, поэтому
вычисление не зависит от промежуточных owner writes.
"""


def calculate_financial_results(object prepared, object eps):
    """Pure numeric result: no enterprise/engine/container mutation."""
    cdef object cost_of_sales = prepared[0]
    cdef object due = prepared[1]
    cdef object paid = prepared[2]
    cdef object revenue = prepared[3]
    cdef object tax = prepared[4]
    cdef object loss_months = prepared[5]
    cdef object profitable_months = prepared[6]
    cdef bint closed = prepared[7]
    cdef object expenses = cost_of_sales+due
    cdef object profit = revenue-tax-expenses

    loss_months = loss_months+1 if profit < -eps else 0
    profitable_months = profitable_months+1 if profit > eps else 0
    return (due, paid, expenses, profit, loss_months, profitable_months,
        closed)


def calculate_unviable_months(object prepared):
    """Pure active-enterprise counter and close decision."""
    cdef object previous_unviable = prepared[0]
    cdef bint unviable = prepared[1]
    cdef object loss_limit = prepared[2]
    cdef object unviable_months = previous_unviable+1 if unviable else 0
    return (unviable_months, unviable_months >= loss_limit)


def run_settle(object engine, object enterprises, object hooks, object eps):
    """Пройти список предприятий; owner writes остаются hooks.

    The only native-owned state here is the local result tuple. Money and
    lifecycle remain the existing multi-store owner operations.
    """
    cdef object e, due, paid, prepared, result, unviable, unviable_result
    cdef object month, loss_limit
    cdef Py_ssize_t index, count
    cdef list rows = enterprises

    month = engine.meta['month']
    loss_limit = engine.cfg['loss_months_to_close']
    count = len(rows)
    for index in range(count):
        e = rows[index]
        # Оброк считает хук движка: у закрытого предприятия прежний код
        # брал 0.0, не вызывая enterprise_overhead.
        due = hooks.overhead(e)
        paid = min(due, hooks.free_cash(e['id']))
        if paid > eps:
            hooks.pay_overhead(e, paid)
        prepared = hooks.prepare_financial_inputs(e, due, paid)
        result = calculate_financial_results(prepared, eps)
        # The hook preserves update/assignment order before return/closure.
        hooks.apply_financial_results(e, result)
        if result[6]:
            # Возврат капитала — хук: деньги и отчёт остаются у движка,
            # прежний код делал continue сразу после него.
            hooks.return_capital(e)
            continue
        unviable = hooks.prepare_unviable_inputs(e, month, loss_limit)
        unviable_result = calculate_unviable_months(unviable)
        hooks.apply_unviable_months(e, unviable_result)
        if unviable_result[1]:
            hooks.close_enterprise(e)
    return None
