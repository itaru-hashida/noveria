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
    """Лексикографическое сравнение decimal suffix без создания строк."""
    cdef int left_digits = _decimal_digits(left)
    cdef int right_digits = _decimal_digits(right)
    cdef int left_divisor = 1, right_divisor = 1, i
    cdef int left_digit, right_digit
    for i in range(left_digits-1):
        left_divisor *= 10
    for i in range(right_digits-1):
        right_divisor *= 10
    while left_divisor and right_divisor:
        left_digit = left//left_divisor
        right_digit = right//right_divisor
        if left_digit != right_digit:
            return left_digit < right_digit
        left %= left_divisor
        right %= right_divisor
        left_divisor //= 10
        right_divisor //= 10
    return left_digits < right_digits


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
        int[::1] offer_index, double[::1] offer_landed, double eps,
        int buyer_enterprise, int buyer_account, unsigned char[::1] sell_active) noexcept nogil:
    """Тот же quoted_cost ceiling: offers, market signal и recovery fallback."""
    cdef int i, j, position, count = 0
    cdef double distance, landed, left = quantity, q, ceiling = 0.0
    cdef double reference, minimum_quote
    cdef double volume_hi = 0.0, volume_lo = 0.0
    cdef double landed_hi = 0.0, landed_lo = 0.0, item, next_hi
    for i in range(sell_count):
        if sell_good[i] != good or not sell_active[i] or sell_account[i] == buyer_account:
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
            if quote_enterprise[good, i] == buyer_enterprise:
                continue
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
    cdef double[::1] output_initial_quantity = data['sell_order_initial_quantity']
    cdef double[::1] output_remaining = data['sell_order_remaining']
    cdef double[::1] output_backing = data['sell_order_backing']
    cdef double[::1] output_price = data['sell_order_price']
    cdef int[::1] output_index = data['sell_order_index']
    cdef int[::1] output_seq = data['sell_execution_seq']
    cdef int[::1] market_version_work = data['recovery_market_version_work']
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
                output_initial_quantity[count] = quantity
                output_remaining[count] = quantity
                output_backing[count] = quantity
                output_price[count] = price
                output_index[count] = initial_order_count+count
                output_seq[count] = count
                market_version_work[good] += 1
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
    cdef unsigned char[::1] sell_active = data['sell_active']
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
    cdef int[::1] demand_good = data['demand_order_good']
    cdef int[::1] demand_participant = data['demand_order_participant']
    cdef int[::1] demand_tile = data['demand_order_tile']
    cdef double[::1] demand_price = data['demand_order_price']
    cdef double[::1] demand_remaining = data['demand_order_remaining']
    cdef double[::1] demand_reserved = data['demand_order_reserved']
    cdef unsigned char[::1] demand_active = data['demand_order_active']
    cdef unsigned char[::1] demand_final = data['demand_order_final']
    cdef unsigned char[::1] demand_construction = data['demand_order_construction']
    cdef int demand_count = data['demand_order_count']
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
                    quote_price, enterprise_tile, offer_index, offer_landed, eps, -1, account, sell_active)
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
                        demand_good[demand_count] = good
                        demand_participant[demand_count] = account
                        demand_tile[demand_count] = account_tile[account]
                        demand_price[demand_count] = price
                        demand_remaining[demand_count] = quantity
                        demand_reserved[demand_count] = quantity*price
                        demand_active[demand_count] = True
                        demand_final[demand_count] = True
                        demand_construction[demand_count] = False
                        demand_count += 1
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
                        demand_good[demand_count] = good
                        demand_participant[demand_count] = account
                        demand_tile[demand_count] = account_tile[account]
                        demand_price[demand_count] = price
                        demand_remaining[demand_count] = quantity
                        demand_reserved[demand_count] = quantity*price
                        demand_active[demand_count] = True
                        demand_final[demand_count] = True
                        demand_construction[demand_count] = False
                        demand_count += 1
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
                    enterprise_tile, offer_index, offer_landed, eps, -1, account, sell_active)
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
                    demand_good[demand_count] = good
                    demand_participant[demand_count] = account
                    demand_tile[demand_count] = account_tile[account]
                    demand_price[demand_count] = price
                    demand_remaining[demand_count] = quantity
                    demand_reserved[demand_count] = quantity*price
                    demand_active[demand_count] = True
                    demand_final[demand_count] = True
                    demand_construction[demand_count] = False
                    demand_count += 1
                    order_count += 1
                request_count += 1
    data['demand_order_count'] = demand_count
    data['household_request_count'] = request_count
    data['household_order_count_created'] = order_count
    return request_count

cdef double _market_reference(int good, int state, int province,
        int[::1] market_state, int[::1] market_province, int[::1] market_good,
        double[::1] market_volume, double[::1] market_landed,
        double default_price) noexcept nogil:
    cdef double volume_hi = 0.0, volume_lo = 0.0
    cdef double landed_hi = 0.0, landed_lo = 0.0, item, next_hi
    cdef int i
    for i in range(market_good.shape[0]):
        if market_good[i] != good or market_state[i] != state or market_province[i] != province:
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
    return ((landed_hi+landed_lo)/(volume_hi+volume_lo)
            if volume_hi+volume_lo != 0 else default_price)


def owner_requests(dict data, object rng):
    """Личное потребление владельцев в исходном shuffle-порядке."""
    cdef int[::1] owners = data['owner_order']
    cdef int[::1] basket = data['owner_basket_good']
    cdef double[::1] balance = data['account_balance']
    cdef double[::1] reserved = data['account_money_reserved']
    cdef double[::1] protected = data['account_protected']
    cdef double[::1] basic_rate = data['household_basic_rate_by_good']
    cdef double[:, ::1] stock = data['account_stock']
    cdef double[::1] project_capacity = data['project_capacity']
    cdef double[::1] project_working = data['project_working']
    cdef double[::1] project_delivery = data['project_delivery']
    cdef int[::1] project_tile = data['project_tile']
    cdef int[::1] construction_good = data['construction_good']
    cdef double[::1] construction_ratio = data['construction_ratio']
    cdef double[::1] construction_amount = data['construction_amount']
    cdef double minimum_capacity = data['minimum_capacity']
    cdef int[::1] demand_good = data['demand_order_good']
    cdef int[::1] demand_participant = data['demand_order_participant']
    cdef int[::1] demand_tile = data['demand_order_tile']
    cdef double[::1] demand_price = data['demand_order_price']
    cdef double[::1] demand_remaining = data['demand_order_remaining']
    cdef double[::1] demand_reserved = data['demand_order_reserved']
    cdef unsigned char[::1] demand_active = data['demand_order_active']
    cdef unsigned char[::1] demand_construction = data['demand_order_construction']
    cdef unsigned char[::1] demand_final = data['demand_order_final']
    cdef double[::1] sell_price = data['sell_order_price']
    cdef double[::1] sell_quantity = data['sell_order_quantity']
    cdef int[::1] sell_good = data['sell_order_good']
    cdef int[::1] sell_account = data['sell_order_account']
    cdef int[::1] sell_index = data['sell_order_index']
    cdef unsigned char[::1] sell_active = data['sell_active']
    cdef int[::1] account_tile = data['account_tile']
    cdef int[::1] account_state = data['account_state']
    cdef int[::1] account_province = data['account_province']
    cdef double[:, ::1] distances = data['distances']
    cdef double[::1] weight = data['good_weight']
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
    cdef double[::1] output_project = data['owner_project_reserve']
    cdef double[::1] output_free = data['owner_free_cash']
    cdef double[::1] output_budget = data['owner_budget']
    cdef int[::1] output_account = data['owner_account']
    cdef int[::1] output_good = data['owner_request_good']
    cdef double[::1] output_quantity = data['owner_order_quantity']
    cdef double[::1] output_reserved = data['owner_order_reserved']
    cdef double[::1] output_price = data['owner_order_price']
    cdef int[::1] output_index = data['owner_order_index']
    cdef int[::1] output_seq = data['owner_execution_seq']
    cdef double rate = data['owner_consumption_rate']
    cdef double family = data['owner_family_size']
    cdef double quality = data['owner_food_quality']
    cdef double eps = data['eps']
    cdef double transport_rate = data['transport_rate']
    cdef int history_len = data['market_history_len']
    cdef int sell_count = data['sell_order_count']
    cdef int next_order = data['initial_order_count']+sell_count+data['household_order_count_created']
    cdef int demand_count = data['demand_order_count']
    cdef int i, j, k, account, good, request = 0
    cdef double project, available, budget, share, reference, quantity, ceiling, distance, material_cost
    cdef double ordered, missing, item, next_hi, weight_hi, weight_lo
    cdef list shuffled = [owners[i] for i in range(owners.shape[0])]
    rng.shuffle(shuffled)
    for i in range(owners.shape[0]):
        owners[i] = shuffled[i]
    with nogil:
        for i in range(owners.shape[0]):
            account = owners[i]
            output_account[i] = account
            project = 0.0
            if project_tile[account] >= 0:
                distance = distances[account_tile[account], project_tile[account]]
                project = project_working[account]
                if distance == distance:
                    weight_hi = 0.0
                    weight_lo = 0.0
                    for j in range(construction_good.shape[0]):
                        item = construction_amount[j]*project_capacity[account]/minimum_capacity \
                            *weight[construction_good[j]]
                        next_hi = weight_hi+item
                        if abs(weight_hi) >= abs(item):
                            weight_lo += (weight_hi-next_hi)+item
                        else:
                            weight_lo += (item-next_hi)+weight_hi
                        weight_hi = next_hi
                    project += (weight_hi+weight_lo)*distance*transport_rate
                else:
                    project += project_delivery[account]
                for k in range(construction_good.shape[0]):
                    good = construction_good[k]
                    ordered = 0.0
                    for j in range(demand_good.shape[0]):
                        if (demand_participant[j] == account and demand_construction[j]
                                and demand_good[j] == good and demand_active[j]
                                and demand_reserved[j] > eps):
                            ordered += demand_remaining[j]
                    missing = max(0.0, construction_amount[k]*project_capacity[account]/minimum_capacity
                                  -stock[account, good]-ordered)
                    ceiling = _household_ceiling(good, missing, account_tile[account],
                        account_state[account], account_province[account], sell_count,
                        sell_good, sell_account, sell_quantity, sell_price, sell_index,
                        account_tile, distances, weight, transport_rate, market_state,
                        market_province, market_good, market_volume, market_landed, history_len,
                        default_price[good], quote_count, quote_enterprise, quote_price,
                        enterprise_tile, offer_index, offer_landed, eps, -1, account, sell_active)
                    # V3 сначала округляет произведение в Python float, затем
                    # прибавляет его к reserve. Явный temporary запрещает
                    # компилятору склеить операции в FMA.
                    material_cost = missing*ceiling
                    project = project+material_cost
            available = max(0.0, balance[account]-reserved[account]-max(protected[account], project))
            budget = rate*available
            output_project[i] = project
            output_free[i] = available
            output_budget[i] = budget
            for k in range(basket.shape[0]):
                good = basket[k]
                share = budget/3.0
                reference = 0.0
                # reference_price: минимальная доставленная котировка или market signal.
                for j in range(sell_count):
                    if sell_good[j] != good:
                        continue
                    distance = distances[account_tile[sell_account[j]], account_tile[account]]
                    if distance != distance:
                        continue
                    ceiling = sell_price[j]+distance*weight[good]*transport_rate
                    if reference <= 0.0 or ceiling < reference:
                        reference = ceiling
                if reference <= 0.0:
                    reference = _market_reference(good, account_state[account],
                        account_province[account], market_state, market_province,
                        market_good, market_volume, market_landed, default_price[good])
                quantity = share/reference
                if basic_rate[good] != 0.0:
                    quantity = min(quantity, family*quality*basic_rate[good])
                ceiling = _household_ceiling(good, quantity, account_tile[account],
                    account_state[account], account_province[account], sell_count,
                    sell_good, sell_account, sell_quantity, sell_price, sell_index,
                    account_tile, distances, weight, transport_rate, market_state,
                    market_province, market_good, market_volume, market_landed, history_len,
                    default_price[good], quote_count, quote_enterprise, quote_price,
                    enterprise_tile, offer_index, offer_landed, eps, -1, account, sell_active)
                output_good[request] = good
                quantity = min(share/ceiling,
                    max(0.0, balance[account]-reserved[account]-protected[account])/ceiling)
                if quantity > eps:
                    output_quantity[request] = quantity
                    output_reserved[request] = quantity*ceiling
                    output_price[request] = ceiling
                    output_index[request] = next_order
                    output_seq[request] = request
                    reserved[account] += quantity*ceiling
                    demand_good[demand_count] = good
                    demand_participant[demand_count] = account
                    demand_tile[demand_count] = account_tile[account]
                    demand_price[demand_count] = ceiling
                    demand_remaining[demand_count] = quantity
                    demand_reserved[demand_count] = quantity*ceiling
                    demand_active[demand_count] = True
                    demand_final[demand_count] = True
                    demand_construction[demand_count] = False
                    demand_count += 1
                    next_order += 1
                request += 1
    data['demand_order_count'] = demand_count
    data['owner_request_count'] = request
    data['owner_order_count_created'] = next_order-data['initial_order_count'] \
        -sell_count-data['household_order_count_created']
    return request


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


