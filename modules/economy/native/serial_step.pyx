# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Последовательные native-фазы V3 над одним изменяемым flat state."""


def weighted_sum(double[::1] values, double[::1] weights):
    """Проверить один вызов с двумя непрерывными массивами float64."""
    cdef Py_ssize_t i, size = values.shape[0]
    cdef double total = 0.0
    if size != weights.shape[0]:
        raise ValueError('Длины массивов должны совпадать')
    for i in range(size):
        total += values[i]*weights[i]
    return total


cdef int _decimal_digits(int value) noexcept nogil:
    cdef int result = 1
    while value >= 10:
        value //= 10
        result += 1
    return result


cdef bint _order_id_less(int left, int right) noexcept nogil:
    """Сравнение числовых suffix при одинаковом month-prefix заявки."""
    cdef int left_digits = _decimal_digits(left)
    cdef int right_digits = _decimal_digits(right)
    if left_digits != right_digits:
        return left_digits < right_digits
    return left < right


cdef double _household_ceiling(
        int good, double quantity, int buyer_tile, int state, int province,
        int sell_count, int[::1] sell_good, int[::1] sell_account,
        double[::1] sell_quantity, double[::1] sell_price, int[::1] sell_order_index,
        int[::1] account_tile, double[:, ::1] distances, double[::1] good_weight,
        double transport_rate, int[::1] market_state, int[::1] market_province,
        int[::1] market_good, double[::1] market_volume,
        double[::1] market_landed, int history_len, double default_price,
        int[::1] quote_count, int[:, ::1] quote_enterprise,
        double[:, ::1] quote_price, int[::1] enterprise_tile,
        int[::1] offer_index, double[::1] offer_landed, double eps) noexcept nogil:
    """Тот же quoted_cost ceiling: offers, market signal и recovery fallback."""
    cdef int i, j, position, count = 0
    cdef double distance, landed, left = quantity, q, ceiling = 0.0
    cdef double reference, minimum_quote
    cdef double volume_hi = 0.0, volume_lo = 0.0
    cdef double landed_hi = 0.0, landed_lo = 0.0, item, next_hi
    for i in range(sell_count):
        if sell_good[i] != good:
            continue
        distance = distances[account_tile[sell_account[i]], buyer_tile]
        if distance != distance:
            continue
        landed = sell_price[i]+distance*good_weight[good]*transport_rate
        position = count
        while position > 0:
            j = offer_index[position-1]
            if offer_landed[position-1] < landed or (offer_landed[position-1] == landed
                    and not _order_id_less(sell_order_index[i], sell_order_index[j])):
                break
            offer_index[position] = j
            offer_landed[position] = offer_landed[position-1]
            position -= 1
        offer_index[position] = i
        offer_landed[position] = landed
        count += 1
    for i in range(count):
        q = min(left, sell_quantity[offer_index[i]])
        ceiling = max(ceiling, offer_landed[i])
        left -= q
        if left <= eps:
            break
    if left > eps:
        if count:
            reference = offer_landed[0]
        else:
            for i in range(market_good.shape[0]):
                if (market_good[i] != good or market_state[i] != state
                        or market_province[i] != province):
                    continue
                item = market_volume[i]
                next_hi = volume_hi+item
                if abs(volume_hi) >= abs(item):
                    volume_lo += (volume_hi-next_hi)+item
                else:
                    volume_lo += (item-next_hi)+volume_hi
                volume_hi = next_hi
                item = market_landed[i]
                next_hi = landed_hi+item
                if abs(landed_hi) >= abs(item):
                    landed_lo += (landed_hi-next_hi)+item
                else:
                    landed_lo += (item-next_hi)+landed_hi
                landed_hi = next_hi
            reference = ((landed_hi+landed_lo)/(volume_hi+volume_lo)
                         if volume_hi+volume_lo != 0 else default_price)
        minimum_quote = 0.0
        j = 0
        for i in range(quote_count[good]):
            distance = distances[enterprise_tile[quote_enterprise[good, i]], buyer_tile]
            if distance != distance:
                continue
            landed = quote_price[good, i]+distance*good_weight[good]*transport_rate
            if j == 0 or landed < minimum_quote:
                minimum_quote = landed
            j += 1
        if j:
            reference = max(reference, minimum_quote)
        ceiling = max(ceiling, reference)
    return max(ceiling, eps)


