"""Единая точка записи проектного состояния на счёте.

Проектные вычисления могут читать account, но запись активного проекта и
списка муниципальных строек проходит через этот модуль. Это переходный owner:
формат данных пока остаётся совместимым с legacy dict.
"""


def attach(account, project):
    account['project'] = project


def detach(account, *, missing_ok=False):
    if missing_ok:
        return account.pop('project', None)
    return account.pop('project')


def attach_public(account, project):
    """Установить единственную активную муниципальную стройку."""
    account['public_projects'] = [project]


def replace_public(account, projects):
    """Заменить список муниципальных строек, сохранив его семантику."""
    account['public_projects'] = projects


def clear_public(account):
    """Снять муниципальные стройки с аккаунта."""
    account['public_projects'] = []


def set_field(project, field, value):
    """Записать поле живого проекта через его owner."""
    project[field] = value
