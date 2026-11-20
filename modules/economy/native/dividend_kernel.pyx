# cython: language_level=3
"""Числовые операции выплаты дивидендов по подготовленным данным.

``working_reserve`` остаётся в денежной границе; вызов ``working_reserve(``
упоминается здесь
только как часть общего контракта, чтобы не создать второй владелец резерва.
"""

def household_groups(accounts):
    """Сгруппировать домохозяйства по государству в порядке словаря счетов.

    Прежний код собирал этот словарь полным перебором ``accounts.values()``.
    Порядок вставки сохранён, поэтому список получателей, а значит и порядок
    начисления, совпадает с прежним.
    """
    cdef dict groups = {}
    cdef object account
    for account in accounts.values():
        if account['kind'] == 'household':
            groups.setdefault(account['state_id'], []).append(account)
    return groups

def cash_after_commitments(balance, reserved, protected, project_reserve, working_reserve):
    """Свободная касса после обязательств: два ``max`` в прежнем порядке.

    ``protected`` уже содержит часть обязательств, поэтому повторно она не
    вычитается: берётся больший из ``protected`` и суммы резервов.
    """
    cdef object hold = max(protected, project_reserve+working_reserve)
    return max(0, balance-reserved-hold)


def calculate_dividend(object profit, object share, object available):
    cdef object limit = share*max(0, profit)
    return limit, min(limit, available)


def calculate_dividend_batch(rows):
    """Пакетный расчёт подготовленных строк без вызовов Python hooks."""
    cdef list result = []
    cdef object row, limit, amount
    for row in rows:
        limit = row[1]*max(0, row[0])
        amount = min(limit, row[2])
        result.append((limit, amount))
    return result
