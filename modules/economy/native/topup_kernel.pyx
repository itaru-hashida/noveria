# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Top-up kernel: числовой цикл допокупки базовой еды из household_top_up.

Перенесён ровно один участок ``Economy.household_top_up`` — цикл, который
идёт после снятия прошлых потребительских заявок:

  * отбор домохозяйств с деньгами и нехваткой базовой еды
    (``free_cash > EPS`` и хотя бы один товар ниже нормы);
  * обход товаров в уже перемешанном порядке и книги офферов;
  * расчёт ``needed``, ``missing`` и ``quantity``.

Всё остальное остаётся Python-хуками, потому что это не число, а side effect:

  * RNG — перемешивание домохозяйств и товаров делает ``engine.shuffled``.
    Ядро вызывает его в тот же момент, в какой это делал прежний код, поэтому
    расход генератора совпадает;
  * ``order``/``match``/``cancel_order`` — создание заявки, сделка и снятие
    остатка;
  * ``events``, ``report``, dirty flags — их трогает только движок.

Ядро работает с ``object``-значениями, а не с ``double``. ``population*rate``,
``needed-free_stock`` и ``free_cash/price`` бывают и int, и float, а тип
результата виден дальше — в количестве заявки, резерве и деньгах сделки.
Арифметика над ``object`` в Cython порождает те же ``PyNumber_*``, что и
интерпретатор, поэтому значения и типы совпадают побитово, включая
``int + int -> int`` и ``int / int -> float``.

Порядок операций сохранён буквально:

  * домохозяйства обходятся в переданном порядке; список кандидатов
    перемешивает Python-хук ровно один раз, как и прежде;
  * внутри — перемешанный список товаров ``cfg['basic_needs']``, который тоже
    готовит хук;
  * ``needed`` считается один раз на товар, ``missing`` — на каждый оффер,
    потому что ``free_stock`` читается заново;
  * ``max(0, ...)`` заменён на явную ветку: при неположительном значении
    возвращается целый ``0``, ровно как у встроенного ``max``;
  * ``min(missing, sell['remaining'], free_cash/price)`` заменён на три
    сравнения в том же порядке аргументов: встроенный ``min`` при равенстве
    оставляет первый аргумент, и тип результата сохраняется.

Второго cash/stock ledger ядро не заводит: деньги и склад меняют только
хуки, которые вызывают существующие границы движка.
"""


def run_top_up(object households, object engine, object sells, object rates,
               object eps, object hooks):
    """Пройти кандидатов и книгу офферов, отдав каждую покупку хуку.

    Ядро считает только числовое количество допкупки; порядок вызовов
    совпадает с прежним циклом ``Economy.household_top_up``.
    """
    cdef object candidates = []
    cdef object a, good, rate, needed, price, sell, quote
    cdef object missing, quantity, share
    cdef bint short

    for a in households:
        # Short-circuit прежнего условия: сначала деньги, затем товары в
        # порядке словаря. Без денег домохозяйство не проверяет склад вовсе.
        if engine.free_cash(a['id']) > eps:
            short = False
            for good, rate in rates.items():
                if engine.free_stock(a['id'], good) + eps < engine.population(a) * rate:
                    short = True
                    break
            if short:
                candidates.append(a)
    # Перемешивание кандидатов — хук: ядро только передаёт готовый список.
    for a in hooks.shuffle_candidates(candidates):
        for good in hooks.shuffle_goods():
            needed = engine.population(a) * rates[good]
            for price, sell, quote in engine.offers(a, good, sells):
                # Порядок ровно как в max(0, needed-free_stock): сначала
                # вычитание, затем отсечение нулём.
                missing = needed - engine.free_stock(a['id'], good)
                if not missing > 0:
                    missing = 0
                if missing <= eps or engine.free_cash(a['id']) <= eps:
                    break
                # min(missing, remaining, free_cash/price) с той же
                # семантикой: аргументы вычисляются все, при равенстве
                # остаётся первый.
                quantity = missing
                if sell['remaining'] < quantity:
                    quantity = sell['remaining']
                share = engine.free_cash(a['id']) / price
                if share < quantity:
                    quantity = share
                hooks.place(a, good, quantity, price)
    return None
