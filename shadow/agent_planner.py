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
    """Сколько своего товара агент продаст: публичный спрос минус конкуренты."""
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
    """Предельная (а не средняя) цена покрытия объёма по книге.

    Заявка с пределом по средней цене не может докупить следующий лот: средняя
    по 40,8 ед. по 1,0479 и 8,4 ед. по 1,0508 равна 1,0484, а следующий лот
    стоит 1,0508 и не проходит. Поэтому предел должен равняться доставленной
    цене последней нужной единицы — это и есть предельная цена спроса.
    Ограничение по марже накладывается отдельно.

    Не хватило книги — остаток оценивается по ориентиру следующего выпуска.
    """
    if quantity <= EPS:
        return reference_price(book, tile, good, rate)
    weight = book['good_weight'][good]
    distance_row = book['distance'][tile]
    ptr = book['offer_ptr']
    # Офферы одного товара упорядочены по площадкам, а не по landed-цене:
    # для предельной цены нужно отсортировать по доставленной цене этот товар.
    rows = []
    for index in range(int(ptr[good]), int(ptr[good+1])):
        if book['offer_remaining'][index] <= EPS:
            continue
        if not book['reachable'][book['offer_tile'][index], tile]:
            continue
        landed = _landed(distance_row, book['offer_tile'][index],
                         book['offer_price'][index], weight, rate)
        rows.append((landed, float(book['offer_remaining'][index])))
    if not rows:
        return reference_price(book, tile, good, rate)
    rows.sort()
    left, marginal = quantity, None
    for landed, remaining in rows:
        take = min(left, remaining)
        if take > EPS:
            marginal = landed
        left -= take
        if left <= EPS:
            break
    if left > EPS:
        # Книги не хватило: последняя докупаемая единица — по ориентиру выпуска.
        marginal = max(marginal or 0.0, reference_price(book, tile, good, rate))
    return marginal if marginal is not None else reference_price(book, tile, good, rate)


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
    # Предел заявки — цена, при которой маржа ещё не отрицательна, но не выше
    # средней цены покрытия нужного объёма. Первое защищает маржу, второе
    # позволяет докупить второй оффер, когда первый выкуплен.
    bids = {}
    for g, ratio in requirements.items():
        other_cost = unit_cost-costs.get(g, 0.0)*quotes[g]+fixed_unit
        marginal_ratio = costs.get(g, ratio)
        ceiling = max(0.0, (sale_price*net-other_cost)/max(marginal_ratio, EPS))
        if desired > EPS:
            needed = max(0.0, desired*ratio*(1+INPUT_BUFFER)-float(stock[g]))
            market = volume_quote(book, tile, g, needed, rate) if needed > EPS else quotes[g]
        else:
            market = quotes[g]
        bids[g] = min(max(quotes[g], market), ceiling) if ceiling > EPS else quotes[g]

    def purchases_for(output):
        if output <= EPS:
            return []
        return [(g, max(0.0, output*r*(1+INPUT_BUFFER)-float(stock[g])), bids[g])
                for g, r in requirements.items() if bids[g] > EPS]

    def cash_needed(output):
        return overhead+output*wage_unit+sum(q*p for _, q, p in purchases_for(output))

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
    # План публикуется после бюджетного ограничения даже при пустом складе.
    intents = [('sell', good, own_stock, sale_price, 'Продажа запасов', target, reason)]
    purchases = purchases_for(target)
    intents.extend(('buy', g, q, bid, 'Сырьё', target, reason)
                   for g, q, bid in purchases if q > EPS)
    if trace is not None:
        trace.update(observed=observed, forecast=forecast, stock=own_stock,
                     history_count=int(book['agent_history_count'][agent]),
                     prior=float(book['agent_forecast'][agent]),
                     public=float(book['public_share'][good]),
                     projected_stock=projected_stock, desired=desired, target=target,
                     wage_reserve=target*wage_unit, overhead=overhead,
                     purchases=purchases, reason=reason, price=sale_price,
                     unit_cost=unit_cost)
    return intents
