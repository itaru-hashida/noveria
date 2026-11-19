"""Точка записи инвестиционного проекта на счёте."""


def attach(account, project):
    account['project'] = project


def detach(account, *, missing_ok=False):
    if missing_ok:
        return account.pop('project', None)
    return account.pop('project')