def owner_preparation(dict data, object rng):
    """Шесть последовательных фаз до recover_enterprises."""
    household_preparation(data, rng)
    owner_requests(data, rng)


cdef double _reference_price(int good, int buyer_account, int buyer_tile, int state, int province,
        int sell_count, int[::1] sell_good, int[::1] sell_account,
        double[::1] sell_price, unsigned char[::1] sell_active,
        int[::1] account_tile, double[:, ::1] distances, double[::1] weight,
        double transport_rate, int[::1] market_state, int[::1] market_province,
        int[::1] market_good, double[::1] market_volume, double[::1] market_landed,
        double default_price) noexcept nogil:
    cdef int i
    cdef double result = 0.0, distance, landed
    for i in range(sell_count):
        if sell_good[i] != good or not sell_active[i] or sell_account[i] == buyer_account:
            continue
        distance = distances[account_tile[sell_account[i]], buyer_tile]
        if distance != distance:
            continue
        landed = sell_price[i]+distance*weight[good]*transport_rate
        if result <= 0.0 or landed < result:
            result = landed
    if result > 0.0:
        return result
    return _market_reference(good, state, province, market_state, market_province,
        market_good, market_volume, market_landed, default_price)


cdef double _reachable_demand(int enterprise, int account, int good, int tile, double price,
        int demand_count, int[::1] order_good, int[::1] order_participant,
        int[::1] order_tile, double[::1] order_price, double[::1] order_remaining,
        double[::1] order_reserved, unsigned char[::1] order_active,
        int sell_count, int[::1] sell_good, int[::1] sell_account,
        double[::1] sell_quantity, double[::1] sell_price, int[::1] sell_order_index,
        unsigned char[::1] sell_active, double[::1] sell_remaining,
        int[::1] account_tile, double[:, ::1] distances, double[::1] weight,
        double transport_rate, int[::1] group_participant, int[::1] group_tile,
        double[::1] group_price, double[::1] group_limit, double[::1] group_freight,
        double[::1] group_volume, int[::1] offer_index, double[::1] offer_landed,
        double eps) noexcept nogil:
    cdef int i, j, g = 0, position, count
    cdef int participant, buyer_tile
    cdef double freight, limit, volume, distance, landed, covered, demand = 0.0
    for i in range(demand_count):
        if (order_good[i] != good or not order_active[i] or order_remaining[i] <= eps
                or order_participant[i] == account):
            continue
        distance = distances[tile, order_tile[i]]
        if distance != distance:
            continue
        freight = distance*weight[good]*transport_rate
        limit = order_price[i]-freight
        if limit+eps < price:
            continue
        participant = order_participant[i]
        buyer_tile = order_tile[i]
        for j in range(g):
            if (group_participant[j] == participant and group_tile[j] == buyer_tile
                    and group_price[j] == order_price[i]):
                break
        else:
            j = g
            group_participant[j] = participant
            group_tile[j] = buyer_tile
            group_price[j] = order_price[i]
            group_limit[j] = limit
            group_freight[j] = freight
            group_volume[j] = 0.0
            g += 1
        group_volume[j] += min(order_remaining[i], order_reserved[i]/max(price+freight, eps))
    for i in range(sell_count):
        sell_remaining[i] = sell_quantity[i]
    for i in range(g):
        count = 0
        for j in range(sell_count):
            if sell_good[j] != good or not sell_active[j]:
                continue
            distance = distances[account_tile[sell_account[j]], group_tile[i]]
            if distance != distance:
                continue
            landed = sell_price[j]+distance*weight[good]*transport_rate
            position = count
            while position > 0:
                if (offer_landed[position-1] < landed
                        or (offer_landed[position-1] == landed
                            and not _order_id_less(sell_order_index[j],
                                                   sell_order_index[offer_index[position-1]]))):
                    break
                offer_index[position] = offer_index[position-1]
                offer_landed[position] = offer_landed[position-1]
                position -= 1
            offer_index[position] = j
            offer_landed[position] = landed
            count += 1
        volume = group_volume[i]
        landed = price+group_freight[i]
        for j in range(count):
            if offer_landed[j] > group_price[i]+eps or offer_landed[j] >= landed-eps:
                break
            if sell_account[offer_index[j]] == account:
                continue
            covered = min(volume, sell_remaining[offer_index[j]])
            volume -= covered
            sell_remaining[offer_index[j]] -= covered
        demand += volume
    return demand