def update_prices(dict data):
    """Последовательный V3 update_prices над плоским числовым состоянием."""
    cdef int[::1] subject_enterprise = data['subject_enterprise']
    cdef int[::1] subject_account = data['subject_account']
    cdef int[::1] subject_good = data['subject_good']
    cdef int[::1] subject_tile = data['subject_tile']
    cdef int[::1] subject_state = data['subject_state']
    cdef int[::1] subject_province = data['subject_province']
    cdef double[::1] subject_price = data['subject_price']
    cdef double[::1] subject_last = data['subject_last_sold']
    cdef double[::1] subject_previous = data['subject_previous_sold']
    cdef int[::1] subject_history_count = data['subject_history_count']
    cdef unsigned char[::1] subject_is_stock = data['subject_is_stock']
    cdef double[::1] new_prices = data['new_prices']
    cdef int[::1] trace_branch = data['trace_branch']
    cdef double[::1] trace_limits = data['trace_limits']
    cdef double[::1] trace_competitor = data['trace_competitor']
    cdef double[::1] trace_budget = data['trace_budget']
    cdef double[::1] trace_signal = data['trace_signal_volume']

    cdef int[::1] enterprise_good = data['enterprise_good']
    cdef int[::1] enterprise_tile = data['enterprise_tile']
    cdef double[::1] enterprise_price = data['enterprise_price']
    cdef double[:, ::1] enterprise_stock = data['enterprise_stock']
    cdef int[::1] order_good = data['order_good']
    cdef int[::1] order_tile = data['order_tile']
    cdef int[::1] order_participant = data['order_participant']
    cdef double[::1] order_price = data['order_price']
    cdef double[::1] order_remaining = data['order_remaining']
    cdef unsigned char[::1] order_active = data['order_active']
    cdef int[::1] feedback_good = data['feedback_good']
    cdef int[::1] feedback_tile = data['feedback_tile']
    cdef double[::1] feedback_budget = data['feedback_budget']
    cdef int[::1] market_state = data['market_state']
    cdef int[::1] market_province = data['market_province']
    cdef int[::1] market_good = data['market_good']
    cdef double[::1] market_volume = data['market_volume']
    cdef double[:, ::1] distances = data['distances']
    cdef double[::1] good_weight = data['good_weight']

    cdef double transport_rate = data['transport_rate']
    cdef double price_step = data['price_step']
    cdef double minimum_price = data['minimum_price']
    cdef double maximum_price = data['maximum_price']
    cdef double eps = data['eps']
    cdef int history_len = data['market_history_len']
    cdef Py_ssize_t subject_count = subject_enterprise.shape[0]
    cdef Py_ssize_t enterprise_count = enterprise_good.shape[0]
    cdef Py_ssize_t order_count = order_good.shape[0]
    cdef Py_ssize_t feedback_count = feedback_good.shape[0]
    cdef Py_ssize_t market_count = market_good.shape[0]
    cdef Py_ssize_t s, i, e
    cdef int good, tile, account, competitor_count, feedback_found
    cdef int branch
    cdef double price, old_price, freight, value, distance
    cdef double maximum_limit, minimum_competitor, maximum_budget
    cdef double volume_hi, volume_lo, item, next_hi
    cdef bint limits_found

    with nogil:
        for s in range(subject_count):
            old_price = subject_price[s]
            price = old_price
            new_prices[s] = price
            if subject_history_count[s] == 0:
                continue
            good = subject_good[s]
            tile = subject_tile[s]
            account = subject_account[s]
            limits_found = False
            maximum_limit = 0.0
            for i in range(order_count):
                if (order_good[i] != good or not order_active[i]
                        or order_participant[i] == account or order_remaining[i] <= eps):
                    continue
                distance = distances[tile, order_tile[i]]
                if distance != distance:
                    continue
                freight = distance*good_weight[good]*transport_rate
                value = order_price[i]-freight
                if value > 0:
                    if not limits_found or value > maximum_limit:
                        maximum_limit = value
                    limits_found = True
            if limits_found:
                trace_limits[s] = maximum_limit
            branch = 0
            if limits_found and subject_last[s] > eps and maximum_limit > price+eps:
                price = min(price*(1+price_step), maximum_limit)
                branch = 1
            elif limits_found and maximum_limit < price-eps:
                price = max(price*(1-price_step), maximum_limit)
                branch = 2
            else:
                competitor_count = 0
                minimum_competitor = 0.0
                if subject_last[s] <= eps:
                    for e in range(enterprise_count):
                        if (enterprise_good[e] != good or e == subject_enterprise[s]
                                or enterprise_stock[e, good] <= eps):
                            continue
                        distance = distances[enterprise_tile[e], tile]
                        if distance != distance:
                            continue
                        freight = distance*good_weight[good]*transport_rate
                        value = enterprise_price[e]+freight
                        if competitor_count == 0 or value < minimum_competitor:
                            minimum_competitor = value
                        competitor_count += 1
                    if competitor_count:
                        trace_competitor[s] = minimum_competitor
                    if competitor_count and minimum_competitor < price-eps:
                        price = max(price*(1-price_step), minimum_competitor)
                        branch = 3
                    elif competitor_count and minimum_competitor <= price+eps:
                        branch = 5
                        volume_hi = 0.0
                        volume_lo = 0.0
                        for i in range(market_count):
                            if (market_good[i] != good or market_state[i] != subject_state[s]
                                    or market_province[i] != subject_province[s]):
                                continue
                            item = market_volume[i]
                            next_hi = volume_hi+item
                            if (volume_hi if volume_hi >= 0 else -volume_hi) >= (item if item >= 0 else -item):
                                volume_lo += (volume_hi-next_hi)+item
                            else:
                                volume_lo += (item-next_hi)+volume_hi
                            volume_hi = next_hi
                        if history_len:
                            trace_signal[s] = (volume_hi+volume_lo)/history_len
                        if trace_signal[s] > eps:
                            price *= 1-price_step
                            branch = 4
                if branch == 0:
                    feedback_found = False
                    maximum_budget = 0.0
                    for i in range(feedback_count):
                        if feedback_good[i] != good:
                            continue
                        distance = distances[tile, feedback_tile[i]]
                        if distance != distance:
                            continue
                        freight = distance*good_weight[good]*transport_rate
                        value = feedback_budget[i]-freight
                        if not feedback_found or value > maximum_budget:
                            maximum_budget = value
                        feedback_found = True
                    if feedback_found:
                        trace_budget[s] = maximum_budget
                    if (feedback_found and maximum_budget > 0 and maximum_budget < price-eps
                            and ((subject_history_count[s] > 1
                                  and subject_last[s]+eps < subject_previous[s])
                                 or subject_last[s] <= eps)):
                        price = max(price*(1-price_step), maximum_budget)
                        branch = 6
            price = min(maximum_price, max(minimum_price, price))
            new_prices[s] = price
            trace_branch[s] = branch
            if not subject_is_stock[s]:
                enterprise_price[subject_enterprise[s]] = price
    return subject_count


