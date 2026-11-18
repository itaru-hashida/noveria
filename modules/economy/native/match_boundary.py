"""Границы матчинга: внутренний цикл книги и внешний цикл покупателей.

Модуль не хранит состояние и не копирует заявки: ядро получает сам движок,
живой список buyer orders, ``OfferView`` и внутренний цикл по книге. Порядок
покупателей по-прежнему задаёт Python (``shuffled(buys)``), поэтому RNG не
меняется.

Эталонные Python-реализации живут в этом же модуле: они нужны как образцы для
focused-тестов на побитовое совпадение и как fallback, если расширение не
собрано. Имена привязываются один раз при импорте, чтобы не платить за
диспетчер на каждом buyer order.

Модуль не импортирует ``modules.economy.support``: EPS приходит аргументом,
``shuffled``/``purchase_quantity``/``offers``/``observe_match`` — хуки
движка, поэтому циклического импорта нет.
"""

import builtins
import importlib
import math
from collections import namedtuple

# Встроенный min сохраняет тип аргумента: ``q`` остаётся целым, если все
# аргументы целые, — ровно как в прежнем Python-цикле. ``max`` сохраняет тип
# так же: адаптивная цена household-заявки остаётся целой при целых входах.
_PY_MAX = builtins.max
_PY_MIN = builtins.min


MatchQuantityInputs = namedtuple(
    'MatchQuantityInputs',
    'buy_remaining sell_remaining reserved price purchase_quantity')
MatchQuantityResult = namedtuple('MatchQuantityResult', 'quantity')


def _row_id(row):
    """Ключ tie-break: ID заявки продавца (прежний ``lambda row: row[1]['id']``)."""
    return row[1]['id']


def _same_level(left, right):
    """Равная полная цена: то же условие, что ``math.isclose`` в V3."""
    return left == right or math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def calculate_quantity_python(prepared, eps):
    """Независимый числовой oracle для одной уже разрешённой пары."""
    q = _PY_MIN(prepared.buy_remaining, prepared.sell_remaining,
                prepared.reserved/prepared.price, prepared.purchase_quantity)
    return MatchQuantityResult(q if q > eps else 0)


def run_match_python(owner, buy, options, sells, eps, cash, store, commit):
    """Эталон прежнего участка ``match`` после построения ``OfferView``.

    Аргументы повторяют native-ядро один в один, чтобы focused-тест мог
    прогнать обе реализации на одинаковом состоянии и сравнить побитово.
    """
    purchase = owner.purchase_quantity
    iterator = iter(options)
    pending = next(iterator, None)
    while pending is not None:
        price_level = pending[0]
        # Группа равной полной цены: только она перемешивается, чтобы равная
        # цена не давала постоянного преимущества одному ID.
        tied = [pending]
        pending = next(iterator, None)
        while pending is not None and _same_level(price_level, pending[0]):
            tied.append(pending)
            pending = next(iterator, None)
        for price, sell, quote in owner.shuffled(tied, key=_row_id):
            if price > buy['price']+eps:
                continue
            q = _PY_MIN(buy['remaining'], sell['remaining'], buy['reserved']/price,
                        purchase(buy, sells))
            if q <= eps:
                continue
            commit(owner, buy, sell, quote, q, price, eps, cash, store)
            if buy['remaining'] <= eps:
                break
        if buy['remaining'] <= eps:
            break