def recover_enterprises(dict data, object rng):
    """Прогноз, финансирование, reopen и отмены в исходном порядке фирм."""
    cdef int enterprise_count = data['enterprise_good'].shape[0]
    cdef list shuffled = list(range(enterprise_count))
    rng.shuffle(shuffled)
    cdef int[::1] recovery_order = data['enterprise_recovery_order']
    cdef int i
    for i in range(enterprise_count):
        recovery_order[i] = shuffled[i]
    cdef int[::1] enterprise_good = data['enterprise_good']
    cdef int[::1] enterprise_tile = data['enterprise_tile']
    cdef int[::1] enterprise_owner = data['enterprise_owner']
    cdef int[::1] enterprise_account = data['enterprise_account']
    cdef double[::1] enterprise_price = data['enterprise_price']
    cdef double[::1] production_price = data['recovery_production_price']
    cdef double[::1] capacity = data['enterprise_capacity']
    cdef double[::1] previous_output = data['enterprise_previous_output']
    cdef double[::1] sales_forecast = data['enterprise_sales_forecast']
    cdef double[::1] sold_now = data['enterprise_sold_now']
    cdef int[::1] history_count = data['enterprise_history_count']
    cdef double[:, ::1] history_sold = data['enterprise_history_sold']
    cdef unsigned char[::1] suspended = data['enterprise_suspended']
    cdef unsigned char[::1] closed = data['enterprise_closed']
    cdef int[::1] available_from = data['enterprise_available_from']
    cdef double[:, ::1] stock = data['account_stock']
    cdef double[:, ::1] stock_reserved = data['account_stock_reserved']
    cdef double[:, ::1] locked = data['account_locked']
    cdef double[::1] balance = data['account_balance']
    cdef double[::1] money_reserved = data['account_money_reserved']
    cdef double[::1] protected = data['account_protected']
    cdef int[::1] input_offset = data['recovery_input_offset']
    cdef int[::1] input_good = data['recovery_input_good']
    cdef double[::1] input_value = data['recovery_input_value']
    cdef int[::1] wear_offset = data['recovery_wear_offset']
    cdef int[::1] wear_good = data['recovery_wear_good']
    cdef double[::1] wear_value = data['recovery_wear_value']
    cdef int[::1] equipment_offset = data['recovery_equipment_offset']
    cdef int[::1] equipment_good = data['recovery_equipment_good']
    cdef double[::1] equipment_value = data['recovery_equipment_value']
    cdef double[::1] labor = data['good_labor']
    cdef double[::1] default_price = data['good_default_price']
    cdef double[::1] weight = data['good_weight']
    cdef unsigned char[::1] consumer = data['good_consumer']
    cdef double[:, ::1] distances = data['distances']
    cdef int[::1] account_tile = data['account_tile']
    cdef int[::1] account_state = data['account_state']
    cdef int[::1] account_province = data['account_province']
    cdef int[::1] market_state = data['market_state']
    cdef int[::1] market_province = data['market_province']
    cdef int[::1] market_good = data['market_good']
    cdef double[::1] market_volume = data['market_volume']
    cdef double[::1] market_landed = data['market_landed_component']
    cdef int sell_count = data['sell_order_count']
    cdef int[::1] sell_good = data['sell_order_good']
    cdef int[::1] sell_account = data['sell_order_account']
    cdef double[::1] sell_quantity = data['sell_order_quantity']
    cdef double[::1] sell_price = data['sell_order_price']
    cdef int[::1] sell_index = data['sell_order_index']
    cdef unsigned char[::1] sell_active = data['sell_active']
    cdef double[::1] sell_remaining = data['sell_remaining']
    cdef int demand_count = data['demand_order_count']
    cdef int[::1] demand_good = data['demand_order_good']
    cdef int[::1] demand_participant = data['demand_order_participant']
    cdef int[::1] demand_tile = data['demand_order_tile']
    cdef double[::1] demand_price = data['demand_order_price']
    cdef double[::1] demand_remaining = data['demand_order_remaining']
    cdef double[::1] demand_reserved = data['demand_order_reserved']
    cdef unsigned char[::1] demand_active = data['demand_order_active']
    cdef unsigned char[::1] demand_final = data['demand_order_final']
    cdef unsigned char[::1] demand_construction = data['demand_order_construction']
    cdef int[::1] group_participant = data['demand_group_participant']
    cdef int[::1] group_tile = data['demand_group_tile']
    cdef double[::1] group_price = data['demand_group_price']
    cdef double[::1] group_limit = data['demand_group_limit']
    cdef double[::1] group_freight = data['demand_group_freight']
    cdef double[::1] group_volume = data['demand_group_volume']
    cdef int[::1] offer_index = data['household_offer_index']
    cdef double[::1] offer_landed = data['household_offer_landed']
    cdef int[::1] quote_count = data['recovery_quote_count']
    cdef int[:, ::1] quote_enterprise = data['recovery_quote_enterprise']
    cdef double[:, ::1] quote_price = data['recovery_quote_price']
    cdef double[::1] project_capacity = data['project_capacity']
    cdef double[::1] project_working = data['project_working']
    cdef double[::1] project_delivery = data['project_delivery']
    cdef int[::1] project_tile = data['project_tile']
    cdef int[::1] construction_good = data['construction_good']
    cdef double[::1] construction_ratio = data['construction_ratio']
    cdef double[::1] construction_amount = data['construction_amount']
    cdef double minimum_capacity = data['minimum_capacity']
    cdef int[::1] output_enterprise = data['recovery_enterprise']
    cdef double[::1] output_transfer = data['recovery_transfer']
    cdef double[::1] output_hold = data['recovery_hold']
    cdef unsigned char[::1] output_reopened = data['recovery_reopened']
    cdef int[:, ::1] output_cancel = data['recovery_cancel_sell']
    cdef int[::1] output_cancel_count = data['recovery_cancel_count']
    cdef double[::1] debug_target = data['recovery_debug_target']
    cdef double[::1] debug_demand = data['recovery_debug_demand']
    cdef double[::1] debug_margin = data['recovery_debug_margin']
    cdef double[::1] debug_required = data['recovery_debug_required']
    cdef double[::1] debug_forecast = data['recovery_debug_forecast']
    cdef double[::1] debug_expected = data['recovery_debug_expected']
    cdef unsigned char[::1] stock_cache_set = data['recovery_stock_cache_set']
    cdef int[:, ::1] outlook_market_version = data['recovery_market_version']
    cdef int[::1] market_version_work = data['recovery_market_version_work']
    cdef double[::1] outlook_cost = data['recovery_expected_cost']
    cdef double[::1] outlook_price = data['recovery_expected_price']
    cdef double[::1] outlook_planned_price = data['recovery_planned_price']
    cdef double[::1] outlook_fixed = data['recovery_fixed_cost']
    cdef double[::1] outlook_fixed_unit = data['recovery_fixed_unit']
    cdef int[::1] outlook_limit = data['recovery_production_limit']
    cdef unsigned char[::1] outlook_valid = data['recovery_outlook_valid']
    cdef double wage = data['wage'], tax = data['consumption_tax']
    cdef double transport_rate = data['transport_rate'], eps = data['eps']
    cdef double overhead_rate = data['enterprise_overhead_rate']
    cdef double overstock = data['output_overstock_factor']
    cdef double decline = data['output_decline_rate']
    cdef int stock_revision = data['stock_planning_revision']
    cdef int month = data['current_month'], history_len = data['market_history_len']
    cdef int j, k, e, account, owner, good, input_g, cancel_count, out = 0
    cdef double cost, raw_hi, raw_lo, wear_hi, wear_lo, item, next_hi
    cdef double reference, base_net, quote_net, price, quantity, final_quantity
    cdef double landed, volume, share, own, demand, stock_demand, forecast, offered, average
    cdef double expected, projected, target, previous, fixed, margin, required
    cdef double missing, free, project, available, amount, hold, distance, ordered
    cdef double weight_hi, weight_lo, limit
    with nogil:
        for i in range(enterprise_count):
            e = recovery_order[i]
            if suspended[e] or available_from[e] > month:
                continue
            account = enterprise_account[e]
            good = enterprise_good[e]
            outlook_valid[e] = True
            for j in range(market_version_work.shape[0]):
                outlook_market_version[e, j] = market_version_work[j]
            raw_hi = 0.0; raw_lo = 0.0; wear_hi = 0.0; wear_lo = 0.0
            for k in range(input_offset[good], input_offset[good+1]):
                input_g = input_good[k]
                reference = _reference_price(input_g, account, enterprise_tile[e],
                    account_state[account], account_province[account], sell_count,
                    sell_good, sell_account, sell_price, sell_active, account_tile,
                    distances, weight, transport_rate, market_state, market_province,
                    market_good, market_volume, market_landed, default_price[input_g])
                item = input_value[k]*reference
                next_hi = raw_hi+item
                if abs(raw_hi) >= abs(item): raw_lo += (raw_hi-next_hi)+item
                else: raw_lo += (item-next_hi)+raw_hi
                raw_hi = next_hi
            for k in range(wear_offset[good], wear_offset[good+1]):
                input_g = wear_good[k]
                reference = _reference_price(input_g, account, enterprise_tile[e],
                    account_state[account], account_province[account], sell_count,
                    sell_good, sell_account, sell_price, sell_active, account_tile,
                    distances, weight, transport_rate, market_state, market_province,
                    market_good, market_volume, market_landed, default_price[input_g])
                item = wear_value[k]*reference
                next_hi = wear_hi+item
                if abs(wear_hi) >= abs(item): wear_lo += (wear_hi-next_hi)+item
                else: wear_lo += (item-next_hi)+wear_hi
                wear_hi = next_hi
            cost = labor[good]*wage+(raw_hi+raw_lo)+(wear_hi+wear_lo)
            quantity = 0.0; final_quantity = 0.0
            for j in range(demand_count):
                if (demand_good[j] != good or not demand_active[j]
                        or demand_remaining[j] <= eps or demand_participant[j] == account):
                    continue
                distance = distances[enterprise_tile[e], demand_tile[j]]
                if distance != distance: continue
                limit = demand_price[j]-distance*weight[good]*transport_rate
                if limit+eps < enterprise_price[e]: continue
                landed = enterprise_price[e]+(demand_price[j]-limit)
                volume = min(demand_remaining[j], demand_reserved[j]/max(landed, eps))
                quantity += volume
                if demand_final[j]: final_quantity += volume
            share = final_quantity/quantity if quantity > eps else (1.0 if consumer[good] else 0.0)
            base_net = enterprise_price[e]*(1-tax*share)
            price = enterprise_price[e] if base_net+eps >= cost else production_price[e]
            if price == enterprise_price[e]:
                quote_net = base_net
            else:
                quantity = 0.0; final_quantity = 0.0
                for j in range(demand_count):
                    if (demand_good[j] != good or not demand_active[j]
                            or demand_remaining[j] <= eps or demand_participant[j] == account):
                        continue
                    distance = distances[enterprise_tile[e], demand_tile[j]]
                    if distance != distance: continue
                    limit = demand_price[j]-distance*weight[good]*transport_rate
                    if limit+eps < price: continue
                    landed = price+(demand_price[j]-limit)
                    volume = min(demand_remaining[j], demand_reserved[j]/max(landed, eps))
                    quantity += volume
                    if demand_final[j]: final_quantity += volume
                share = final_quantity/quantity if quantity > eps else (1.0 if consumer[good] else 0.0)
                quote_net = price*(1-tax*share)
            if history_count[e]:
                raw_hi = 0.0; raw_lo = 0.0
                for k in range(history_count[e]):
                    item = history_sold[e, k]
                    next_hi = raw_hi+item
                    if abs(raw_hi) >= abs(item): raw_lo += (raw_hi-next_hi)+item
                    else: raw_lo += (item-next_hi)+raw_hi
                    raw_hi = next_hi
                own = (raw_hi+raw_lo)/history_count[e]
            else:
                own = sales_forecast[e]
            demand = _reachable_demand(e, account, good, enterprise_tile[e], price,
                demand_count, demand_good, demand_participant, demand_tile, demand_price,
                demand_remaining, demand_reserved, demand_active, sell_count, sell_good,
                sell_account, sell_quantity, sell_price, sell_index, sell_active,
                sell_remaining, account_tile, distances, weight, transport_rate,
                group_participant, group_tile, group_price, group_limit, group_freight,
                group_volume, offer_index, offer_landed, eps)
            forecast = max(own, sold_now[e]+demand)
            offered = 0.0
            for j in range(sell_count):
                if sell_good[j] == good and sell_account[j] == account and sell_active[j]:
                    offered += sell_quantity[j]
            expected = 0.0
            if stock_revision and offered > eps:
                average = own if history_count[e] else 0.0
                stock_demand = demand
                if price != enterprise_price[e]:
                    stock_demand = _reachable_demand(e, account, good, enterprise_tile[e],
                        enterprise_price[e], demand_count, demand_good, demand_participant,
                        demand_tile, demand_price, demand_remaining, demand_reserved,
                        demand_active, sell_count, sell_good, sell_account, sell_quantity,
                        sell_price, sell_index, sell_active, sell_remaining, account_tile,
                        distances, weight, transport_rate, group_participant, group_tile,
                        group_price, group_limit, group_freight, group_volume, offer_index,
                        offer_landed, eps)
                expected = min(stock[account, good], offered, max(stock_demand, average))
                stock_cache_set[e] = True
            debug_forecast[e] = forecast
            debug_expected[e] = expected
            projected = max(0.0, stock[account, good]-expected)
            target = max(0.0, min(capacity[e], forecast-projected))
            previous = previous_output[e]
            if (stock_revision >= 2 and target < previous-eps
                    and not stock[account, good] > forecast*overstock):
                target = previous-decline*(previous-target)
                target = max(0.0, min(capacity[e], target))
            fixed = capacity[e]*labor[good]*wage*overhead_rate
            limit = fixed/target if target > eps else 0.0
            margin = quote_net-cost-limit
            outlook_limit[e] = (0 if projected >= forecast and forecast > eps
                else 1 if forecast <= eps else 2)
            if margin < -eps:
                target = 0.0
                outlook_limit[e] = 3
            outlook_cost[e] = cost
            outlook_price[e] = quote_net
            outlook_planned_price[e] = price
            outlook_fixed[e] = fixed
            outlook_fixed_unit[e] = limit
            debug_target[e] = target
            debug_demand[e] = demand
            debug_margin[e] = margin
            if target <= eps or margin <= eps or demand <= eps:
                continue
            required = target*labor[good]*wage
            for k in range(input_offset[good], input_offset[good+1]):
                input_g = input_good[k]
                free = max(0.0, stock[account, input_g]-stock_reserved[account, input_g]
                           -locked[account, input_g])
                missing = max(0.0, target*input_value[k]-free)
                reference = _household_ceiling(input_g, missing, account_tile[account],
                    account_state[account], account_province[account], sell_count,
                    sell_good, sell_account, sell_quantity, sell_price, sell_index,
                    account_tile, distances, weight, transport_rate, market_state,
                    market_province, market_good, market_volume, market_landed, history_len,
                    default_price[input_g], quote_count, quote_enterprise, quote_price,
                    enterprise_tile, offer_index, offer_landed, eps, e, account, sell_active)
                required += missing*reference
            for k in range(equipment_offset[good], equipment_offset[good+1]):
                input_g = equipment_good[k]
                free = max(0.0, stock[account, input_g]-stock_reserved[account, input_g]
                           -locked[account, input_g])
                missing = max(0.0, target*equipment_value[k]-free)
                reference = _household_ceiling(input_g, missing, account_tile[account],
                    account_state[account], account_province[account], sell_count,
                    sell_good, sell_account, sell_quantity, sell_price, sell_index,
                    account_tile, distances, weight, transport_rate, market_state,
                    market_province, market_good, market_volume, market_landed, history_len,
                    default_price[input_g], quote_count, quote_enterprise, quote_price,
                    enterprise_tile, offer_index, offer_landed, eps, e, account, sell_active)
                required += missing*reference
            debug_required[e] = required
            owner = enterprise_owner[e]
            project = 0.0
            if project_tile[owner] >= 0:
                distance = distances[account_tile[owner], project_tile[owner]]
                project = project_working[owner]
                if distance == distance:
                    weight_hi = 0.0; weight_lo = 0.0
                    for k in range(construction_good.shape[0]):
                        item = construction_amount[k]*project_capacity[owner]/minimum_capacity*weight[construction_good[k]]
                        next_hi = weight_hi+item
                        if abs(weight_hi) >= abs(item): weight_lo += (weight_hi-next_hi)+item
                        else: weight_lo += (item-next_hi)+weight_hi
                        weight_hi = next_hi
                    project += (weight_hi+weight_lo)*distance*transport_rate
                else:
                    project += project_delivery[owner]
                for k in range(construction_good.shape[0]):
                    input_g = construction_good[k]
                    ordered = 0.0
                    for j in range(demand_count):
                        if (demand_participant[j] == owner and demand_construction[j]
                                and demand_good[j] == input_g and demand_active[j]
                                and demand_reserved[j] > eps):
                            ordered += demand_remaining[j]
                    missing = max(0.0, construction_amount[k]*project_capacity[owner]/minimum_capacity
                                  -stock[owner, input_g]-ordered)
                    reference = _household_ceiling(input_g, missing, account_tile[owner],
                        account_state[owner], account_province[owner], sell_count,
                        sell_good, sell_account, sell_quantity, sell_price, sell_index,
                        account_tile, distances, weight, transport_rate, market_state,
                        market_province, market_good, market_volume, market_landed, history_len,
                        default_price[input_g], quote_count, quote_enterprise, quote_price,
                        enterprise_tile, offer_index, offer_landed, eps, -1, owner, sell_active)
                    project += missing*reference
            available = max(0.0, balance[owner]-money_reserved[owner]-max(protected[owner], project))
            free = max(0.0, balance[account]-money_reserved[account]-protected[account])
            amount = min(max(0.0, required-free), available)
            if amount > 0.0:
                balance[owner] -= amount
                balance[account] += amount
            if closed[e] and max(0.0, balance[account]-money_reserved[account]-protected[account]) > eps:
                closed[e] = False
                cancel_count = 0
                for j in range(sell_count):
                    if sell_account[j] == account and sell_good[j] != good and sell_active[j]:
                        sell_active[j] = False
                        market_version_work[sell_good[j]] += 1
                        stock_reserved[account, sell_good[j]] = max(0.0,
                            stock_reserved[account, sell_good[j]]-sell_quantity[j])
                        output_cancel[out, cancel_count] = j
                        cancel_count += 1
                output_cancel_count[out] = cancel_count
                output_reopened[out] = True
            if not closed[e]:
                hold = min(max(0.0, balance[account]-money_reserved[account]-protected[account]), required)
                protected[account] += hold
                output_hold[out] = hold
            output_enterprise[out] = e
            output_transfer[out] = amount
            out += 1
    data['recovery_outlook_count'] = out
    return out