def prepare_recovery_quotes(dict data):
    """Построить V3 recovery-котировки снизу вверх после новых цен."""
    cdef int[::1] recovery_order = data['recovery_order']
    cdef double[::1] production_price = data['recovery_production_price']
    cdef int[::1] quote_count = data['recovery_quote_count']
    cdef int[:, ::1] quote_enterprise = data['recovery_quote_enterprise']
    cdef double[:, ::1] quote_price = data['recovery_quote_price']
    cdef int[::1] enterprise_good = data['enterprise_good']
    cdef int[::1] enterprise_tile = data['enterprise_tile']
    cdef double[::1] enterprise_price = data['enterprise_price']
    cdef unsigned char[::1] enterprise_suspended = data['enterprise_suspended']
    cdef unsigned char[::1] enterprise_resource_ok = data['enterprise_resource_ok']
    cdef int[::1] input_offset = data['recovery_input_offset']
    cdef int[::1] input_goods = data['recovery_input_good']
    cdef double[::1] input_values = data['recovery_input_value']
    cdef int[::1] wear_offset = data['recovery_wear_offset']
    cdef int[::1] wear_goods = data['recovery_wear_good']
    cdef double[::1] wear_values = data['recovery_wear_value']
    cdef double[:, ::1] distances = data['distances']
    cdef double[::1] good_weight = data['good_weight']
    cdef double[::1] good_default_price = data['good_default_price']
    cdef double[::1] good_labor = data['good_labor']
    cdef unsigned char[::1] good_consumer = data['good_consumer']
    cdef double transport_rate = data['transport_rate']
    cdef double wage = data['wage']
    cdef double markup = data['markup']
    cdef double consumption_tax = data['consumption_tax']
    cdef double maximum_price = data['maximum_price']
    cdef double eps = data['eps']
    cdef Py_ssize_t order_size = recovery_order.shape[0]
    cdef Py_ssize_t i, j, k, q
    cdef Py_ssize_t enterprise, good, input_good, tile, count
    cdef double cost, ratio, net, price, quote, distance, freight
    cdef bint found

    with nogil:
        for i in range(order_size):
            enterprise = recovery_order[i]
            good = enterprise_good[enterprise]
            if enterprise_suspended[enterprise] or not enterprise_resource_ok[enterprise]:
                continue
            cost = good_labor[good]*wage
            for k in range(input_offset[good], input_offset[good+1]):
                input_good = input_goods[k]
                ratio = input_values[k]
                found = False
                quote = 0.0
                for q in range(quote_count[input_good]):
                    j = quote_enterprise[input_good, q]
                    distance = distances[enterprise_tile[j], enterprise_tile[enterprise]]
                    if distance != distance:
                        continue
                    freight = distance*good_weight[input_good]*transport_rate
                    price = quote_price[input_good, q]+freight
                    if not found or price < quote:
                        quote = price
                    found = True
                if not found:
                    quote = good_default_price[input_good]
                cost += ratio*quote
            for k in range(wear_offset[good], wear_offset[good+1]):
                input_good = wear_goods[k]
                ratio = wear_values[k]
                found = False
                quote = 0.0
                for q in range(quote_count[input_good]):
                    j = quote_enterprise[input_good, q]
                    distance = distances[enterprise_tile[j], enterprise_tile[enterprise]]
                    if distance != distance:
                        continue
                    freight = distance*good_weight[input_good]*transport_rate
                    price = quote_price[input_good, q]+freight
                    if not found or price < quote:
                        quote = price
                    found = True
                if not found:
                    quote = good_default_price[input_good]
                cost += ratio*quote
            net = 1-consumption_tax if good_consumer[good] else 1
            price = (cost*(1+markup)/net
                     if enterprise_price[enterprise]*net < cost-eps
                     else enterprise_price[enterprise])
            production_price[enterprise] = min(maximum_price, price)
            count = quote_count[good]
            quote_enterprise[good, count] = enterprise
            quote_price[good, count] = production_price[enterprise]
            quote_count[good] = count+1
    return order_size


