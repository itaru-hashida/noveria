"""Чистое планирование: агент видит только свою строку снимка и публичный рынок.

Почему так, а не вызовом V3: `production_outlook`, `unit_cost`, `landed` и
`customer_quotes` кэшируются по `id()` живых объектов. В дочернем процессе `id`
чужой, поэтому кэши движка там недействительны. Здесь все нужные величины уже
лежат в снимке числами, а функция не имеет состояния вовсе.

Формулы повторяют V3: себестоимость по доставленным ценам входов, рентабельную
цену не поднимаем, план режется бюджетом, а не задним числом.
"""
import numpy as np

from modules.economy.support import EPS

# Сколько офферов товара просматривать при оценке конкурентов. Книга
# отсортирована по доставленной цене, дальше идти незачем.
FORECAST_ALPHA = 0.35
OUTPUT_BUFFER = 0.25
INPUT_BUFFER = 0.1
INITIAL_SALES_FRACTION = 0.5


def _landed(distance_row, offer_tile, offer_price, weight, rate):
    return offer_price+distance_row[offer_tile]*weight*rate


def reference_price(book, tile, good, rate):
    """Лучшая доставленная цена предложения, иначе базовая цена контента."""
    weight = book['good_weight'][good]
    distance_row = book['distance'][tile]
    ptr = book['offer_ptr']
    start, end = int(ptr[good]), int(ptr[good+1])
    best = None
    for index in range(start, end):
        if book['offer_remaining'][index] <= EPS:
            continue
        if not book['reachable'][book['offer_tile'][index], tile]:
            continue
        landed = _landed(distance_row, book['offer_tile'][index],
                         book['offer_price'][index], weight, rate)
        if best is None or landed < best:
            best = landed
    if best is not None:
        return float(best)
    historical = float(book.get('public_price', book['good_price'])[good])
    return historical if historical > EPS else float(book['good_price'][good])


def reachable_demand(book, tile, good, price, rate):
    """Сколько своего товара агент продаст: публичный спрос минус конкуренты.

    Делить публичный спрос на число продавцов пробовали — стало хуже
    (потребление хлеба 1668 -> 1226): заявки на промежуточные товары рождаются
    в том же месяце, и занижённая доля оставляет пекарни без муки. Поэтому
    спрос оценивается целиком, а переоценка гасится предельной ценой заявки и
    бюджетом.
    """
    weight = book['good_weight'][good]
    distance_row = book['distance'][tile]
    ask_ptr, offer_ptr = book['ask_ptr'], book['offer_ptr']
    total = 0.0
    for ask_index in range(int(ask_ptr[good]), int(ask_ptr[good+1])):
        ask_tile = book['ask_tile'][ask_index]
        if not book['reachable'][tile, ask_tile]:
            continue
        if book['ask_remaining'][ask_index] <= EPS:
            continue
        freight = distance_row[ask_tile]*weight*rate
        limit = book['ask_price'][ask_index]-freight
        if limit+EPS < price:
            continue
        landed = price+freight
        reserved = book['ask_reserved'][ask_index]
        affordable = min(book['ask_remaining'][ask_index],
                         reserved/max(landed, EPS)) if reserved > EPS else 0.0
        if affordable <= EPS:
            continue
        # Дешёвые конкуренты забирают спрос раньше агента.
        covered = 0.0
        for offer_index in range(int(offer_ptr[good]), int(offer_ptr[good+1])):
            if book['offer_remaining'][offer_index] <= EPS:
                continue
            competitor = _landed(distance_row, book['offer_tile'][offer_index],
                                 book['offer_price'][offer_index], weight, rate)
            if competitor >= landed-EPS:
                continue
            covered += book['offer_remaining'][offer_index]
            if covered >= affordable:
                break
        if covered < affordable:
            total += affordable-covered
    return float(total)