def recovery_preparation(dict data, object rng):
    """Семь последовательных фаз через recovery в одном native state."""
    owner_preparation(data, rng)
    recover_enterprises(data, rng)


def public_project_requests(dict data):
    """Закрепить городской склад и создать обеспеченные публичные заявки."""
    cdef int request_count = data['public_request_good'].shape[0]
    cdef int[::1] request_account = data['public_request_account']
    cdef int[::1] request_good = data['public_request_good']
    cdef double[::1] request_required = data['public_request_required']
    cdef double[::1] request_held = data['public_request_held']
    cdef double[::1] output_held = data['public_held']
    cdef double[::1] output_quantity = data['public_order_quantity']
    cdef double[::1] output_reserved = data['public_order_reserved']
    cdef double[::1] output_price = data['public_order_price']
    cdef int[::1] output_index = data['public_order_index']
    cdef int[::1] output_release_request = data['public_release_request']
    cdef int[::1] output_release_sell = data['public_release_sell']
    cdef double[::1] output_release_quantity = data['public_release_quantity']
    cdef double[:, ::1] stock = data['account_stock']
    cdef double[:, ::1] stock_reserved = data['account_stock_reserved']
    cdef double[:, ::1] locked = data['account_locked']
    cdef double[::1] balance = data['account_balance']
    cdef double[::1] reserved = data['account_money_reserved']
    cdef double[::1] protected = data['account_protected']
    cdef int[::1] sell_good = data['sell_order_good']
    cdef int[::1] sell_account = data['sell_order_account']
    cdef double[::1] sell_quantity = data['sell_order_quantity']
    cdef double[::1] sell_remaining = data['sell_order_remaining']
    cdef double[::1] sell_backing = data['sell_order_backing']
    cdef double[::1] sell_price = data['sell_order_price']
    cdef int[::1] sell_index = data['sell_order_index']
    cdef unsigned char[::1] sell_active = data['sell_active']
    cdef int sell_count = data['sell_order_count']
    cdef int[::1] account_tile = data['account_tile']
    cdef int[::1] account_state = data['account_state']
    cdef int[::1] account_province = data['account_province']
    cdef double[:, ::1] distances = data['distances']
    cdef double[::1] weight = data['good_weight']
    cdef double transport_rate = data['transport_rate']
    cdef int[::1] market_state = data['market_state']
    cdef int[::1] market_province = data['market_province']
    cdef int[::1] market_good = data['market_good']
    cdef double[::1] market_volume = data['market_volume']
    cdef double[::1] market_landed = data['market_landed_component']
    cdef int history_len = data['market_history_len']
    cdef double[::1] default_price = data['good_default_price']
    cdef int[::1] quote_count = data['recovery_quote_count']
    cdef int[:, ::1] quote_enterprise = data['recovery_quote_enterprise']
    cdef double[:, ::1] quote_price = data['recovery_quote_price']
    cdef int[::1] enterprise_tile = data['enterprise_tile']
    cdef int[::1] offer_index = data['household_offer_index']
    cdef double[::1] offer_landed = data['household_offer_landed']
    cdef int[::1] market_version_work = data['recovery_market_version_work']
    cdef int[::1] demand_good = data['demand_order_good']
    cdef int[::1] demand_participant = data['demand_order_participant']
    cdef int[::1] demand_tile = data['demand_order_tile']
    cdef double[::1] demand_price = data['demand_order_price']
    cdef double[::1] demand_remaining = data['demand_order_remaining']
    cdef double[::1] demand_reserved = data['demand_order_reserved']
    cdef unsigned char[::1] demand_active = data['demand_order_active']
    cdef unsigned char[::1] demand_final = data['demand_order_final']
    cdef unsigned char[::1] demand_construction = data['demand_order_construction']
    cdef int demand_count = data['demand_order_count']
    cdef int next_order = data['initial_order_count']+sell_count \
        +data['household_order_count_created']+data['owner_order_count_created']
    cdef double eps = data['eps']
    cdef int i, j, account, good, release_out = 0, order_out = 0
    cdef double held, wanted, assign, free, release, take, missing, ceiling, quantity
    with nogil:
        for i in range(request_count):
            account = request_account[i]
            good = request_good[i]
            held = request_held[i]
            wanted = max(0.0, request_required[i]-held)
            if wanted <= eps:
                output_held[i] = held
                continue
            assign = min(wanted, max(0.0, stock[account, good]-locked[account, good]))
            free = max(0.0, stock[account, good]-stock_reserved[account, good]
                       -locked[account, good])
            release = max(0.0, assign-free)
            for j in range(sell_count):
                if release <= eps:
                    break
                if sell_account[j] != account or sell_good[j] != good or not sell_active[j]:
                    continue
                take = min(release, sell_remaining[j])
                sell_quantity[j] -= take
                sell_remaining[j] -= take
                sell_backing[j] -= take
                stock_reserved[account, good] = max(0.0,
                    stock_reserved[account, good]-take)
                release -= take
                market_version_work[good] += 1
                output_release_request[release_out] = i
                output_release_sell[release_out] = j
                output_release_quantity[release_out] = take
                release_out += 1
                if sell_remaining[j] <= eps:
                    sell_active[j] = False
                    market_version_work[good] += 1
            free = max(0.0, stock[account, good]-stock_reserved[account, good]
                       -locked[account, good])
            assign = min(wanted, free)
            output_held[i] = held+assign
            locked[account, good] += assign
            missing = max(0.0, request_required[i]-(held+assign))
            if missing <= eps:
                continue
            ceiling = _household_ceiling(good, missing, account_tile[account],
                account_state[account], account_province[account], sell_count,
                sell_good, sell_account, sell_quantity, sell_price, sell_index,
                account_tile, distances, weight, transport_rate, market_state,
                market_province, market_good, market_volume, market_landed, history_len,
                default_price[good], quote_count, quote_enterprise, quote_price,
                enterprise_tile, offer_index, offer_landed, eps, -1, account, sell_active)
            quantity = min(missing,
                max(0.0, balance[account]-reserved[account]-protected[account])/ceiling)
            if quantity <= eps:
                continue
            output_quantity[i] = quantity
            output_reserved[i] = quantity*ceiling
            output_price[i] = ceiling
            output_index[i] = next_order
            reserved[account] += quantity*ceiling
            demand_good[demand_count] = good
            demand_participant[demand_count] = account
            demand_tile[demand_count] = account_tile[account]
            demand_price[demand_count] = ceiling
            demand_remaining[demand_count] = quantity
            demand_reserved[demand_count] = quantity*ceiling
            demand_active[demand_count] = True
            demand_final[demand_count] = False
            demand_construction[demand_count] = False
            demand_count += 1
            next_order += 1
            order_out += 1
    data['public_release_count'] = release_out
    data['public_order_count_created'] = order_out
    data['demand_order_count'] = demand_count
    return order_out


