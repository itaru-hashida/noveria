# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Report/history kernel: числовые writers истории продаж и агрегатов отчёта.

Три среза из хвоста ``V3.step``:

  * ``update_sales_history`` — строки ``sales_history`` предприятий
    (``sold``, ``offered``, ``output`` и служебные поля месяца);
  * ``update_stock_sales_history`` — строки ``stock_sales_history`` счетов
    (``sold`` по товарам склада);
  * ``build_report_aggregates`` — числовые агрегаты отчёта месяца:
    ``account_flows`` (incoming/outgoing/net), ``sector_flows``,
    ``balances``, ``money_shares``, ``stocks``, ``active/open_enterprises``.

Границы среза:

  * порядок обхода — порядок словарей движка (``enterprises``/``accounts``):
    ядро сам ничего не сортирует, кроме прежних ``sorted`` в агрегатах;
  * заявки и сделки читает живой ``report`` месяца: второй копии книги нет;
  * истории пишутся в существующие dict-ы предприятий и счетов — второго
    ledger нет, source of truth экономики не меняется;
  * денежное поле ``a['protected']`` граница не трогает: это политика кассы,
    остаётся в ``v3.step`` рядом с прежним местом;
  * форматирование report (строки diagnostics, money_share-строки) остаётся
    Python-стороной: ядро возвращает ``balances``/``money_shares`` числами.

Арифметика намеренно ``object``: остатки, золото и количества бывают и int,
и float, и тип результата виден дальше в отчёте и истории. Суммы считает тот
же встроенный ``sum``: в Python 3.12 он складывает float компенсированно,
и наивное накопление разошлось бы в последнем бите, а оно меняет траекторию.
Аккумуляторы с прежним ``defaultdict(float)`` стартуют с 0.0, чтобы типы
значений не съехали от int-нуля.
"""

import builtins

# Встроенный sum: та же функция, что стояла в прежних выражениях, поэтому
# компенсация float-слагаемых и тип результата совпадают гарантированно.
_PY_SUM = builtins.sum


def update_sales_history(object engine, object eps):
    """Один проход по книге заявок: строки sales_history всех предприятий.

    Группировка sell-заявок по (participant, good) — прежняя индексация, она
    не меняет порядок: предприятия обходятся в порядке словаря ``enterprises``,
    а слагаемые каждой суммы идут в порядке списка заявок месяца.
    """
    cdef object offers_by_firm, o, e, offers
    offers_by_firm = {}
    for o in engine.report['orders']:
        if o['side'] != 'sell':
            continue
        offers_by_firm.setdefault((o['participant'], o['good']), []).append(o)
    for e in engine.enterprises.values():
        offers = offers_by_firm.get((e['id'], e['good']), ())
        history = e.get('sales_history', [])
        # Обрезка прежняя (последние три строки), но срез только по
        # положительному индексу: при wraparound=False отрицательный срез
        # в Cython не определён и дал бы другую длину истории.
        combined = history+[dict(
            month=engine.meta['month'],
            sold=_PY_SUM([o['quantity']-o['remaining'] for o in offers]),
            offered=_PY_SUM([o['quantity'] for o in offers]),
            output=e['output'], input_limited=e.get('input_limited', False),
            additional_demand=e.get('reachable_demand', 0) > eps)]
        e['sales_history'] = combined[max(len(combined)-3, 0):]


def update_stock_sales_history(object engine):
    """Строки stock_sales_history по товарам склада каждого счёта.

    Товары обходятся в порядке ``a['stock']``, сделки — в порядке отчёта;
    количества сделок группируются по (seller, good) прежней индексацией.
    ``a['protected']`` из прежнего же цикла граница не пишет: это денежное
    поле, и в v3.step оно осталось на прежнем месте.
    """
    cdef object trades_by_account, t, a, histories, good, history
    trades_by_account = {}
    for t in engine.report['trades']:
        trades_by_account.setdefault((t['seller'], t['good']), []).append(t['quantity'])
    for a in engine.accounts.values():
        for good in a['stock']:
            # setdefault — внутри обхода товаров: у счёта с пустым складом
            # прежний код не создавал пустой stock_sales_history вообще.
            histories = a.setdefault('stock_sales_history', {})
            history = histories.get(good, [])
            combined = history+[dict(sold=_PY_SUM(
                trades_by_account.get((a['id'], good), ())))]
            histories[good] = combined[max(len(combined)-3, 0):]


def build_report_aggregates(object engine, object report):
    """Числовые агрегаты отчёта месяца: flows, balances, shares, counts.

    Аккумуляторы стартуют с 0.0, как прежний ``defaultdict(float)``: тип
    значения после первого прибавления тот же. Сортировка секторов и видов
    счетов — прежние ``sorted`` с теми же ключами. Возвращает
    ``(balances, money_shares)`` числами: money_share-строки diagnostics
    форматирует Python-сторона.
    """
    cdef object incoming, outgoing, f, a, inc, out, rows, sectors, key
    cdef object kinds, balances, total_gold, shares

    incoming = {}
    outgoing = {}
    for f in report['flows']:
        incoming[f['recipient']] = incoming.get(f['recipient'], 0.0)+f['gold']
        outgoing[f['sender']] = outgoing.get(f['sender'], 0.0)+f['gold']
    rows = []
    for a in engine.accounts.values():
        inc = incoming.get(a['id'], 0.0)
        out = outgoing.get(a['id'], 0.0)
        rows.append(dict(id=a['id'], kind=a['kind'], incoming=inc,
                         outgoing=out, net=inc-out))
    report['account_flows'] = rows
    sectors = {}
    for f in report['flows']:
        key = (engine.accounts[f['sender']]['kind'],
               engine.accounts[f['recipient']]['kind'])
        sectors[key] = sectors.get(key, 0.0)+f['gold']
    report['sector_flows'] = [dict(sender_kind=s, recipient_kind=r, gold=q)
        for (s, r), q in sorted(sectors.items())]
    report['gold_after'] = engine.gold()
    kinds = sorted({a['kind'] for a in engine.accounts.values()})
    balances = {kind: _PY_SUM(a['balance'] for a in engine.accounts.values()
                              if a['kind'] == kind) for kind in kinds}
    report['balances'] = balances
    total_gold = engine.gold()
    shares = {kind: balance/total_gold if total_gold else 0
              for kind, balance in balances.items()}
    report['money_shares'] = shares
    report['stocks'] = engine.total_stock()
    report['active_enterprises'] = _PY_SUM(
        e['status'] == 'active' for e in engine.enterprises.values())
    report['open_enterprises'] = _PY_SUM(
        e['status'] != 'closed' for e in engine.enterprises.values())
    return balances, shares
