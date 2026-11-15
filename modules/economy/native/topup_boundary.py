"""Граница top-up-ядра: один вызов из household_top_up, эталон и fallback.

В Cython уходит только числовой цикл допокупки базовой еды:

  * отбор домохозяйств (``free_cash > EPS`` и нехватка хотя бы одного товара);
  * обход товаров и книги офферов;
  * расчёт ``needed``, ``missing`` и ``quantity``.

Всё остальное остаётся Python-хуками, потому что это не число:

  * ``engine.shuffled`` — RNG; хук вызывается в тот же момент, что и прежний
    код, поэтому расход генератора совпадает;
  * ``engine.order``/``engine.match``/``engine.cancel_order`` — заявка, сделка
    и снятие остатка. Второго cash/stock ledger здесь нет: деньги и склад
    двигают существующие границы движка;
  * поля заявки ``household_top_up`` и ``report['household_top_up']`` —
    прежний код движка, он остаётся в ``Economy.household_top_up``.

EPS приходит аргументом из единственного call site: своей копии константы у
границы нет, поэтому допуск не может разойтись с проектом.

Эталонная Python-ветка (``run_top_up_python``) — буквальный прежний цикл из
``Economy.household_top_up``. Она служит образцом для focused-теста на
побитовое совпадение с ядром и fallback, если расширение не собрано.
"""
import importlib


class TopUpHooks(object):
    """Побочные эффекты допкупки, которые обязаны остаться в Python.

    Ядро вызывает эти методы в том же порядке, в каком прежний цикл создавал
    заявки, поэтому RNG расходуется так же, а заявки появляются в прежней
    последовательности.
    """

    __slots__ = ('engine', 'sells')

    def __init__(self, engine, sells):
        self.engine = engine
        # Книга офферов остаётся объектом движка: хук только держит ссылку,
        # чтобы не тащить её через ядро и не копировать.
        self.sells = sells

    def shuffle_candidates(self, households):
        """Перемешать кандидатов — ровно тот же вызов ``engine.shuffled``.

        Аргумент-ключ прежнего вызова — ``lambda item: item['id']``, он же
        остаётся по умолчанию у ``shuffled``, поэтому порядок и расход RNG
        совпадают.
        """
        return self.engine.shuffled(households)

    def shuffle_goods(self):
        """Перемешать товары базовых нужд в прежнем порядке ключей."""
        return self.engine.shuffled(self.engine.cfg['basic_needs'], key=lambda g: g)

    def place(self, account, good, quantity, price):
        """Создать заявку допкупки, провести сделку и снять остаток."""
        engine = self.engine
        buy = engine.order(account['id'], good, quantity, 'buy', price, 'Базовые нужды')
        if buy:
            buy['household_top_up'] = True
            engine.match(buy, self.sells)
            engine.cancel_order(buy, 'Окончание допокупки')


def run_top_up_python(households, engine, sells, rates, eps, hooks):
    """Эталон числового цикла top-up — буквальный прежний код V3."""
    candidates = [a for a in households if engine.free_cash(a['id']) > eps
        and any(engine.free_stock(a['id'], g) + eps < engine.population(a)*rate
            for g, rate in rates.items())]
    for a in hooks.shuffle_candidates(candidates):
        for good in hooks.shuffle_goods():
            needed = engine.population(a)*rates[good]
            for price, sell, _ in engine.offers(a, good, sells):
                missing = max(0, needed-engine.free_stock(a['id'], good))
                if missing <= eps or engine.free_cash(a['id']) <= eps:
                    break
                quantity = min(missing, sell['remaining'], engine.free_cash(a['id'])/price)
                hooks.place(a, good, quantity, price)


def top_up(engine, households, sells, eps):
    """Единственный вход из household_top_up: native-цикл или эталон."""
    run_top_up(households, engine, sells, engine.cfg['basic_needs'], eps,
               TopUpHooks(engine, sells))


def top_up_python(engine, households, sells, eps):
    """Эталонная Python-ветка целиком: те же аргументы и те же хуки."""
    run_top_up_python(households, engine, sells, engine.cfg['basic_needs'], eps,
                      TopUpHooks(engine, sells))


def load_top_up_kernel():
    """Вернуть native-модуль topup_kernel или None, если он не собран.

    Отсутствие собранного расширения даёт ImportError, а не
    ModuleNotFoundError: пакет ``modules.economy.native`` существует, а
    подмодуля нет. Обе ситуации означают одно — ядро не собрано.
    """
    try:
        return importlib.import_module('.topup_kernel', __package__)
    except (ImportError, ModuleNotFoundError):
        return None


NATIVE_KERNEL = False

_kernel = load_top_up_kernel()
if _kernel is not None:
    run_top_up = _kernel.run_top_up
    NATIVE_KERNEL = True
else:
    run_top_up = run_top_up_python