def public_project_preparation(dict data, object rng):
    """Восемь последовательных фаз через public_project_requests."""
    recovery_preparation(data, rng)
    public_project_requests(data)


# ---------------------------------------------------------------------------
# Шаг 4: чистый native project_candidate (без commit, без подключения к step)
# ---------------------------------------------------------------------------

from libc.stdlib cimport malloc, free
from libc.math cimport isnan, fabs


cdef double _csum(double* values, Py_ssize_t n) noexcept nogil:
    """Компенсированное сложение Неумайера как в CPython 3.12 sum(float)."""
    cdef Py_ssize_t i
    cdef double s = 0.0, c = 0.0, x, t
    for i in range(n):
        x = values[i]
        t = s + x
        if fabs(s) >= fabs(x):
            c += (s - t) + x
        else:
            c += (x - t) + s
        s = t
    return s + c


cdef int _find_pair(int[::1] keys_a, int[::1] keys_b, int count,
                    int a, int b) noexcept nogil:
    cdef int i
    for i in range(count):
        if keys_a[i] == a and keys_b[i] == b:
            return i
    return -1


cdef int _find_triple(int[::1] keys_a, int[::1] keys_b, int[::1] keys_c,
                      int count, int a, int b, int c) noexcept nogil:
    cdef int i
    for i in range(count):
        if keys_a[i] == a and keys_b[i] == b and keys_c[i] == c:
            return i
    return -1


cdef double _lookup2_or_zero(int[::1] keys_a, int[::1] keys_b,
                             double[::1] values, int count,
                             int a, int b) noexcept nogil:
    cdef int i
    for i in range(count):
        if keys_a[i] == a and keys_b[i] == b:
            return values[i]
    return 0.0


cdef double _sig_field(dict sup, int state, int province, int good,
                       int field) except? -1.0:
    """field: 0=volume 1=unmet 2=price 3=landed 4=limit. -1.0 = ключ не найден."""
    cdef int idx = _find_triple(sup['sig_state'], sup['sig_province'],
                                sup['sig_good'], sup['sig_count'],
                                state, province, good)
    if idx < 0:
        return -1.0
    if field == 0:
        return sup['sig_volume'][idx]
    if field == 1:
        return sup['sig_unmet'][idx]
    if field == 2:
        return sup['sig_price'][idx]
    if field == 3:
        return sup['sig_landed'][idx]
    return sup['sig_limit'][idx]


cdef void _book_range(dict sup, int tile, int good, int* start, int* end) except *:
    cdef int idx = _find_pair(sup['bk_tile'], sup['bk_good'], sup['bk_count'],
                              tile, good)
    if idx < 0:
        raise ValueError('candidate support: книга (%d, %d) не упакована'
                         % (tile, good))
    start[0] = sup['bk_offset'][idx]
    end[0] = sup['bk_offset'][idx + 1]


cdef double _rf_value(dict sup, int tile, int good, int exclude) except? -1.0:
    cdef int idx = _find_triple(sup['rf_tile'], sup['rf_good'], sup['rf_exclude'],
                                sup['rf_count'], tile, good, exclude)
    if idx < 0:
        raise ValueError('candidate support: ref price (%d, %d, %d) не упакован'
                         % (tile, good, exclude))
    return sup['rf_value'][idx]


cdef void _quoted_cost_native(dict inv, dict sup, int tile, int good, int exclude,
                              double quantity,
                              double* out_value, double* out_ceiling) except *:
    """Точная копия quoted_cost: книга, затем recovery fallback."""
    cdef int start, end, i, firm
    cdef double left = quantity, value = 0.0, ceiling = 0.0
    cdef double q, price, reference, best = 0.0, d, freight, weight
    cdef bint any_quote = False
    cdef double eps = inv['eps']
    cdef int[::1] rq_offset = inv['recovery_quote_offset']
    cdef int[::1] rq_firm = inv['recovery_quote_firm']
    cdef double[::1] rq_price = inv['recovery_quote_price']
    cdef int[::1] ent_tile = inv['enterprise_tile']
    cdef int[::1] ent_account = inv['enterprise_account']
    cdef double[:, ::1] distances = sup['distances']
    _book_range(sup, tile, good, &start, &end)
    cdef double[::1] bk_price = sup['bk_price']
    cdef int[::1] bk_participant = sup['bk_participant']
    cdef double[::1] bk_remaining = sup['bk_remaining']
    cdef unsigned char[::1] bk_active = sup['bk_active']
    for i in range(start, end):
        if bk_participant[i] == exclude or bk_remaining[i] <= eps or not bk_active[i]:
            continue
        q = min(left, bk_remaining[i])
        value += q*bk_price[i]
        ceiling = max(ceiling, bk_price[i])
        left -= q
        if left <= eps:
            break
    if left > eps:
        reference = _rf_value(sup, tile, good, exclude)
        weight = inv['good_weight'][good]
        for i in range(rq_offset[good], rq_offset[good + 1]):
            firm = rq_firm[i]
            if ent_account[firm] == exclude:
                continue
            d = distances[ent_tile[firm], tile]
            if isnan(d):
                continue
            freight = d*weight*inv['transport_rate']
            price = rq_price[i] + freight
            if not any_quote or price < best:
                best = price
                any_quote = True
        if any_quote:
            reference = max(reference, best)
        value += left*reference
        ceiling = max(ceiling, reference)
    out_value[0] = value
    out_ceiling[0] = max(ceiling, eps)


