"""Минимальные проверки контрактов между расчётом и применением.

Это не новый владелец состояния. Модуль только проверяет, что результат
расчёта содержит полный набор данных до того, как boundary начнёт мутации.
"""


def require_fields(result, fields, *, contract):
    """Проверить форму результата native-фазы до применения.

    Проверка намеренно не преобразует словарь и не сортирует поля: порядок и
    значения остаются частью контракта вызывающей фазы.
    """
    missing = [field for field in fields if field not in result]
    if missing:
        raise ValueError(
            f'{contract} result missing fields: {", ".join(missing)}')
    return result


DIVIDEND_RESULT_FIELDS = (
    'owner_id', 'owner_kind', 'amount', 'paid', 'limit', 'available',
    'working', 'project', 'revision', 'reason', 'payments')

CANCELLATION_RESULT_FIELDS = ('outcome', 'reason')