def run_buyer_python(owner, buy, sells, eps, commit, inner, local_only=False):
    """Эталон прежнего тела ``Economy.match`` вместе с финализацией статуса."""
    if buy is None or buy['status'] in ('cancelled', 'expired', 'filled'):
        return None
    # Ссылки на границы берутся один раз на заявку: внутри цикла сделок
    # мутаций много, и property на каждую стоил бы дороже самой записи.
    store = owner.order_store
    cash = owner.cash_store
    owner.reprice_input_order(buy, sells)
    buyer = owner.accounts[buy['participant']]
    if (buy.get('adaptive_household_budget') and buyer['kind'] == 'household'
            and buy.get('final_consumption') and buy['remaining'] > eps):
        # Первая оценка корзины не является жёстким пользовательским лимитом.
        # Пересмотр возможен лишь за счёт денег вне остальных обязательств.
        _, ceiling, _ = owner.quoted_cost(buyer, buy['good'], buy['remaining'], sells)
        budget = buy['reserved']+owner.free_cash(buy['participant'])
        price = _PY_MAX(buy['price'], _PY_MIN(ceiling, budget/buy['remaining']))
        if price > buy['price']+eps:
            extra = buy['remaining']*(price-buy['price'])
            cash.add_reserved_cash(buy['participant'], extra)
            store.set_reserved(buy, buy['reserved']+extra)
            buy.setdefault('initial_limit', buy['price'])
            store.set_price(buy, price)
            buy['backing'] = buy['reserved']
            owner._demand_revision[buy['good']] += 1
    options = (owner.offers(buyer, buy['good'], sells, local_only=True)
        if local_only else owner.offers(buyer, buy['good'], sells))
    observation = owner.observe_match(buy, sells, options)
    inner(owner, buy, options, sells, eps, cash, store, commit)
    store.set_status(buy, 'filled' if buy['remaining'] <= eps else 'partial' if buy['remaining'] < buy['quantity'] else 'unfilled')
    owner.finish_match_observation(buy, sells, observation)
    if buy['remaining'] > eps:
        buy['reason'] = observation['reason_code']
    return None


def run_buyers_python(owner, buys, sells, eps, commit, inner, local_only=False):
    """Эталон внешнего цикла: тот же порядок, без копий и без нового RNG."""
    for buy in buys:
        run_buyer_python(owner, buy, sells, eps, commit, inner, local_only)
    return None


def load_match_kernel():
    """Вернуть native-модуль match_kernel или None, если он не собран.

    Отсутствие расширения даёт ImportError: пакет ``modules.economy.native``
    существует, а подмодуля нет. Это значит одно — ядро не собрано, берём
    эталон.
    """
    try:
        return importlib.import_module('.match_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


def load_match_driver():
    """Вернуть native-модуль match_driver или None, если он не собран.

    Условие то же, что и у внутреннего цикла: отдельное расширение, отдельная
    проверка. Без него внешний цикл остаётся Python-эталоном.
    """
    try:
        return importlib.import_module('.match_driver', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


def _run_match_calculated(owner, buy, options, sells, eps, cash, store, commit,
                          calculate):
    """Python-owned traversal using only the supplied numeric calculation."""
    purchase = owner.purchase_quantity
    iterator = iter(options)
    pending = next(iterator, None)
    while pending is not None:
        price_level = pending[0]
        tied = [pending]
        pending = next(iterator, None)
        while pending is not None and _same_level(price_level, pending[0]):
            tied.append(pending)
            pending = next(iterator, None)
        for price, sell, quote in owner.shuffled(tied, key=_row_id):
            if price > buy['price']+eps:
                continue
            prepared = MatchQuantityInputs(
                buy['remaining'], sell['remaining'], buy['reserved'],
                price, purchase(buy, sells))
            q = calculate(prepared, eps).quantity
            if q <= eps:
                continue
            commit(owner, buy, sell, quote, q, price, eps, cash, store)
            if buy['remaining'] <= eps:
                break
        if buy['remaining'] <= eps:
            break


def run_match(owner, buy, options, sells, eps, cash, store, commit):
    """Native numeric calculation with Python-owned matcher traversal."""
    return _run_match_calculated(owner, buy, options, sells, eps, cash, store,
                                 commit, calculate_quantity)


NATIVE_MATCH_KERNEL = False
NATIVE_MATCH_DRIVER = False

_kernel = load_match_kernel()
if _kernel is not None:
    def calculate_quantity(prepared, eps):
        """Return the named native result without exposing live state."""
        return MatchQuantityResult(_kernel.calculate_quantity(prepared, eps))
    NATIVE_MATCH_KERNEL = True
else:
    calculate_quantity = calculate_quantity_python

# ``match_driver`` remains importable for compatibility diagnostics, but it
# owns the old owner-taking orchestration and is deliberately not a production
# entry point. Buyer traversal and all callbacks stay in Python.
_driver = load_match_driver()
run_buyer = run_buyer_python
run_buyers = run_buyers_python