cdef double _unit_cost_native(dict inv, dict sup, int tile, int good,
                              int exclude) except? -1.0:
    """Точная копия unit_cost: компенсированные суммы raw и wear."""
    cdef int i, input_good
    cdef double* raw_terms = <double*> malloc(64*sizeof(double))
    cdef double* wear_terms = <double*> malloc(64*sizeof(double))
    if raw_terms == NULL or wear_terms == NULL:
        free(raw_terms); free(wear_terms)
        raise MemoryError()
    cdef int[::1] in_off = inv['recipe_input_offset']
    cdef int[::1] in_good = inv['recipe_input_good']
    cdef double[::1] in_value = inv['recipe_input_value']
    cdef int[::1] eq_off = inv['recipe_equipment_offset']
    cdef int[::1] eq_good = inv['recipe_equipment_good']
    cdef double[::1] eq_value = inv['recipe_equipment_value']
    cdef double[::1] wear_rate = sup['good_wear_rate']
    cdef Py_ssize_t n_raw = 0, n_wear = 0
    cdef double result
    for i in range(in_off[good], in_off[good + 1]):
        input_good = in_good[i]
        raw_terms[n_raw] = in_value[i]*_rf_value(sup, tile, input_good, exclude)
        n_raw += 1
    for i in range(eq_off[good], eq_off[good + 1]):
        input_good = eq_good[i]
        wear_terms[n_wear] = (eq_value[i]*wear_rate[input_good]) \
            *_rf_value(sup, tile, input_good, exclude)
        n_wear += 1
    result = inv['good_labor'][good]*inv['wage'] \
        + _csum(raw_terms, n_raw) + _csum(wear_terms, n_wear)
    free(raw_terms); free(wear_terms)
    return result


cdef double _net_price_native(dict inv, dict sup, int tile, int good,
                              double price, int exclude) except? -1.0:
    """Точная копия net_price по упакованным customer quotes."""
    cdef int idx = _find_pair(sup['cq_tile'], sup['cq_good'], sup['cq_count'],
                              tile, good)
    if idx < 0:
        raise ValueError('candidate support: customer quotes (%d, %d) не упакованы'
                         % (tile, good))
    cdef int start = sup['cq_offset'][idx], end = sup['cq_offset'][idx + 1]
    cdef int i
    cdef double quantity = 0.0, final_quantity = 0.0, limit, landed, volume
    cdef int[::1] cq_participant = sup['cq_participant']
    cdef double[::1] cq_price = sup['cq_price']
    cdef double[::1] cq_limit = sup['cq_limit']
    cdef double[::1] cq_remaining = sup['cq_remaining']
    cdef double[::1] cq_reserved = sup['cq_reserved']
    cdef unsigned char[::1] cq_final = sup['cq_final']
    cdef double eps = inv['eps']
    for i in range(start, end):
        if cq_participant[i] == exclude:
            continue
        limit = cq_limit[i]
        if limit+eps < price:
            continue
        landed = price+(cq_price[i]-limit)
        volume = min(cq_remaining[i], cq_reserved[i]/max(landed, eps))
        quantity += volume
        if cq_final[i]:
            final_quantity += volume
    cdef double share
    if quantity > eps:
        share = final_quantity/quantity
    else:
        share = 1.0 if inv['good_consumer'][good] else 0.0
    return price*(1-inv['consumption_tax']*share)


cdef double _customer_limit_native(dict sup, int tile, int good,
                                   int exclude) except? -1.0:
    """Лучшая цена спроса не самого инвестора; -1.0 = лимита нет."""
    cdef int idx = _find_pair(sup['ld_tile'], sup['ld_good'], sup['ld_count'],
                              tile, good)
    if idx < 0:
        raise ValueError('candidate support: leaders (%d, %d) не упакованы'
                         % (tile, good))
    cdef double limit0 = sup['ld_limit0'][idx]
    cdef int part0 = sup['ld_part0'][idx]
    cdef double limit1 = sup['ld_limit1'][idx]
    cdef int part1 = sup['ld_part1'][idx]
    if part0 != exclude and limit0 > 0:
        return limit0
    if part1 != exclude and limit1 > 0:
        return limit1
    return -1.0


cdef double _investment_demand_native(dict inv, dict sup, dict dyn,
                                      int tile, int good, double price,
                                      int exclude) except? -1.0:
    """Точная копия investment_demand для quote_firm одного кандидата."""
    cdef int i, j, s, pos
    cdef double volume, covered, residual = 0.0
    cdef double eps = inv['eps']
    cdef double weight = inv['good_weight'][good]
    cdef double rate = inv['transport_rate']
    cdef double[:, ::1] distances = sup['distances']

    # cheap = {order_id: remaining} по всем sells[good]
    cdef int[::1] gs_offset = sup['gs_offset']
    cdef double[::1] gs_remaining = sup['gs_remaining']
    cdef int cheap_start = gs_offset[good], cheap_end = gs_offset[good + 1]
    cdef double* cheap = <double*> malloc((cheap_end-cheap_start)*sizeof(double))
    if cheap == NULL:
        raise MemoryError()
    for i in range(cheap_end-cheap_start):
        cheap[i] = gs_remaining[cheap_start+i]

    # suppliers: базовые фирмы, затем проекты живого индекса; копии на вызов
    cdef int[::1] sup_offset = sup['sup_offset']
    cdef int n_base = sup_offset[good+1]-sup_offset[good]
    cdef int[::1] pj_offset = dyn['pj_offset']
    cdef int n_pj = pj_offset[good+1]-pj_offset[good]
    cdef int n_sup = n_base+n_pj
    cdef double* sup_cap = <double*> malloc((n_sup if n_sup > 0 else 1)*sizeof(double))
    if sup_cap == NULL:
        free(cheap)
        raise MemoryError()
    cdef int[::1] sup_tile = sup['sup_tile']
    cdef double[::1] sup_price = sup['sup_price']
    cdef double[::1] sup_capacity = sup['sup_capacity']
    cdef int[::1] pj_tile = dyn['pj_tile']
    cdef double[::1] pj_price = dyn['pj_price']
    cdef double[::1] pj_capacity = dyn['pj_capacity']
    for s in range(n_base):
        sup_cap[s] = sup_capacity[sup_offset[good]+s]
    for s in range(n_pj):
        sup_cap[n_base+s] = pj_capacity[pj_offset[good]+s]

    # группы спроса: отсев по цене, вставка в порядке следования строк
    cdef int dq_idx = _find_pair(sup['dq_tile'], sup['dq_good'], sup['dq_count'],
                                 tile, good)
    if dq_idx < 0:
        free(cheap); free(sup_cap)
        raise ValueError('candidate support: demand rows (%d, %d) не упакованы'
                         % (tile, good))
    cdef int dq_start = sup['dq_offset'][dq_idx], dq_end = sup['dq_offset'][dq_idx+1]
    cdef int n_rows = dq_end-dq_start
    cdef int* g_part = <int*> malloc((n_rows if n_rows > 0 else 1)*sizeof(int))
    cdef int* g_tile = <int*> malloc((n_rows if n_rows > 0 else 1)*sizeof(int))
    cdef double* g_price = <double*> malloc((n_rows if n_rows > 0 else 1)*sizeof(double))
    cdef double* g_volume = <double*> malloc((n_rows if n_rows > 0 else 1)*sizeof(double))
    cdef int* g_row = <int*> malloc((n_rows if n_rows > 0 else 1)*sizeof(int))
    if g_part == NULL or g_tile == NULL or g_price == NULL or g_volume == NULL \
            or g_row == NULL:
        free(cheap); free(sup_cap); free(g_part); free(g_tile); free(g_price)
        free(g_volume); free(g_row)
        raise MemoryError()
    cdef int n_groups = 0, found
    cdef int[::1] dq_participant = sup['dq_participant']
    cdef int[::1] dq_order_tile = sup['dq_order_tile']
    cdef double[::1] dq_order_price = sup['dq_order_price']
    cdef double[::1] dq_limit = sup['dq_limit']
    cdef double[::1] dq_freight = sup['dq_freight']
    cdef double[::1] dq_remaining = sup['dq_remaining']
    cdef double[::1] dq_reserved = sup['dq_reserved']
    cdef double row_volume
    for i in range(dq_start, dq_end):
        if dq_limit[i]+eps < price:
            continue
        row_volume = min(dq_remaining[i],
                         dq_reserved[i]/max(price+dq_freight[i], eps))
        found = -1
        for j in range(n_groups):
            if g_part[j] == dq_participant[i] and g_tile[j] == dq_order_tile[i] \
                    and g_price[j] == dq_order_price[i]:
                found = j
                break
        if found < 0:
            found = n_groups
            g_part[n_groups] = dq_participant[i]
            g_tile[n_groups] = dq_order_tile[i]
            g_price[n_groups] = dq_order_price[i]
            g_row[n_groups] = i
            g_volume[n_groups] = 0.0
            n_groups += 1
        g_volume[found] += row_volume

    cdef int bk_start, bk_end
    cdef int[::1] bk_participant = sup['bk_participant']
    cdef double[::1] bk_remaining = sup['bk_remaining']
    cdef unsigned char[::1] bk_active = sup['bk_active']
    cdef double[::1] bk_price = sup['bk_price']
    cdef int[::1] bk_sellpos = sup['bk_sellpos']
    cdef double d, freight, landed_price
    cdef int supply_tile
    cdef double supply_price
    for j in range(n_groups):
        if g_part[j] == exclude:
            continue
        if dq_limit[g_row[j]]+eps < price:
            continue
        volume = g_volume[j]
        # дешёвые офферы покупателя группы
        _book_range(sup, g_tile[j], good, &bk_start, &bk_end)
        for i in range(bk_start, bk_end):
            if bk_participant[i] == g_part[j] or bk_remaining[i] <= eps \
                    or not bk_active[i]:
                continue
            if bk_price[i] > g_price[j]+eps:
                break
            pos = bk_sellpos[i]
            covered = min(volume, cheap[pos])
            cheap[pos] -= covered
            volume -= covered
        # поставщики
        for s in range(n_sup):
            if volume <= eps:
                break
            if sup_cap[s] <= eps:
                continue
            if s < n_base:
                supply_tile = sup_tile[sup_offset[good]+s]
                supply_price = sup_price[sup_offset[good]+s]
            else:
                supply_tile = pj_tile[pj_offset[good]+s-n_base]
                supply_price = pj_price[pj_offset[good]+s-n_base]
            d = distances[supply_tile, g_tile[j]]
            if isnan(d):
                continue
            freight = d*weight*rate
            landed_price = supply_price+freight
            if landed_price <= g_price[j]+eps:
                covered = min(volume, sup_cap[s])
                sup_cap[s] -= covered
                volume -= covered
        residual += volume

    free(cheap); free(sup_cap); free(g_part); free(g_tile); free(g_price)
    free(g_volume); free(g_row)
    return residual


