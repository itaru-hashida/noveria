# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Enterprise settlement kernel: числовой цикл оброка и прибыли.

Ядро буквально повторяет прежний цикл ``Economy.settle`` по предприятиям:
``due`` получает хук, ``paid`` считает ``min`` тем же порядком, числовые поля
(``fixed_cost_due``, ``fixed_cost_paid``, ``expenses``, ``profit``,
``loss_months``, ``profitable_months``, ``unviable_months``) пишутся в том же
порядке ключей, с теми же типами. Перевод оброка, возврат капитала и
закрытие предприятия — вызовы переданных хуков: деньги двигает прежний
``Economy.transfer`` через ``cash_store``, отчёты и события остаются Python.

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
сохранён буквально, включая чтение уже записанных полей.
"""


def run_settle(object engine, object enterprises, object hooks, object eps):
    """Пройти список предприятий и записать settlement-числа месяца."""
    cdef object e, due, paid
    cdef object month, loss_limit, unviable
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
        # Порядок ключей — прежний: dict сохраняет его при update, и новые
        # ключи встают в том же порядке, что и у Python-ветки.
        e.update(fixed_cost_due=due, fixed_cost_paid=paid)
        e['expenses'] = e['cost_of_sales']+due
        e['profit'] = e['revenue']-e['tax']-e['expenses']
        e['loss_months'] = e['loss_months']+1 if e['profit'] < -eps else 0
        e['profitable_months'] = e['profitable_months']+1 if e['profit'] > eps else 0
        if e['status'] == 'closed':
            # Возврат капитала — хук: деньги и отчёт остаются у движка,
            # прежний код делал continue сразу после него.
            hooks.return_capital(e)
            continue
        unviable = e['loss_months'] > 0 and e['available_from'] <= month
        e['unviable_months'] = e.get('unviable_months', 0)+1 if unviable else 0
        if e['unviable_months'] >= loss_limit:
            hooks.close_enterprise(e)
    return None