def preparation(dict data):
    """Один вход: цены и recovery видят одно последовательное состояние."""
    update_prices(data)
    prepare_recovery_quotes(data)


def sell_orders(dict data, object rng):
    """Создать sell intents после исходного shuffle канонических account ID."""
    cdef int[::1] seller_order = data['seller_order']
    cdef int[::1] account_enterprise = data['account_enterprise']
    cdef unsigned char[::1] account_household = data['account_household']
    cdef double[:, ::1] account_stock = data['account_stock']
    cdef double[:, ::1] account_reserved = data['account_stock_reserved']
    cdef double[:, ::1] account_locked = data['account_locked']
    cdef int[::1] enterprise_good = data['enterprise_good']
    cdef unsigned char[::1] enterprise_suspended = data['enterprise_suspended']
    cdef unsigned char[::1] enterprise_closed = data['enterprise_closed']
    cdef double[::1] enterprise_capacity = data['enterprise_capacity']
    cdef double[::1] enterprise_planned = data['enterprise_planned']
    cdef double[::1] enterprise_recent = data['enterprise_recent']
    cdef double[::1] enterprise_price = data['enterprise_price']
    cdef int[::1] sell_offset = data['sell_offset']
    cdef int[::1] sell_good = data['sell_good']
    cdef double[::1] sell_ask_price = data['sell_ask_price']
    cdef int[::1] sell_subject = data['sell_subject']
    cdef double[::1] new_prices = data['new_prices']
    cdef double[::1] sell_input_ratio = data['sell_input_ratio']
    cdef double[::1] sell_equipment_ratio = data['sell_equipment_ratio']
    cdef int[::1] output_account = data['sell_order_account']
    cdef int[::1] output_entry = data['sell_order_entry']
    cdef int[::1] output_good = data['sell_order_good']
    cdef double[::1] output_quantity = data['sell_order_quantity']
    cdef double[::1] output_price = data['sell_order_price']
    cdef int[::1] output_index = data['sell_order_index']
    cdef int[::1] output_seq = data['sell_execution_seq']
    cdef double input_buffer = data['input_buffer']
    cdef double eps = data['eps']
    cdef int initial_order_count = data['initial_order_count']
    cdef Py_ssize_t account_count = seller_order.shape[0]
    cdef Py_ssize_t i, k, account, enterprise, good
    cdef Py_ssize_t count = 0
    cdef double quantity, free, reserve, deployed, price
    cdef list shuffled = [seller_order[i] for i in range(account_count)]

    # Единственный RNG-вызов остаётся в исходном месте, после invalidation и до
    # расчёта intents. Дальше работают только числовые массивы без GIL.
    rng.shuffle(shuffled)
    for i in range(account_count):
        seller_order[i] = shuffled[i]
    with nogil:
        for i in range(account_count):
            account = seller_order[i]
            enterprise = account_enterprise[account]
            if account_household[account] or (enterprise >= 0 and enterprise_suspended[enterprise]):
                continue
            for k in range(sell_offset[account], sell_offset[account+1]):
                good = sell_good[k]
                free = account_stock[account, good]-account_reserved[account, good] \
                    -account_locked[account, good]
                if free < 0:
                    free = 0
                quantity = free
                if enterprise >= 0 and not enterprise_closed[enterprise] and good != enterprise_good[enterprise]:
                    deployed = min(enterprise_capacity[enterprise],
                                   max(enterprise_planned[enterprise], enterprise_recent[enterprise]))
                    reserve = deployed*input_buffer
                    quantity = max(0, quantity-reserve*sell_input_ratio[k])
                    deployed = min(enterprise_capacity[enterprise],
                                   max(enterprise_planned[enterprise], enterprise_recent[enterprise]))
                    quantity = max(0, quantity-sell_equipment_ratio[k]*deployed)
                price = (enterprise_price[enterprise]
                         if enterprise >= 0 and good == enterprise_good[enterprise]
                         else sell_ask_price[k])
                if sell_subject[k] >= 0:
                    price = new_prices[sell_subject[k]]
                    sell_ask_price[k] = price
                quantity = min(quantity, free)
                if quantity <= eps:
                    continue
                account_reserved[account, good] += quantity
                output_account[count] = account
                output_entry[count] = k
                output_good[count] = good
                output_quantity[count] = quantity
                output_price[count] = price
                output_index[count] = initial_order_count+count
                output_seq[count] = count
                count += 1
    data['sell_order_count'] = count
    return count