cdef object _recipe_requirements_native(dict inv, int good, double quantity):
    """Точная копия recipe_requirements: DFS со стеком, += в порядке обхода."""
    cdef int MAXD = 32
    cdef int[::1] in_off = inv['recipe_input_offset']
    cdef int[::1] in_good = inv['recipe_input_good']
    cdef double[::1] in_value = inv['recipe_input_value']
    cdef int n_goods = len(in_off)-1
    cdef int* stack_good = <int*> malloc(1024*sizeof(int))
    cdef double* stack_amount = <double*> malloc(1024*sizeof(double))
    cdef int* stack_size = <int*> malloc(1024*sizeof(int))
    cdef int* stack_path = <int*> malloc(1024*MAXD*sizeof(int))
    cdef int* req_good = <int*> malloc(n_goods*sizeof(int))
    cdef double* req_value = <double*> malloc(n_goods*sizeof(double))
    cdef int* cur_path = <int*> malloc(MAXD*sizeof(int))
    if stack_good == NULL or stack_amount == NULL or stack_size == NULL \
            or stack_path == NULL or req_good == NULL or req_value == NULL \
            or cur_path == NULL:
        free(stack_good); free(stack_amount); free(stack_size); free(stack_path)
        free(req_good); free(req_value); free(cur_path)
        raise MemoryError()
    cdef int n_req = 0
    cdef int top = 0
    cdef int i, j, product, input_good, size, found
    cdef double amount, required
    cdef bint cyclic
    stack_good[0] = good
    stack_amount[0] = quantity
    stack_size[0] = 0
    top = 1
    while top > 0:
        top -= 1
        product = stack_good[top]
        amount = stack_amount[top]
        size = stack_size[top]
        for i in range(size):
            cur_path[i] = stack_path[top*MAXD+i]
        cyclic = False
        for i in range(size):
            if cur_path[i] == product:
                cyclic = True
                break
        if cyclic:
            free(stack_good); free(stack_amount); free(stack_size)
            free(stack_path); free(req_good); free(req_value); free(cur_path)
            raise ValueError('Цикл рецептов')
        for i in range(in_off[product], in_off[product+1]):
            input_good = in_good[i]
            required = amount*in_value[i]
            found = -1
            for j in range(n_req):
                if req_good[j] == input_good:
                    found = j
                    break
            if found < 0:
                req_good[n_req] = input_good
                req_value[n_req] = 0.0
                found = n_req
                n_req += 1
            req_value[found] += required
            stack_good[top] = input_good
            stack_amount[top] = required
            stack_size[top] = size+1
            for j in range(size):
                stack_path[top*MAXD+j] = cur_path[j]
            stack_path[top*MAXD+size] = product
            top += 1
    result = [(int(req_good[i]), req_value[i]) for i in range(n_req)]
    free(stack_good); free(stack_amount); free(stack_size); free(stack_path)
    free(req_good); free(req_value); free(cur_path)
    return result


