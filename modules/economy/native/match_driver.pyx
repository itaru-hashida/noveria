# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Внешний цикл покупателей ``Economy.match``: порядок, книга и статус заявки.

Предыдущий уровень (``match_kernel``) обходит книгу одного покупателя. Здесь
переносится следующий: обход уже перемешанного списка buyer orders. Python
по-прежнему задаёт порядок (``shuffled(buys)``) и владеет всеми хуками, ядро
вызывает их в прежнем порядке:

  * ``reprice_input_order`` — перед выбором книги;
  * ``offers`` — выбор market book (первый общий проход заполняет её кэш);
  * ``observe_match`` — наблюдение до сделок;
  * внутренний Cython-цикл (``match_kernel.run_match``) передаётся аргументом;
  * ``finish_match_observation`` и запись ``reason`` — после сделок.

``purchase_quantity`` и ``shuffled`` вызывает сам внутренний цикл, поэтому
расход RNG остаётся прежним: ядро не перемешивает список покупателей и не
трогает генератор. Копий заявок нет: перебирается тот же список и те же
словари, что и ``report['orders']``. Границы ``OrderStore`` и ``CashStore``
берутся у движка на каждую заявку, ровно как в прежнем ``match``.

Блок адаптивной цены household-заявки перенесён буквально: он был частью
прежнего тела ``match`` внутри того же call site. Новых транзакционных правил
и rollback здесь нет.

Перенос архитектурный: на каждого покупателя приходится Python-callback
(хуки книги, наблюдений и финализации), поэтому прироста скорости он не
обещает.
"""

import builtins

# Встроенные max/min сохраняют тип аргумента: ``price`` остаётся целым, если
# все аргументы целые, — ровно как в прежнем Python-коде.
_PY_MAX = builtins.max
_PY_MIN = builtins.min


cdef object _run_buyer(object owner, object buy, object sells, object eps,
                       object commit, object inner, bint local_only):
    """Один buyer order: ровно прежний участок ``Economy.match``."""
    cdef object store
    cdef object cash
    cdef object buyer
    cdef object options
    cdef object observation
    cdef object quoted
    cdef object ceiling
    cdef object budget
    cdef object price
    cdef object extra

    if buy is None or buy['status'] in ('cancelled', 'expired', 'filled'):
        return None
    # Ссылка на границу берётся один раз на заявку: внутри цикла сделок
    # мутаций много, и property на каждую стоил бы дороже самой записи.
    store = owner.order_store
    # Денежная граница тоже одна на заявку: счета сдвигаются только между
    # ходами, поэтому внутри цикла сделок тождество словаря не меняется.
    cash = owner.cash_store
    owner.reprice_input_order(buy, sells)
    buyer = owner.accounts[buy['participant']]
    if (buy.get('adaptive_household_budget') and buyer['kind'] == 'household'
            and buy.get('final_consumption') and buy['remaining'] > eps):
        # Первая оценка корзины не является жёстким пользовательским лимитом.
        # Пересмотр возможен лишь за счёт денег вне остальных обязательств.
        quoted = owner.quoted_cost(buyer, buy['good'], buy['remaining'], sells)
        ceiling = quoted[1]
        budget = buy['reserved']+owner.free_cash(buy['participant'])
        price = _PY_MAX(buy['price'], _PY_MIN(ceiling, budget/buy['remaining']))
        if price > buy['price']+eps:
            extra = buy['remaining']*(price-buy['price'])
            cash.add_reserved_cash(buy['participant'], extra)
            store.set_reserved(buy, buy['reserved']+extra)
            buy.setdefault('initial_limit', buy['price'])
            store.set_price(buy, price)
            store.set_metadata(buy, 'backing', buy['reserved'])
            owner._demand_revision[buy['good']] += 1
    options = (owner.offers(buyer, buy['good'], sells, local_only=True)
        if local_only else owner.offers(buyer, buy['good'], sells))
    observation = owner.observe_match(buy, sells, options)
    # Порядок offers, tie-break, расчёт q и остановка при полном исполнении —
    # в существующем внутреннем цикле; он приходит аргументом, поэтому
    # focused-тест может прогнать ту же последовательность с Python-эталоном.
    inner(owner, buy, options, sells, eps, cash, store, commit)
    store.set_status(buy, 'filled' if buy['remaining'] <= eps else 'partial' if buy['remaining'] < buy['quantity'] else 'unfilled')
    owner.finish_match_observation(buy, sells, observation)
    if buy['remaining'] > eps:
        store.set_metadata(buy, 'reason', observation['reason_code'])
    return None


def run_buyer(object owner, object buy, object sells, object eps, object commit,
              object inner, bint local_only=False):
    """Выполнить один buyer order: статус, книга, сделки и наблюдение."""
    return _run_buyer(owner, buy, sells, eps, commit, inner, local_only)


def run_buyers(object owner, object buys, object sells, object eps, object commit,
               object inner, bint local_only=False):
    """Обойти покупателей в переданном порядке: прежний внешний цикл V3.

    Список уже перемешан вызовом ``owner.shuffled(buys)``: ядро не меняет
    порядок и не тратит RNG, поэтому последовательность сделок остаётся той
    же, что и у цикла ``for buy in ordered_buys``.
    """
    for buy in buys:
        _run_buyer(owner, buy, sells, eps, commit, inner, local_only)
    return None
