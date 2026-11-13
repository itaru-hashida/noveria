# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False
"""Persistent native state участка update_prices.

Файл подключается в serial_step.pyx и делит с остальными native-фазами общее
пространство имён модуля.

Состояние живёт на движке между ходами и хранит:

- стабильные массивы: индексы товаров, площадок, государств и провинций,
  рецепты, матрица расстояний, канонический порядок счетов и предприятий;
- numpy-буферы всего flat pack'а update_prices.

Стабильные значения пересобираются только при смене подписи карты, товаров
или индексации. Динамические значения (цены, склады, заявки, резервы)
перезаписываются в те же буферы каждый ход. Все заполнения только копируют
исходные значения в исходном порядке, поэтому арифметика и порядок
float-операций не меняются.
"""

import numpy as _state_np


cdef inline object _state_buffer(dict store, object key, tuple shape, object dtype,
                                 object fill):
    """Буфер нужной формы; пересоздаётся только при смене формы или dtype."""
    cdef object value = store.get(key)
    if value is not None:
        if (<object>value).shape == shape and (<object>value).dtype == dtype:
            if fill is not None:
                (<object>value).fill(fill)
            return value
    if fill is None:
        value = _state_np.empty(shape, dtype=dtype)
    elif fill == 0:
        value = _state_np.zeros(shape, dtype=dtype)
    else:
        value = _state_np.full(shape, fill, dtype=dtype)
    store[key] = value
    return value