def evaluate_project_candidate(dict inv, dict sup, dict dyn, int investor,
                               object rng, int derived_good, double derived_amount,
                               int demand_region_province, bint parent_present):
    """Чистый native project_candidate: один инвестор, без commit и мутаций.

    Возвращает candidate result, диагностику и trace. RNG: только profile и
    выбор resource tile — тем же rng-объектом в том же порядке, что V3.
    Packed state не мутируется.
    """
    cdef int i, j, pid, good, resource, start, end, rt_idx, n_tiles
    cdef double eps = inv['eps']
    cdef double minimum_capacity = inv['minimum_capacity']
    cdef double wage = inv['wage']
    cdef double overhead = inv['enterprise_overhead_rate']
    cdef double startup_cash = inv['startup_cash']
    cdef double payback = inv['investment_payback_months']
    cdef double extraction = inv['extraction_per_tile']
    cdef int month = inv['current_month']
    cdef int stall_months = inv['project_stall_months']
    cdef double[:, ::1] distances = sup['distances']
    cdef int[::1] account_state = inv['account_state']
    cdef int[::1] account_tile = inv['account_tile']
    cdef double[::1] account_balance = inv['account_balance']
    cdef double[::1] account_reserved = inv['account_reserved']
    cdef double[::1] account_protected = inv['account_protected']
    cdef double[::1] account_optimism = inv['account_optimism']
    cdef double[::1] account_risk_tolerance = inv['account_risk_tolerance']
    cdef double[:, ::1] account_stock = inv['account_stock']
    cdef double[::1] good_labor = inv['good_labor']
    cdef double[::1] good_weight = inv['good_weight']
    cdef double[::1] good_price = inv['good_price']
    cdef int[::1] good_resource = inv['good_resource']
    cdef int[::1] construction_good = inv['construction_good']
    cdef double[::1] construction_value = inv['construction_value']
    cdef int n_construction = len(construction_good)
    cdef int[::1] eq_off = inv['recipe_equipment_offset']
    cdef int[::1] eq_good = inv['recipe_equipment_good']
    cdef double[::1] eq_value = inv['recipe_equipment_value']
    cdef int[::1] loc_tile = sup['loc_tile']
    cdef int[::1] loc_state = sup['loc_state']
    cdef int[::1] mk_province = dyn['mk_province']
    cdef int[::1] mk_good = dyn['mk_good']
    cdef int mk_count = dyn['mk_count']
    cdef unsigned char[::1] al_mask = dyn['al_mask']
    cdef bint al_present = dyn['al_present']
    cdef bint exp_present = dyn['exp_present']
    cdef int exp_good = dyn['exp_good']
    cdef int exp_province = dyn['exp_province']
    cdef int exp_state = dyn['exp_state']
    cdef int exp_tile = dyn['exp_tile']
    cdef bint exp_city = dyn['exp_city_present']

    cdef int investor_state = account_state[investor]
    cdef int investor_tile = account_tile[investor]
    cdef double optimism = account_optimism[investor]
    cdef double risk = account_risk_tolerance[investor]

    # markets: упакованные отсортированные пары + derived
    cdef int* markets_pid = <int*> malloc((mk_count+1)*sizeof(int))
    cdef int* markets_good = <int*> malloc((mk_count+1)*sizeof(int))
    if markets_pid == NULL or markets_good == NULL:
        raise MemoryError()
    cdef int n_markets = mk_count
    for i in range(mk_count):
        markets_pid[i] = mk_province[i]
        markets_good[i] = mk_good[i]
    # derived-пара уже вставлена в dyn['mk_*'] harness'ом в строковом
    # порядке Python (sorted(markets)); native использует список как есть
    cdef bint derived_present = derived_good >= 0

    cdef int max_candidates = n_markets
    cdef double* c_capacity = <double*> malloc(max_candidates*sizeof(double))
    cdef double* c_price = <double*> malloc(max_candidates*sizeof(double))
    cdef double* c_profit = <double*> malloc(max_candidates*sizeof(double))
    cdef double* c_investment = <double*> malloc(max_candidates*sizeof(double))
    cdef double* c_unit_full = <double*> malloc(max_candidates*sizeof(double))
    cdef double* c_working = <double*> malloc(max_candidates*sizeof(double))
    cdef int* c_good = <int*> malloc(max_candidates*sizeof(int))
    cdef int* c_tile = <int*> malloc(max_candidates*sizeof(int))
    cdef int* c_pid = <int*> malloc(max_candidates*sizeof(int))
    cdef int* c_state = <int*> malloc(max_candidates*sizeof(int))
    cdef int* c_city = <int*> malloc(max_candidates*sizeof(int))
    cdef double* c_key = <double*> malloc(max_candidates*sizeof(double))
    if c_capacity == NULL or c_price == NULL or c_profit == NULL \
            or c_investment == NULL or c_unit_full == NULL or c_working == NULL \
            or c_good == NULL or c_tile == NULL or c_pid == NULL \
            or c_state == NULL or c_city == NULL or c_key == NULL:
        raise MemoryError()
    cdef int n_candidates = 0

    cdef int max_diag = n_markets+4
    cdef int* d_pid = <int*> malloc(max_diag*sizeof(int))
    cdef int* d_good = <int*> malloc(max_diag*sizeof(int))
    cdef int* d_stage = <int*> malloc(max_diag*sizeof(int))
    cdef double* d_v1 = <double*> malloc(max_diag*sizeof(double))
    cdef double* d_v2 = <double*> malloc(max_diag*sizeof(double))
    cdef double* d_v3 = <double*> malloc(max_diag*sizeof(double))
    cdef double* d_v4 = <double*> malloc(max_diag*sizeof(double))
    if d_pid == NULL or d_good == NULL or d_stage == NULL or d_v1 == NULL \
            or d_v2 == NULL or d_v3 == NULL or d_v4 == NULL:
        raise MemoryError()
    cdef int n_diag = 0

    cdef double demand, capacity, labor, used, limit, sig_price, price
    cdef double unit, fixed_unit, net, margin, distance
    cdef double material, delivery, equipment_capital, available, startup
    cdef double working, investment, profit, horizon, key
    cdef double best_key = -1.7976931348623157e308
    cdef int best = -1
    cdef double qc_value, qc_ceiling
    cdef double* terms = <double*> malloc(64*sizeof(double))
    if terms == NULL:
        raise MemoryError()
    cdef int loc_tile_idx, loc_state_idx, city_present
    cdef bint is_derived_market
    cdef int n_eq

    for j in range(n_markets):
        pid = markets_pid[j]
        good = markets_good[j]
        if al_present and not al_mask[good]:
            continue
        if exp_present and (good != exp_good or pid != exp_province):
            continue
        if exp_present:
            loc_state_idx = exp_state
            loc_tile_idx = exp_tile
            city_present = exp_city
        else:
            loc_state_idx = loc_state[pid]
            loc_tile_idx = loc_tile[pid]
            city_present = 0
        if loc_state_idx != investor_state:
            continue
        resource = good_resource[good]
        n_tiles = 0
        if resource >= 0:
            rt_idx = _find_triple(sup['rt_state'], sup['rt_province'],
                                  sup['rt_resource'], sup['rt_count'],
                                  investor_state, pid, resource)
            if rt_idx < 0:
                continue
            start = sup['rt_offset'][rt_idx]
            end = sup['rt_offset'][rt_idx+1]
            n_tiles = end-start
            if n_tiles == 0:
                continue
            if exp_present:
                loc_tile_idx = exp_tile
            else:
                order = list(range(n_tiles))
                rng.shuffle(order)
                loc_tile_idx = sup['rt_tile'][start+order[0]]
            city_present = 0

        limit = _customer_limit_native(sup, loc_tile_idx, good, investor)
        sig_price = _sig_field(sup, loc_state_idx, pid, good, 2)
        if limit != -1.0:
            price = limit
        elif sig_price != -1.0 and not isnan(sig_price) and sig_price != 0.0:
            price = sig_price
        else:
            price = good_price[good]

        demand = _investment_demand_native(inv, sup, dyn, loc_tile_idx, good,
                                           price, investor) \
            - account_stock[investor, good]
        if demand < 0:
            demand = 0.0
        is_derived_market = derived_present and good == derived_good
        if is_derived_market and derived_amount > demand:
            demand = derived_amount
        if demand < minimum_capacity:
            d_pid[n_diag] = pid; d_good[n_diag] = good; d_stage[n_diag] = 1
            d_v1[n_diag] = demand; d_v2[n_diag] = minimum_capacity
            d_v3[n_diag] = 0.0; d_v4[n_diag] = 0.0
            n_diag += 1
            continue
        if is_derived_market:
            capacity = min(demand, derived_amount)
        else:
            capacity = demand
        labor = _lookup2_or_zero(dyn['la_state'], dyn['la_province'],
                                 dyn['la_value'], dyn['la_count'],
                                 loc_state_idx, pid)
        labor -= _lookup2_or_zero(dyn['lr_state'], dyn['lr_province'],
                                  dyn['lr_value'], dyn['lr_count'],
                                  loc_state_idx, pid)
        capacity = min(capacity, labor/good_labor[good])
        if resource >= 0:
            used = _lookup2_or_zero(dyn['ru_province'], dyn['ru_resource'],
                                    dyn['ru_value'], dyn['ru_count'],
                                    pid, resource)
            used += _lookup2_or_zero(dyn['rr_province'], dyn['rr_resource'],
                                     dyn['rr_value'], dyn['rr_count'],
                                     pid, resource)
            capacity = min(capacity, max(0.0, n_tiles*extraction-used))
        if capacity < minimum_capacity:
            d_pid[n_diag] = pid; d_good[n_diag] = good; d_stage[n_diag] = 2
            d_v1[n_diag] = capacity; d_v2[n_diag] = minimum_capacity
            d_v3[n_diag] = 0.0; d_v4[n_diag] = 0.0
            n_diag += 1
            continue
        unit = _unit_cost_native(inv, sup, loc_tile_idx, good, investor)
        fixed_unit = good_labor[good]*wage*overhead
        net = _net_price_native(inv, sup, loc_tile_idx, good, price, investor)
        margin = net-unit-fixed_unit
        if margin <= eps:
            d_pid[n_diag] = pid; d_good[n_diag] = good; d_stage[n_diag] = 3
            d_v1[n_diag] = margin; d_v2[n_diag] = unit+fixed_unit; d_v3[n_diag] = net
            d_v4[n_diag] = 0.0
            n_diag += 1
            continue
        distance = distances[investor_tile, loc_tile_idx]
        if isnan(distance):
            d_pid[n_diag] = pid; d_good[n_diag] = good; d_stage[n_diag] = 4
            d_v1[n_diag] = 0.0; d_v2[n_diag] = 0.0; d_v3[n_diag] = 0.0
            d_v4[n_diag] = 0.0
            n_diag += 1
            continue
        # bill = construction_bill(1); material/delivery/equipment через quoted_cost
        for i in range(n_construction):
            terms[i] = (construction_value[i]*1.0/minimum_capacity) \
                *good_weight[construction_good[i]]
        delivery = _csum(terms, n_construction)*distance*inv['transport_rate']
        for i in range(n_construction):
            q = construction_value[i]*1.0/minimum_capacity
            _quoted_cost_native(inv, sup, investor_tile, construction_good[i],
                                investor, q, &qc_value, &qc_ceiling)
            terms[i] = q*qc_value/max(q, eps)
        material = _csum(terms, n_construction)
        n_eq = eq_off[good+1]-eq_off[good]
        for i in range(n_eq):
            _quoted_cost_native(inv, sup, investor_tile, eq_good[eq_off[good]+i],
                                investor, eq_value[eq_off[good]+i],
                                &qc_value, &qc_ceiling)
            terms[i] = eq_value[eq_off[good]+i]*qc_ceiling
        equipment_capital = _csum(terms, n_eq)
        available = account_balance[investor]-account_reserved[investor] \
            -account_protected[investor]
        if available < 0:
            available = 0.0
        startup = 0.0 if exp_present else startup_cash
        # project_finances
        capacity = min(capacity, max(0.0, available-startup)
                       /max(material+delivery+unit+equipment_capital, eps))
        working = capacity*equipment_capital
        if not exp_present:
            working += max(startup, capacity*(unit+fixed_unit))
        investment = capacity*(material+delivery)+working
        profit = capacity*margin
        if capacity < minimum_capacity:
            d_pid[n_diag] = pid; d_good[n_diag] = good; d_stage[n_diag] = 5
            d_v1[n_diag] = capacity; d_v2[n_diag] = minimum_capacity
            d_v3[n_diag] = 0.0; d_v4[n_diag] = 0.0
            n_diag += 1
            continue
        # profile: RNG только при отсутствии полей
        if isnan(optimism):
            optimism = rng.uniform(0.7, 1.3)
        if isnan(risk):
            risk = rng.uniform(0.1, 0.9)
        horizon = payback*(0.5+risk)
        if investment > available+eps or profit*horizon < investment:
            d_pid[n_diag] = pid; d_good[n_diag] = good; d_stage[n_diag] = 6
            d_v1[n_diag] = investment; d_v2[n_diag] = available
            d_v3[n_diag] = profit; d_v4[n_diag] = horizon
            n_diag += 1
            continue
        c_good[n_candidates] = good
        c_capacity[n_candidates] = capacity
        c_price[n_candidates] = price
        c_profit[n_candidates] = profit
        c_investment[n_candidates] = investment
        c_unit_full[n_candidates] = unit+fixed_unit
        c_working[n_candidates] = working
        c_tile[n_candidates] = loc_tile_idx
        c_pid[n_candidates] = pid
        c_state[n_candidates] = loc_state_idx
        c_city[n_candidates] = city_present
        c_key[n_candidates] = profit*payback-investment
        n_candidates += 1

    # max по ключу, первый максимум при равенстве
    for i in range(n_candidates):
        if best < 0 or c_key[i] > best_key:
            best = i
            best_key = c_key[i]

    cdef object chain = None
    if best >= 0:
        chain = _recipe_requirements_native(inv, c_good[best], c_capacity[best])

    result = dict(
        candidate_present=best >= 0,
        investor=investor,
        good=int(c_good[best]) if best >= 0 else -1,
        capacity=c_capacity[best] if best >= 0 else 0.0,
        expected_price=c_price[best] if best >= 0 else 0.0,
        expected_profit=c_profit[best] if best >= 0 else 0.0,
        expected_investment=c_investment[best] if best >= 0 else 0.0,
        expected_unit_cost=c_unit_full[best] if best >= 0 else 0.0,
        working_cash=c_working[best] if best >= 0 else 0.0,
        location_tile=int(c_tile[best]) if best >= 0 else -1,
        location_province=int(c_pid[best]) if best >= 0 else -1,
        location_state=int(c_state[best]) if best >= 0 else -1,
        city_present=bool(c_city[best]) if best >= 0 else False,
        expansion=bool(exp_present),
        derived_value=(derived_amount if (best >= 0 and derived_present
                                          and int(c_good[best]) == derived_good)
                       else None),
        dependency_expires=(month+stall_months) if parent_present else None,
        created=month,
        optimism=optimism,
        risk_tolerance=risk,
        chain=chain,
        candidate_count=n_candidates,
        diagnostics=[(int(d_pid[i]), int(d_good[i]), int(d_stage[i]),
                      d_v1[i], d_v2[i], d_v3[i], d_v4[i])
                     for i in range(n_diag)],
        candidates=[(int(c_pid[i]), int(c_good[i]), c_capacity[i], c_price[i],
                     c_profit[i], c_investment[i], c_working[i],
                     c_unit_full[i], int(c_tile[i]))
                    for i in range(n_candidates)],
    )

    free(markets_pid); free(markets_good)
    free(c_capacity); free(c_price); free(c_profit); free(c_investment)
    free(c_unit_full); free(c_working); free(c_good); free(c_tile); free(c_pid)
    free(c_state); free(c_city); free(c_key)
    free(d_pid); free(d_good); free(d_stage)
    free(d_v1); free(d_v2); free(d_v3); free(d_v4)
    free(terms)
    return result