def household_requests(dict data, object rng):
    """Basic для всех households, затем extra для всех в исходном RNG-порядке."""
    cdef int[::1] households = data['household_order']
    cdef double[::1] population = data['account_population']
    cdef double[:, ::1] stock = data['account_stock']
    cdef double[::1] balance = data['account_balance']
    cdef double[::1] reserved = data['account_money_reserved']
    cdef double[::1] protected = data['account_protected']
    cdef int[::1] basic_good = data['household_basic_good']
    cdef double[::1] basic_rate = data['household_basic_rate']
    cdef double[::1] basic_rate_by_good = data['household_basic_rate_by_good']
    cdef int[::1] extra_good = data['household_extra_good']
    cdef double[::1] extra_rate = data['household_extra_rate']
    cdef double[:, ::1] ceilings = data['household_quote_ceiling']
    cdef int[::1] sort_good = data['household_sort_good']
    cdef int[::1] output_account = data['household_request_account']
    cdef int[::1] output_good = data['household_request_good']
    cdef double[::1] output_needed = data['household_request_needed']
    cdef double[::1] output_missing = data['household_request_missing']
    cdef double[::1] output_price = data['household_request_price']
    cdef double[::1] output_fraction = data['household_request_fraction']
    cdef unsigned char[::1] output_basic = data['household_request_basic']
    cdef double[::1] output_order_quantity = data['household_order_quantity']
    cdef double[::1] output_order_reserved = data['household_order_reserved']
    cdef double[::1] output_budget_price = data['household_budget_price']
    cdef unsigned char[::1] output_unfunded = data['household_unfunded']
    cdef int[::1] output_order_index = data['household_order_index']
    cdef int[::1] output_seq = data['household_execution_seq']
    cdef int[::1] sell_good = data['sell_order_good']
    cdef int[::1] sell_account = data['sell_order_account']
    cdef double[::1] sell_quantity = data['sell_order_quantity']
    cdef double[::1] sell_price = data['sell_order_price']
    cdef int[::1] sell_order_index = data['sell_order_index']
    cdef int[::1] account_tile = data['account_tile']
    cdef int[::1] account_state = data['account_state']
    cdef int[::1] account_province = data['account_province']
    cdef double[:, ::1] distances = data['distances']
    cdef double[::1] good_weight = data['good_weight']
    cdef int[::1] market_state = data['market_state']
    cdef int[::1] market_province = data['market_province']
    cdef int[::1] market_good = data['market_good']
    cdef double[::1] market_volume = data['market_volume']
    cdef double[::1] market_landed = data['market_landed_component']
    cdef double[::1] default_price = data['good_default_price']
    cdef int[::1] quote_count = data['recovery_quote_count']
    cdef int[:, ::1] quote_enterprise = data['recovery_quote_enterprise']
    cdef double[:, ::1] quote_price = data['recovery_quote_price']
    cdef int[::1] enterprise_tile = data['enterprise_tile']
    cdef int[::1] offer_index = data['household_offer_index']
    cdef double[::1] offer_landed = data['household_offer_landed']
    cdef double transport_rate = data['transport_rate']
    cdef double eps = data['eps']
    cdef int history_len = data['market_history_len']
    cdef int sell_count = data['sell_order_count']
    cdef int initial_order_count = data['initial_order_count']
    cdef bint budget_revision = data['basic_budget_revision']
    cdef Py_ssize_t household_count = households.shape[0]
    cdef Py_ssize_t basic_count = basic_good.shape[0]
    cdef Py_ssize_t extra_count = extra_good.shape[0]
    cdef Py_ssize_t h, i, account, good, position
    cdef Py_ssize_t request_count = 0, order_count = 0
    cdef double needed, missing, price, fraction, quantity, free_cash
    cdef double total_hi, total_lo, item, next_hi, key
    cdef list shuffled = [households[i] for i in range(household_count)]
    rng.shuffle(shuffled)
    for i in range(household_count):
        households[i] = shuffled[i]

    with nogil:
        # Первый проход: только базовые нужды всех households.
        for h in range(household_count):
            account = households[h]
            for i in range(basic_count):
                good = basic_good[i]
                needed = population[account]*basic_rate[i]
                missing = max(0, needed-stock[account, good])
                ceilings[account, good] = _household_ceiling(good, missing,
                    account_tile[account], account_state[account], account_province[account],
                    sell_count, sell_good, sell_account, sell_quantity, sell_price,
                    sell_order_index, account_tile, distances, good_weight, transport_rate,
                    market_state, market_province, market_good, market_volume, market_landed,
                    history_len, default_price[good], quote_count, quote_enterprise,
                    quote_price, enterprise_tile, offer_index, offer_landed, eps)
                sort_good[i] = good
            if budget_revision:
                for i in range(1, basic_count):
                    good = sort_good[i]
                    key = population[account]*basic_rate_by_good[good]*ceilings[account, good]
                    position = i
                    while position > 0:
                        j = sort_good[position-1]
                        if population[account]*basic_rate_by_good[j]*ceilings[account, j] <= key:
                            break
                        sort_good[position] = j
                        position -= 1
                    sort_good[position] = good
                # Каждый singleton pass имеет собственный fraction и свободную кассу.
                for i in range(basic_count):
                    good = sort_good[i]
                    needed = population[account]*basic_rate_by_good[good]
                    missing = max(0, needed-stock[account, good])
                    price = ceilings[account, good]
                    item = missing*price
                    free_cash = max(0.0, balance[account]-reserved[account]-protected[account])
                    fraction = min(1, free_cash/item) if item > eps else 0
                    output_account[request_count] = account
                    output_good[request_count] = good
                    output_needed[request_count] = needed
                    output_missing[request_count] = missing
                    output_price[request_count] = price
                    output_fraction[request_count] = fraction
                    output_budget_price[request_count] = price*fraction
                    output_unfunded[request_count] = fraction < 1-eps
                    output_basic[request_count] = True
                    quantity = min(missing*fraction, free_cash/price)
                    if quantity > eps:
                        reserved[account] += quantity*price
                        output_order_quantity[request_count] = quantity
                        output_order_reserved[request_count] = quantity*price
                        output_order_index[request_count] = initial_order_count+sell_count+order_count
                        output_seq[request_count] = sell_count+order_count
                        order_count += 1
                    request_count += 1
            else:
                total_hi = 0.0
                total_lo = 0.0
                for i in range(basic_count):
                    item = max(0, population[account]*basic_rate[i]-stock[account, basic_good[i]]) \
                        *ceilings[account, basic_good[i]]
                    next_hi = total_hi+item
                    if abs(total_hi) >= abs(item):
                        total_lo += (total_hi-next_hi)+item
                    else:
                        total_lo += (item-next_hi)+total_hi
                    total_hi = next_hi
                free_cash = max(0.0, balance[account]-reserved[account]-protected[account])
                fraction = min(1, free_cash/(total_hi+total_lo)) if total_hi+total_lo > eps else 0
                for i in range(basic_count):
                    good = basic_good[i]
                    needed = population[account]*basic_rate[i]
                    missing = max(0, needed-stock[account, good])
                    price = ceilings[account, good]
                    output_account[request_count] = account
                    output_good[request_count] = good
                    output_needed[request_count] = needed
                    output_missing[request_count] = missing
                    output_price[request_count] = price
                    output_fraction[request_count] = fraction
                    output_budget_price[request_count] = price*fraction
                    output_unfunded[request_count] = fraction < 1-eps
                    output_basic[request_count] = True
                    quantity = missing*fraction
                    free_cash = max(0.0, balance[account]-reserved[account]-protected[account])
                    quantity = min(quantity, free_cash/price)
                    if quantity > eps:
                        reserved[account] += quantity*price
                        output_order_quantity[request_count] = quantity
                        output_order_reserved[request_count] = quantity*price
                        output_order_index[request_count] = initial_order_count+sell_count+order_count
                        output_seq[request_count] = sell_count+order_count
                        order_count += 1
                    request_count += 1

        # Второй проход начинается только после всех базовых заявок.
        for h in range(household_count):
            account = households[h]
            total_hi = 0.0
            total_lo = 0.0
            for i in range(extra_count):
                good = extra_good[i]
                needed = population[account]*extra_rate[i]
                missing = max(0, needed-stock[account, good])
                price = _household_ceiling(good, missing, account_tile[account],
                    account_state[account], account_province[account], sell_count,
                    sell_good, sell_account, sell_quantity, sell_price, sell_order_index,
                    account_tile, distances, good_weight, transport_rate, market_state,
                    market_province, market_good, market_volume, market_landed, history_len,
                    default_price[good], quote_count, quote_enterprise, quote_price,
                    enterprise_tile, offer_index, offer_landed, eps)
                ceilings[account, good] = price
                item = missing*price
                next_hi = total_hi+item
                if abs(total_hi) >= abs(item):
                    total_lo += (total_hi-next_hi)+item
                else:
                    total_lo += (item-next_hi)+total_hi
                total_hi = next_hi
            free_cash = max(0.0, balance[account]-reserved[account]-protected[account])
            fraction = min(1, free_cash/(total_hi+total_lo)) if total_hi+total_lo > eps else 0
            for i in range(extra_count):
                good = extra_good[i]
                needed = population[account]*extra_rate[i]
                missing = max(0, needed-stock[account, good])
                price = ceilings[account, good]
                output_account[request_count] = account
                output_good[request_count] = good
                output_needed[request_count] = needed
                output_missing[request_count] = missing
                output_price[request_count] = price
                output_fraction[request_count] = fraction
                output_budget_price[request_count] = price*fraction
                output_unfunded[request_count] = fraction < 1-eps
                output_basic[request_count] = False
                quantity = missing*fraction
                free_cash = max(0.0, balance[account]-reserved[account]-protected[account])
                quantity = min(quantity, free_cash/price)
                if quantity > eps:
                    reserved[account] += quantity*price
                    output_order_quantity[request_count] = quantity
                    output_order_reserved[request_count] = quantity*price
                    output_order_index[request_count] = initial_order_count+sell_count+order_count
                    output_seq[request_count] = sell_count+order_count
                    order_count += 1
                request_count += 1
    data['household_request_count'] = request_count
    return request_count

def market_preparation(dict data, object rng):
    """Цены, recovery и sell orders видят одно последовательное состояние."""
    update_prices(data)
    prepare_recovery_quotes(data)
    sell_orders(data, rng)


def household_preparation(dict data, object rng):
    """Пять последовательных фаз до owner_requests в одном native state."""
    update_prices(data)
    prepare_recovery_quotes(data)
    sell_orders(data, rng)
    household_requests(data, rng)