def expectations(book, agent, observed):
    """Отсутствие текущего покупателя не стирает историю продаж."""
    count = int(book['agent_history_count'][agent])
    good = int(book['agent_good'][agent])
    prior = float(book['agent_forecast'][agent])
    public = float(book['public_share'][good])
    if count:
        history = book['agent_history'][agent, :count]
        own = float(np.mean(history[:, 0]))
        # Недовыпуск из-за входов не является наблюдением исчезнувшего спроса.
        if history[-1, 2]:
            own = max(own, prior)
        signal = max(own, public, observed)
    elif book['agent_initialized'][agent]:
        signal = max(public, observed)
    else:
        signal = max(public, observed)
        if signal <= EPS:
            signal = float(book['agent_capacity'][agent])*INITIAL_SALES_FRACTION
        prior = signal
    return max(observed, (1-FORECAST_ALPHA)*prior+FORECAST_ALPHA*signal)


def volume_quote(book, tile, good, quantity, rate):
    """Предельная цена покрытия объёма по книге — оставлено для совместимости.

    `plan()` больше не использует: он идёт по `offer_curve` и берёт цену
    последней реально взятой единицы через `input_plan`. Функция оставлена,
    потому что на неё ссылаются старые пробы замера.
    """
    curve = offer_curve(book, tile, good, rate)
    if not curve:
        return reference_price(book, tile, good, rate)
    left, marginal = quantity, None
    for landed, available in curve:
        take = min(left, available)
        if take > EPS:
            marginal = landed
        left -= take
        if left <= EPS:
            break
    if left > EPS:
        marginal = max(marginal or 0.0, reference_price(book, tile, good, rate))
    return marginal if marginal is not None else reference_price(book, tile, good, rate)


def offer_curve(book, tile, good, rate):
    """Достижимые офферы товара для площадки: [(landed, available)] по возрастанию.

    Это и есть «кривая предложения», о которой говорит задача: каждое звено —
    одна доставленная цена и сколько товара по ней реально доступно. Раньше
    функции брали минимум или предельную цену по несортированному списку и
    теряли количество, поэтому нельзя было сказать, покрывает ли книга объём.

    Уровни с одинаковой landed-ценой сливаются: заявка всё равно их не
    различает, а суммарное количество — различает.
    """
    weight = book['good_weight'][good]
    distance_row = book['distance'][tile]
    ptr = book['offer_ptr']
    rows = []
    for index in range(int(ptr[good]), int(ptr[good+1])):
        remaining = float(book['offer_remaining'][index])
        if remaining <= EPS:
            continue
        offer_tile = int(book['offer_tile'][index])
        if not book['reachable'][offer_tile, tile]:
            continue
        landed = _landed(distance_row, offer_tile, book['offer_price'][index], weight, rate)
        rows.append((landed, remaining))
    rows.sort()
    merged = []
    for landed, quantity in rows:
        if merged and abs(merged[-1][0]-landed) <= EPS:
            merged[-1] = (merged[-1][0], merged[-1][1]+quantity)
        else:
            merged.append((landed, quantity))
    return merged


