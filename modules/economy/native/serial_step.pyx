# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Техническая проверка сборки; игровая логика сюда ещё не перенесена."""


def weighted_sum(double[::1] values, double[::1] weights):
    """Проверить один вызов с двумя непрерывными массивами float64."""
    cdef Py_ssize_t i, size = values.shape[0]
    cdef double total = 0.0
    if size != weights.shape[0]:
        raise ValueError('Длины массивов должны совпадать')
    for i in range(size):
        total += values[i]*weights[i]
    return total


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