cdef class NativeUpdateState:
    """Промежуточное native-состояние update_prices, живущее на движке."""

    cdef:
        dict _data
        dict _static
        public tuple layout_key

    def __cinit__(self):
        self._data = {}
        self._static = {}
        self.layout_key = None

    def __deepcopy__(self, memo):
        # Копия мира получает собственное пустое состояние: буферы соберутся
        # заново из тех же Python-данных, поэтому значения не зависят от копии.
        return NativeUpdateState()

    def __reduce__(self):
        return (NativeUpdateState, ())

    property data:
        def __get__(self):
            return self._data

    property static:
        def __get__(self):
            return self._static

    def reset(self):
        """Полный сброс: следующий pack соберёт состояние с нуля."""
        self._data = {}
        self._static = {}
        self.layout_key = None

    def buffer(self, key, shape, dtype, fill=None):
        """Буфер по ключу: форма, dtype и начальное значение."""
        return _state_buffer(self._data, key, tuple(shape), dtype, fill)

    # --- цены: субъекты, спрос, заявки, рынок ------------------------------

    cpdef void fill_prices(self, list subjects, list accounts, list enterprises,
                           dict good_index, dict tile_index, dict state_index,
                           dict province_index):
        """Плоские строки ценовых субъектов в порядке V3."""
        cdef Py_ssize_t n = len(subjects), i
        cdef int[::1] subject_enterprise = _state_buffer(
            self._data, 'subject_enterprise', (n,), _state_np.int32, 0)
        cdef int[::1] subject_account = _state_buffer(
            self._data, 'subject_account', (n,), _state_np.int32, 0)
        cdef int[::1] subject_good = _state_buffer(
            self._data, 'subject_good', (n,), _state_np.int32, 0)
        cdef int[::1] subject_tile = _state_buffer(
            self._data, 'subject_tile', (n,), _state_np.int32, 0)
        cdef int[::1] subject_state = _state_buffer(
            self._data, 'subject_state', (n,), _state_np.int32, 0)
        cdef int[::1] subject_province = _state_buffer(
            self._data, 'subject_province', (n,), _state_np.int32, 0)
        cdef double[::1] subject_price = _state_buffer(
            self._data, 'subject_price', (n,), _state_np.float64, 0)
        cdef double[::1] subject_last = _state_buffer(
            self._data, 'subject_last_sold', (n,), _state_np.float64, 0)
        cdef double[::1] subject_previous = _state_buffer(
            self._data, 'subject_previous_sold', (n,), _state_np.float64, 0)
        cdef int[::1] subject_history_count = _state_buffer(
            self._data, 'subject_history_count', (n,), _state_np.int32, 0)
        cdef unsigned char[::1] subject_is_stock = _state_buffer(
            self._data, 'subject_is_stock', (n,), _state_np.uint8, 0)
        cdef tuple row
        cdef int enterprise, account
        cdef dict a
        cdef list history
        cdef object market_tile
        for i in range(n):
            row = subjects[i]
            enterprise = row[0]
            account = row[1]
            a = accounts[account]
            subject_enterprise[i] = enterprise
            subject_account[i] = account
            subject_good[i] = good_index[row[2]]
            if enterprise >= 0:
                market_tile = enterprises[enterprise]['tile_id']
            else:
                market_tile = a.get('cell_id') or a['tile_id']
            subject_tile[i] = tile_index[market_tile]
            subject_state[i] = state_index[a['state_id']]
            subject_province[i] = province_index[a['province_id']]
            subject_price[i] = row[3]
            history = row[4]
            # Отрицательные индексы под wraparound=False недопустимы: длина
            # списка проверяется явно, а позиции считаются от начала.
            if history:
                subject_last[i] = history[len(history)-1]['sold']
            else:
                subject_last[i] = 0.0
            if len(history) > 1:
                subject_previous[i] = history[len(history)-2]['sold']
            else:
                subject_previous[i] = 0.0
            subject_history_count[i] = len(history)
            subject_is_stock[i] = 1 if row[5] else 0

    cpdef void fill_demand(self, list orders, dict good_index,
                           dict account_index, dict tile_index,
                           Py_ssize_t capacity):
        """Девять плоских полей buy-заявок и нулевой хвост до capacity."""
        cdef Py_ssize_t n = len(orders), i
        cdef int[::1] good = _state_buffer(
            self._data, 'demand_order_good', (capacity,), _state_np.int32, 0)
        cdef int[::1] participant = _state_buffer(
            self._data, 'demand_order_participant', (capacity,), _state_np.int32, 0)
        cdef int[::1] tile = _state_buffer(
            self._data, 'demand_order_tile', (capacity,), _state_np.int32, 0)
        cdef double[::1] price = _state_buffer(
            self._data, 'demand_order_price', (capacity,), _state_np.float64, 0)
        cdef double[::1] remaining = _state_buffer(
            self._data, 'demand_order_remaining', (capacity,), _state_np.float64, 0)
        cdef double[::1] reserved = _state_buffer(
            self._data, 'demand_order_reserved', (capacity,), _state_np.float64, 0)
        cdef unsigned char[::1] active = _state_buffer(
            self._data, 'demand_order_active', (capacity,), _state_np.uint8, 0)
        cdef unsigned char[::1] final = _state_buffer(
            self._data, 'demand_order_final', (capacity,), _state_np.uint8, 0)
        cdef unsigned char[::1] construction = _state_buffer(
            self._data, 'demand_order_construction', (capacity,), _state_np.uint8, 0)
        cdef dict o
        cdef object status, purpose
        for i in range(n):
            o = orders[i]
            good[i] = good_index[o['good']]
            participant[i] = account_index[o['participant']]
            tile[i] = tile_index[o['tile_id']]
            price[i] = o.get('price', 0)
            remaining[i] = o.get('remaining', 0)
            reserved[i] = o.get('reserved', 0)
            status = o['status']
            active[i] = 0 if status == 'cancelled' or status == 'expired' else 1
            final[i] = 1 if o.get('final_consumption', False) else 0
            purpose = o['purpose']
            construction[i] = 1 if purpose == 'Строительные материалы' else 0
        for i in range(n, capacity):
            good[i] = 0
            participant[i] = 0
            tile[i] = 0
            price[i] = 0.0
            remaining[i] = 0.0
            reserved[i] = 0.0
            active[i] = 0
            final[i] = 0
            construction[i] = 0

    cpdef void fill_public(self, list orders, dict good_index, dict tile_index,
                           dict account_index):
        """Поля public buy-заявок из meta."""
        cdef Py_ssize_t n = len(orders), i
        cdef int[::1] good = _state_buffer(
            self._data, 'order_good', (n,), _state_np.int32, 0)
        cdef int[::1] tile = _state_buffer(
            self._data, 'order_tile', (n,), _state_np.int32, 0)
        cdef int[::1] participant = _state_buffer(
            self._data, 'order_participant', (n,), _state_np.int32, 0)
        cdef double[::1] price = _state_buffer(
            self._data, 'order_price', (n,), _state_np.float64, 0)
        cdef double[::1] remaining = _state_buffer(
            self._data, 'order_remaining', (n,), _state_np.float64, 0)
        cdef unsigned char[::1] active = _state_buffer(
            self._data, 'order_active', (n,), _state_np.uint8, 0)
        cdef dict o
        cdef object status
        for i in range(n):
            o = orders[i]
            good[i] = good_index[o['good']]
            tile[i] = tile_index[o['tile_id']]
            participant[i] = account_index[o['participant']]
            price[i] = o['price']
            remaining[i] = o['remaining']
            status = o['status']
            active[i] = 0 if status == 'cancelled' or status == 'expired' else 1

    cpdef void fill_feedback(self, list feedback, dict good_index, dict tile_index):
        """Необеспеченные потребности, по которым V3 двигает цену вниз."""
        cdef Py_ssize_t n = len(feedback), i
        cdef int[::1] good = _state_buffer(
            self._data, 'feedback_good', (n,), _state_np.int32, 0)
        cdef int[::1] tile = _state_buffer(
            self._data, 'feedback_tile', (n,), _state_np.int32, 0)
        cdef double[::1] budget = _state_buffer(
            self._data, 'feedback_budget', (n,), _state_np.float64, 0)
        cdef dict o
        for i in range(n):
            o = feedback[i]
            good[i] = good_index[o['good']]
            tile[i] = tile_index[o['tile_id']]
            budget[i] = o['budget_price']

    cpdef void fill_market(self, list rows, dict state_index,
                           dict province_index, dict good_index):
        """Плоская история рынка в порядке месяцев и строк."""
        cdef Py_ssize_t n = len(rows), i
        cdef int[::1] state = _state_buffer(
            self._data, 'market_state', (n,), _state_np.int32, 0)
        cdef int[::1] province = _state_buffer(
            self._data, 'market_province', (n,), _state_np.int32, 0)
        cdef int[::1] good = _state_buffer(
            self._data, 'market_good', (n,), _state_np.int32, 0)
        cdef double[::1] volume = _state_buffer(
            self._data, 'market_volume', (n,), _state_np.float64, 0)
        cdef double[::1] landed = _state_buffer(
            self._data, 'market_landed_component', (n,), _state_np.float64, 0)
        cdef dict row
        for i in range(n):
            row = rows[i]
            state[i] = state_index[row['state_id']]
            province[i] = province_index[row['province_id']]
            good[i] = good_index[row['good']]
            volume[i] = row['volume']
            landed[i] = row['trade_gold']+row['freight']

    # --- предприятия -------------------------------------------------------

    cpdef void fill_enterprises(self, list enterprises, dict good_index,
                                dict account_index, dict tile_index,
                                trades_by_seller, double initial_fraction,
                                dict goods, set available):
        """Индексные, скалярные и исторические поля предприятий.

        ``available`` — множество пар (провинция, ресурс) карты: оно меняется
        только вместе с картой, поэтому живёт в стабильной части состояния.
        """
        cdef Py_ssize_t n = len(enterprises), i, k, count, start
        cdef int[::1] enterprise_good = _state_buffer(
            self._data, 'enterprise_good', (n,), _state_np.int32, 0)
        cdef int[::1] enterprise_tile = _state_buffer(
            self._data, 'enterprise_tile', (n,), _state_np.int32, 0)
        cdef int[::1] owner = _state_buffer(
            self._data, 'enterprise_owner', (n,), _state_np.int32, 0)
        cdef int[::1] account = _state_buffer(
            self._data, 'enterprise_account', (n,), _state_np.int32, 0)
        cdef int[::1] available_from = _state_buffer(
            self._data, 'enterprise_available_from', (n,), _state_np.int32, 0)
        cdef int[::1] history_count = _state_buffer(
            self._data, 'enterprise_history_count', (n,), _state_np.int32, 0)
        cdef int[::1] trades_version = _state_buffer(
            self._data, 'enterprise_trades_version', (n,), _state_np.int32, 0)
        cdef double[::1] price = _state_buffer(
            self._data, 'enterprise_price', (n,), _state_np.float64, 0)
        cdef double[::1] capacity = _state_buffer(
            self._data, 'enterprise_capacity', (n,), _state_np.float64, 0)
        cdef double[::1] planned = _state_buffer(
            self._data, 'enterprise_planned', (n,), _state_np.float64, 0)
        cdef double[::1] recent = _state_buffer(
            self._data, 'enterprise_recent', (n,), _state_np.float64, 0)
        cdef double[::1] forecast = _state_buffer(
            self._data, 'enterprise_sales_forecast', (n,), _state_np.float64, 0)
        cdef double[::1] previous_output = _state_buffer(
            self._data, 'enterprise_previous_output', (n,), _state_np.float64, 0)
        cdef double[:, ::1] history_sold = _state_buffer(
            self._data, 'enterprise_history_sold', (n, 3), _state_np.float64, 0)
        cdef double[:, ::1] history_output = _state_buffer(
            self._data, 'enterprise_history_output', (n, 3), _state_np.float64, 0)
        cdef unsigned char[:, ::1] history_limited = _state_buffer(
            self._data, 'enterprise_history_input_limited', (n, 3),
            _state_np.uint8, 0)
        cdef unsigned char[::1] closed = _state_buffer(
            self._data, 'enterprise_closed', (n,), _state_np.uint8, 0)
        cdef unsigned char[::1] suspended = _state_buffer(
            self._data, 'enterprise_suspended', (n,), _state_np.uint8, 0)
        cdef unsigned char[::1] had_price = _state_buffer(
            self._data, 'recovery_had_price', (n,), _state_np.uint8, 0)
        cdef unsigned char[::1] resource_ok = _state_buffer(
            self._data, 'enterprise_resource_ok', (n,), _state_np.uint8, 0)
        cdef dict e, row, recipe
        cdef list history
        cdef object good, status, resource
        cdef double best, candidate, default
        cdef bint found
        for i in range(n):
            e = enterprises[i]
            good = e['good']
            enterprise_good[i] = good_index[good]
            enterprise_tile[i] = tile_index[e['tile_id']]
            owner[i] = account_index[e['owner_id']]
            account[i] = account_index[e['id']]
            available_from[i] = e['available_from']
            price[i] = e['price']
            capacity[i] = e['capacity']
            planned[i] = e.get('production_plan', {}).get('output', 0)
            forecast[i] = e['sales_forecast']
            previous_output[i] = e.get('planned_output', 0)
            status = e['status']
            closed[i] = 1 if status == 'closed' else 0
            suspended[i] = 1 if e.get('suspended') else 0
            had_price[i] = 1 if 'production_price' in e else 0
            trades_version[i] = trades_by_seller.get(e['id'], 0)
            recipe = goods[good]
            resource = recipe['resource']
            resource_ok[i] = 1 if (not resource
                                   or (e['province_id'], resource) in available) else 0
            history = e.get('sales_history', [])
            count = len(history)
            if count > 3:
                start = count-3
                count = 3
            else:
                start = 0
            history_count[i] = count
            for k in range(count):
                row = history[start+k]
                history_sold[i, k] = row.get('sold', 0)
                history_output[i, k] = row.get('output', 0)
                history_limited[i, k] = 1 if row.get('input_limited') else 0
            for k in range(count, 3):
                history_sold[i, k] = 0.0
                history_output[i, k] = 0.0
                history_limited[i, k] = 0
            # default=capacity*initial_fraction вычисляется в V3 всегда, даже
            # при непустой истории: это самое правое слагаемое выражения.
            # Сам max берёт максимум по истории, а default — лишь при пустой.
            best = e['capacity']*initial_fraction
            if count:
                best = history_output[i, 0]
                for k in range(1, count):
                    candidate = history_output[i, k]
                    if candidate > best:
                        best = candidate
            recent[i] = best

    cpdef void fill_enterprise_stock(self, Py_ssize_t good_count):
        """Склад предприятия — строка его же счёта в account_stock."""
        cdef Py_ssize_t n = self._data['enterprise_account'].shape[0], i, k
        cdef int[::1] account = self._data['enterprise_account']
        cdef double[:, ::1] stock = self._data['account_stock']
        cdef double[:, ::1] enterprise_stock = _state_buffer(
            self._data, 'enterprise_stock', (n, good_count), _state_np.float64, 0)
        for i in range(n):
            for k in range(good_count):
                enterprise_stock[i, k] = stock[account[i], k]

    # --- счета -------------------------------------------------------------

    cpdef void fill_accounts(self, list accounts, dict enterprise_index,
                             dict good_index, dict state_index,
                             dict province_index, dict tile_index,
                             dict tiles, dict entities):
        """Индексные, денежные, складские поля счетов и закреплённый товар."""
        cdef Py_ssize_t n = len(accounts), i, good_value
        cdef Py_ssize_t goods_count = len(good_index)
        cdef int[::1] enterprise = _state_buffer(
            self._data, 'account_enterprise', (n,), _state_np.int32, 0)
        cdef unsigned char[::1] household = _state_buffer(
            self._data, 'account_household', (n,), _state_np.uint8, 0)
        cdef int[::1] state = _state_buffer(
            self._data, 'account_state', (n,), _state_np.int32, 0)
        cdef int[::1] province = _state_buffer(
            self._data, 'account_province', (n,), _state_np.int32, 0)
        cdef int[::1] tile = _state_buffer(
            self._data, 'account_tile', (n,), _state_np.int32, 0)
        cdef double[::1] balance = _state_buffer(
            self._data, 'account_balance', (n,), _state_np.float64, 0)
        cdef double[::1] reserved = _state_buffer(
            self._data, 'account_money_reserved', (n,), _state_np.float64, 0)
        cdef double[::1] protected = _state_buffer(
            self._data, 'account_protected', (n,), _state_np.float64, 0)
        cdef double[::1] population = _state_buffer(
            self._data, 'account_population', (n,), _state_np.float64, 0)
        cdef double[:, ::1] stock = _state_buffer(
            self._data, 'account_stock', (n, goods_count), _state_np.float64, 0)
        cdef double[:, ::1] stock_reserved = _state_buffer(
            self._data, 'account_stock_reserved', (n, goods_count),
            _state_np.float64, 0)
        cdef double[:, ::1] locked = _state_buffer(
            self._data, 'account_locked', (n, goods_count), _state_np.float64, 0)
        cdef dict a, project
        cdef object market_tile, market_province, good, value
        for i in range(n):
            a = accounts[i]
            enterprise[i] = enterprise_index.get(a['id'], -1)
            household[i] = 1 if a['kind'] == 'household' else 0
            state[i] = state_index[a['state_id']]
            market_tile = a.get('cell_id') or a['tile_id']
            tile[i] = tile_index[market_tile]
            market_province = tiles.get(market_tile, {}).get(
                'province_id', a['province_id'])
            province[i] = province_index[market_province]
            balance[i] = a['balance']
            reserved[i] = a.get('reserved', 0)
            protected[i] = a.get('protected', 0)
            population[i] = 0
            if a['kind'] == 'household':
                population[i] = entities[a['group_id']]['properties']['population']
            for good, value in a['stock'].items():
                stock[i, <Py_ssize_t>good_index[good]] = value
            for good, value in a.get('stock_reserved', {}).items():
                stock_reserved[i, <Py_ssize_t>good_index[good]] = value
            # Закреплённый товар: тот же порядок сложения, что в
            # locked_material() V3 — проект счёта, затем бакеты публичных
            # проектов в порядке списка.
            for good in sorted(a['stock']):
                good_value = good_index[good]
                value = a.get('project', {}).get('materials', {}).get(good, 0)
                for project in a.get('public_projects', []):
                    value = value+project.get('materials', {}).get(good, 0)
                    value = value+project.get('equipment_held', {}).get(good, 0)
                    value = value+project.get('support_stock', {}).get(good, 0)
                locked[i, good_value] = value

    cpdef void fill_sells(self, list accounts, dict enterprises, dict goods,
                          dict good_index, dict subject_by_pair,
                          Py_ssize_t capacity):
        """Строки продаж в порядке аккаунтов и sorted(stock) внутри счёта."""
        cdef Py_ssize_t n = len(accounts), i, position = 0, good_value
        cdef int[::1] sell_good = _state_buffer(
            self._data, 'sell_good', (capacity,), _state_np.int32, 0)
        cdef int[::1] sell_subject = _state_buffer(
            self._data, 'sell_subject', (capacity,), _state_np.int32, 0)
        cdef int[::1] sell_offset = _state_buffer(
            self._data, 'sell_offset', (n+1,), _state_np.int32, 0)
        cdef double[::1] sell_ask_price = _state_buffer(
            self._data, 'sell_ask_price', (capacity,), _state_np.float64, 0)
        cdef unsigned char[::1] sell_ask_present = _state_buffer(
            self._data, 'sell_ask_present', (capacity,), _state_np.uint8, 0)
        cdef double[::1] sell_input_ratio = _state_buffer(
            self._data, 'sell_input_ratio', (capacity,), _state_np.float64, 0)
        cdef double[::1] sell_equipment_ratio = _state_buffer(
            self._data, 'sell_equipment_ratio', (capacity,), _state_np.float64, 0)
        cdef dict a, ask, recipe
        cdef object source, good
        cdef bint is_output
        sell_offset[0] = 0
        for i in range(n):
            a = accounts[i]
            source = enterprises.get(a['id'])
            recipe = goods[source['good']] if source is not None else None
            ask = a.get('ask_prices', {})
            for good in sorted(a['stock']):
                good_value = good_index[good]
                sell_good[position] = good_value
                is_output = source is not None and good == source['good']
                sell_ask_price[position] = ask.get(good, goods[good]['price'])
                sell_ask_present[position] = 0 if (is_output or good not in ask) else 1
                sell_subject[position] = subject_by_pair.get((i, good_value), -1)
                if recipe is None:
                    sell_input_ratio[position] = 0
                    sell_equipment_ratio[position] = 0
                else:
                    sell_input_ratio[position] = recipe['inputs'].get(good, 0)
                    sell_equipment_ratio[position] = recipe.get('equipment', {}).get(good, 0)
                position += 1
            sell_offset[i+1] = position