def input_plan(book, tile, good, wanted, ceiling, cash, rate, rivals=1):
    """Сколько закупить и по какой предельной цене.

    Идёт по кривой офферов и берёт единицы, пока они дешевле `ceiling`
    (маржа), пока хватает `cash` (бюджет, зарплата, обязательства) и пока не
    набран `wanted` объём. Предел заявки — landed-цена **последней взятой**
    единицы, а не лучшая цена и не средняя: иначе заявка не дотянется до
    второго уровня после выкупа первого.

    `rivals` — сколько ещё покупателей товара претендуют на те же офферы. Один
    уровень книги делится между ними, поэтому на себя рассчитывается
    `available/rivals` единиц. Без этого заявка ставила предел по первому
    уровню (641 единица по 1,7733), которого ей не достанется: её выкупали
    соседи, а сама она оставалась с ценой ниже следующего уровня и получала
    landed_price_above_limit. Замер: 27 отклонённых заявок на муку при 2313
    муки на складах и 1483 недополученного спроса.

    Если книга не покрывает объём, возвращается доступное количество: спрос
    необеспеченный не создаётся. Возвращает (quantity, bid, available_total,
    affordable_total) — для трассировки видно, что именно ограничило.
    """
    curve = offer_curve(book, tile, good, rate)
    if not curve:
        return 0.0, 0.0, 0.0, 0.0
    rivals = max(1, int(rivals))
    available_total = sum(quantity for _, quantity in curve)
    affordable_total = sum(quantity for price, quantity in curve if price <= ceiling+EPS)
    if wanted <= EPS or cash <= EPS:
        return 0.0, 0.0, available_total, affordable_total
    left, covered, spent, bid = wanted, 0.0, 0.0, 0.0
    for landed, quantity in curve:
        if landed > ceiling+EPS:
            break
        if spent >= cash-EPS:
            break
        # Своя часть уровня: остальное достанется другим покупателям.
        share = quantity/rivals
        take = share if share < left else left
        if landed > EPS:
            take = min(take, (cash-spent)/landed)
        if take <= EPS:
            # Уровень целиком уйдёт соседям — двигаемся к следующему.
            continue
        left -= take
        covered += take
        spent += take*landed
        bid = landed
        if left <= EPS:
            break
    return covered, bid, available_total, affordable_total


