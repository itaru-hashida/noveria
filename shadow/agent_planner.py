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
OFFER_SCAN = 64


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
        return best
    return float(book['good_price'][good])


def reachable_demand(book, tile, good, price, rate):
    """Сколько своего товара агент продаст: публичный спрос минус конкуренты."""
    weight = book['good_weight'][good]
    distance_row = book['distance'][tile]
    ask_ptr, offer_ptr = book['ask_ptr'], book['offer_ptr']
    total = 0.0
    for ask_index in range(int(ask_ptr[good]), int(ask_ptr[good+1])):
        ask_tile = book['ask_tile'][ask_index]
        if ask_tile == tile:
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
    return total


def plan(book, agent, rate, wage, tax, markup, overhead_rate):
    """Намерения агента: продать выпуск, купить входы. Без общего состояния.

    Порядок как в V3: прогноз спроса и цена → план выпуска → потребность в
    сырье, урезанная бюджетом → заявки с предельными ценами.

    Предельная цена закупки (замена `adaptive_input_budget`): агент платит не
    больше, чем оставляет маржа. Верхняя граница — цена, при которой
    `sale_price*net - остальные издержки - fixed` обнуляется. Это защищает
    зарплату и постоянные издержки: закупка не может съесть их.
    """
    good = int(book['agent_good'][agent])
    tile = int(book['agent_tile'][agent])
    capacity = float(book['agent_capacity'][agent])
    price = float(book['agent_price'][agent])
    stock = book['agent_stock'][agent]
    budget = float(book['agent_budget'][agent])
    consumer = float(book['good_taxed'][good])

    inputs = [(int(column), float(book['good_inputs'][good, column]))
              for column in np.nonzero(book['good_inputs'][good])[0]]
    equipment = [(int(column), float(book['good_equip'][good, column]))
                 for column in np.nonzero(book['good_equip'][good])[0]]

    unit_cost = float(book['good_labor'][good])*wage
    for input_good, ratio in inputs:
        unit_cost += ratio*reference_price(book, tile, input_good, rate)
    for input_good, ratio in equipment:
        unit_cost += ratio*reference_price(book, tile, input_good, rate)

    net = 1.0-tax*consumer
    sale_price = price if price*net+EPS >= unit_cost else unit_cost*(1+markup)/max(net, EPS)

    demand = reachable_demand(book, tile, good, sale_price, rate)
    own_stock = float(stock[good])
    # Склад продаётся в этом месяце, новый выпуск — со следующего.
    target = max(0.0, min(capacity, demand-own_stock))
    overhead = capacity*float(book['good_labor'][good])*wage*overhead_rate
    fixed_unit = overhead/target if target > EPS else 0.0
    margin = sale_price*net-unit_cost-fixed_unit
    reason = 'План'
    if target <= EPS:
        reason = 'Нет обеспеченного спроса'
    elif margin < -EPS:
        target, reason = 0.0, 'Отрицательная ожидаемая маржа'

    intents = []
    sell_quantity = own_stock+target if target > EPS else own_stock
    if sell_quantity > EPS:
        intents.append(('sell', good, sell_quantity, sale_price,
                        'Продажа запасов', target, reason))
    if target <= EPS:
        return intents

    # Предельная цена каждого входа: сколько остаётся на него после маржи и
    # остальных издержек. Именно это защищает зарплату и оброк.
    wage_unit = float(book['good_labor'][good])*wage
    purchases, needed = [], overhead
    for input_good, ratio in inputs+equipment:
        missing = max(0.0, target*ratio-float(stock[input_good]))
        if missing <= EPS:
            continue
        quote = reference_price(book, tile, input_good, rate)
        other_cost = wage_unit+fixed_unit
        for other_good, other_ratio in inputs+equipment:
            if other_good == input_good:
                continue
            other_cost += other_ratio*reference_price(book, tile, other_good, rate)
        ceiling = max(0.0, (sale_price*net-other_cost)/max(ratio, EPS))
        # Не переплачиваем рынку: цена заявки — минимум из котировки и предела.
        bid = min(quote, ceiling) if ceiling > EPS else quote
        purchases.append((input_good, missing, bid, quote))
        needed += missing*quote
    if needed > budget+EPS and needed > EPS:
        # Бюджет режет объём, а не цену: предельная цена уже защищает маржу.
        scale = max(0.0, budget)/needed
        target *= scale
        purchases = [(g, quantity*scale, bid, quote)
                     for g, quantity, bid, quote in purchases]
        reason = 'Деньги на выпуск'
    for input_good, quantity, bid, quote in purchases:
        if quantity <= EPS:
            continue
        intents.append(('buy', input_good, quantity, bid, 'Сырьё', target, reason))
    return intents
