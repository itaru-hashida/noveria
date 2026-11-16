"""Граница report/history-ядра: одна привязка реализации на импорт.

Ядро пишет числовые истории (``sales_history``, ``stock_sales_history``) и
числовые агрегаты отчёта месяца в существующие живые контейнеры движка —
второй копии книг, складов и ledger'ов нет. Порядок строк, порядок
суммирования, int/float-типы и RNG сохранены буквально; заявки и сделки
читает живой ``report`` месяца.

Что осталось в Python и почему:

  * ``a['protected']`` из прежнего цикла истории — денежное поле кассы,
    остаётся в ``v3.step``;
  * money_share-строки ``report['diagnostics']`` — форматирование отчёта,
    граница лишь возвращает ``balances``/``money_shares`` числами;
  * сериализация, UI/API и полный report builder не затрагиваются.

Эталонная Python-реализация живёт в этом же модуле: она нужна как образец
для focused-теста на побитовое совпадение и как fallback, если расширение
не собрано. Имена привязываются один раз при импорте.
"""

import builtins
import importlib

# Встроенный sum: компенсация float-слагаемых в Python 3.12 обязана остаться
# прежней, поэтому эталон суммирует той же функцией, что и прежний код.
_PY_SUM = builtins.sum


def update_sales_history_python(engine, eps):
    """Эталон строк sales_history — буквальный прежний код V3."""
    offers_by_firm = {}
    for o in engine.report['orders']:
        if o['side'] != 'sell':
            continue
        offers_by_firm.setdefault((o['participant'], o['good']), []).append(o)
    for e in engine.enterprises.values():
        offers = offers_by_firm.get((e['id'], e['good']), ())
        e['sales_history'] = (e.get('sales_history', [])+[dict(
            month=engine.meta['month'],
            sold=_PY_SUM([o['quantity']-o['remaining'] for o in offers]),
            offered=_PY_SUM([o['quantity'] for o in offers]),
            output=e['output'], input_limited=e.get('input_limited', False),
            additional_demand=e.get('reachable_demand', 0) > eps)])[-3:]


def update_stock_sales_history_python(engine):
    """Эталон строк stock_sales_history — буквальный прежний код V3."""
    trades_by_account = {}
    for t in engine.report['trades']:
        trades_by_account.setdefault((t['seller'], t['good']), []).append(t['quantity'])
    for a in engine.accounts.values():
        for good in a['stock']:
            # setdefault — внутри обхода товаров: у счёта с пустым складом
            # прежний код не создавал пустой stock_sales_history вообще.
            histories = a.setdefault('stock_sales_history', {})
            history = histories.get(good, [])
            histories[good] = (history+[dict(sold=_PY_SUM(
                trades_by_account.get((a['id'], good), ())))])[-3:]


def build_report_aggregates_python(engine, report):
    """Эталон числовых агрегатов — буквальный прежний код V3."""
    from collections import defaultdict
    incoming_by_account, outgoing_by_account = defaultdict(float), defaultdict(float)
    for f in report['flows']:
        incoming_by_account[f['recipient']] += f['gold']
        outgoing_by_account[f['sender']] += f['gold']
    report['account_flows'] = []
    for a in engine.accounts.values():
        incoming = incoming_by_account[a['id']]
        outgoing = outgoing_by_account[a['id']]
        report['account_flows'].append(dict(id=a['id'], kind=a['kind'],
            incoming=incoming, outgoing=outgoing, net=incoming-outgoing))
    sectors = defaultdict(float)
    for f in report['flows']:
        sectors[engine.accounts[f['sender']]['kind'],
                engine.accounts[f['recipient']]['kind']] += f['gold']
    report['sector_flows'] = [dict(sender_kind=s, recipient_kind=r, gold=q)
        for (s, r), q in sorted(sectors.items())]
    report['gold_after'] = engine.gold()
    report['balances'] = {kind: _PY_SUM(a['balance'] for a in engine.accounts.values()
                                        if a['kind'] == kind)
                          for kind in sorted({a['kind'] for a in engine.accounts.values()})}
    total_gold = engine.gold()
    report['money_shares'] = {kind: balance/total_gold if total_gold else 0
        for kind, balance in report['balances'].items()}
    report['stocks'] = engine.total_stock()
    report['active_enterprises'] = _PY_SUM(
        e['status'] == 'active' for e in engine.enterprises.values())
    report['open_enterprises'] = _PY_SUM(
        e['status'] != 'closed' for e in engine.enterprises.values())
    return report['balances'], report['money_shares']


def load_report_history_kernel():
    """Вернуть native-модуль report_history_kernel или None.

    Отсутствие расширения даёт ImportError, а не ModuleNotFoundError: пакет
    ``modules.economy.native`` существует, а подмодуля нет. Обе ситуации
    означают одно — расширение не собрано, берём эталон.
    """
    try:
        return importlib.import_module('.report_history_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_report_history_kernel()
if _kernel is not None:
    update_sales_history = _kernel.update_sales_history
    update_stock_sales_history = _kernel.update_stock_sales_history
    build_report_aggregates = _kernel.build_report_aggregates
    NATIVE_KERNEL = True
else:
    update_sales_history = update_sales_history_python
    update_stock_sales_history = update_stock_sales_history_python
    build_report_aggregates = build_report_aggregates_python