def plan(book, agent, rate, wage, tax, markup, overhead_rate, trace=None):
    """Ожидания → выпуск → обеспеченные закупки; реальные продажи не обещаны."""
    good = int(book['agent_good'][agent])
    tile = int(book['agent_tile'][agent])
    capacity = float(book['agent_capacity'][agent])
    price = float(book['agent_price'][agent])
    stock = book.get('agent_available_stock', book['agent_stock'])[agent]
    budget = float(book['agent_budget'][agent])
    inputs = [(int(g), float(book['good_inputs'][good, g]))
              for g in np.nonzero(book['good_inputs'][good])[0]]
    equipment = [(int(g), float(book['good_equip'][good, g]))
                 for g in np.nonzero(book['good_equip'][good])[0]]
    quotes = {g: float(book['agent_input_price'][agent, g])
              for g, _ in inputs+equipment}
    wage_unit = float(book['good_labor'][good])*wage
    costs = dict(inputs)
    for g, ratio in equipment:
        costs[g] = costs.get(g, 0.0)+ratio*float(book['good_wear'][g])
    # Себестоимость единицы — как в V3 (`unit_cost`): по ориентиру лучшей
    # доставленной цены, а не по среднему на весь объём. Средняя цена на объём
    # нужна ниже, для предела заявки, иначе фирма не дотягивается до второго
    # оффера и простои растут.
    unit_cost = wage_unit+sum(r*quotes[g] for g, r in costs.items())
    net = 1.0-tax*float(book['good_taxed'][good])
    sale_price = price if price*net+EPS >= unit_cost else unit_cost*(1+markup)/max(net, EPS)
    observed = reachable_demand(book, tile, good, sale_price, rate)
    forecast = expectations(book, agent, observed)
    own_stock = float(book['agent_stock'][agent, good])
    projected_stock = max(0.0, own_stock-min(own_stock, forecast))
    desired = max(0.0, min(capacity, forecast*(1+OUTPUT_BUFFER)-projected_stock))
    overhead = capacity*wage_unit*overhead_rate
    fixed_unit = overhead/desired if desired > EPS else 0.0
    reason = 'План'
    if desired <= EPS:
        reason = 'Готовые запасы' if forecast > EPS else 'Нет ожидаемого спроса'
    elif sale_price*net-unit_cost-fixed_unit < -EPS:
        desired, reason = 0.0, 'Отрицательная ожидаемая маржа'

    # Оборудование требуется целиком, но в себестоимость входит лишь износ.
    requirements = dict(inputs)
    for g, ratio in equipment:
        requirements[g] = requirements.get(g, 0.0)+ratio
    # Предел по марже: цена, при которой маржа ещё не отрицательна.
    ceilings = {}
    for g, ratio in requirements.items():
        other_cost = unit_cost-costs.get(g, 0.0)*quotes[g]+fixed_unit
        marginal_ratio = costs.get(g, ratio)
        ceilings[g] = max(0.0, (sale_price*net-other_cost)/max(marginal_ratio, EPS))

    def wanted_for(output, g, ratio):
        # Буфер запаса расширяет запрос, но предел цены берётся по тому
        # объёму, который реально нужен производству. Иначе последняя единица
        # буфера тянет предел вверх на чужой уровень цены, а следующая заявка
        # соседа остаётся с пределом чуть ниже landed и отклоняется.
        return max(0.0, output*ratio*(1+INPUT_BUFFER)-float(stock[g]))

    def purchases_for(output, cash):
        """Закупки по кривой офферов: объём и предел цены из доступного."""
        if output <= EPS:
            return []
        rows = []
        remaining_cash = cash
        for g, ratio in requirements.items():
            wanted = wanted_for(output, g, ratio)
            if wanted <= EPS:
                continue
            quantity, bid, available, affordable = input_plan(
                book, tile, g, wanted, ceilings[g], remaining_cash, rate,
                rivals=book['input_buyers'][g] if 'input_buyers' in book else 1)
            if quantity <= EPS or bid <= EPS:
                continue
            rows.append((g, quantity, bid, wanted, available, affordable))
            remaining_cash = max(0.0, remaining_cash-quantity*bid)
        return rows

    def cash_needed(output):
        """Полная потребность с учётом зарплатного резерва и оброка.

        Закупка не может тратить деньги, зарезервированные под зарплату и
        оброк, поэтому кривая офферов ограничена остатком, а не всем бюджетом.
        Раньше здесь стоял полный бюджет, и бисекция принимала план, который
        потом нечем было оплатить: заявки уходили в отказ.
        """
        purchase_cash = max(0.0, budget-overhead-output*wage_unit)
        rows = purchases_for(output, cash=purchase_cash)
        return overhead+output*wage_unit+sum(q*p for _, q, p, *_ in rows)

    target = desired
    if target > EPS and cash_needed(target) > budget+EPS:
        low, high = 0.0, target
        for _ in range(40):
            middle = (low+high)/2
            if cash_needed(middle) <= budget:
                low = middle
            else:
                high = middle
        target, reason = low, 'Деньги на выпуск'
    # Деньги на закупку: бюджет минус оброк и зарплата принятого плана.
    # Зарплатный резерв и оброк защищены до закупок, как в V3.
    purchase_cash = max(0.0, budget-overhead-target*wage_unit)
    # План публикуется после бюджетного ограничения даже при пустом складе.
    intents = [('sell', good, own_stock, sale_price, 'Продажа запасов', target, reason)]
    purchases = purchases_for(target, cash=purchase_cash)
    intents.extend(('buy', g, quantity, bid, 'Сырьё', target, reason)
                   for g, quantity, bid, *_ in purchases if quantity > EPS)
    if trace is not None:
        trace.update(observed=observed, forecast=forecast, stock=own_stock,
                     history_count=int(book['agent_history_count'][agent]),
                     prior=float(book['agent_forecast'][agent]),
                     public=float(book['public_share'][good]),
                     projected_stock=projected_stock, desired=desired, target=target,
                     wage_reserve=target*wage_unit, overhead=overhead,
                     purchases=[(g, quantity, bid) for g, quantity, bid, *_ in purchases],
                     reasons=[dict(good=int(g), needed=needed, available=available,
                                   affordable=affordable, bought=quantity, bid=bid)
                              for g, quantity, bid, needed, available, affordable in purchases],
                     reason=reason, price=sale_price, unit_cost=unit_cost,
                     purchase_cash=purchase_cash, budget=budget)
    return intents
