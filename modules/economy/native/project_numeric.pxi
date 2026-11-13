# Числовые рабочие структуры. Dict допускается только в таблицах ID,
# именованных packed-полях и при материализации диагностического payload.

cdef class _FlowLedger:
    cdef public object data, present, order
    cdef double[::1] values
    cdef unsigned char[::1] seen
    cdef int[::1] sequence
    cdef public int count
    cdef int size, n_goods
    cdef object names

    def __init__(self, names):
        self.names = names
        self.n_goods = len(names['good_names'])
        self.size = len(names['state_names'])*self.n_goods
        self.data = np.zeros(self.size, dtype=np.float64)
        self.present = np.zeros(self.size, dtype=np.uint8)
        self.order = np.empty(self.size, dtype=np.int32)
        self.values = self.data
        self.seen = self.present
        self.sequence = self.order
        self.count = 0

    cdef void copy_from(self, _FlowLedger source) except *:
        cdef int i
        if self.size != source.size:
            raise ValueError('Несовместимые размеры flow ledger')
        for i in range(self.size):
            self.values[i] = source.values[i]
            self.seen[i] = source.seen[i]
        self.count = source.count
        for i in range(self.count):
            self.sequence[i] = source.sequence[i]

    cdef void add(self, int key, double quantity) noexcept:
        if not self.seen[key]:
            self.seen[key] = 1
            self.sequence[self.count] = key
            self.count += 1
        self.values[key] = self.values[key]+quantity

    def replace(self, _FlowLedger source):
        self.copy_from(source)

    def __setitem__(self, key, double value):
        cdef int k = (self.names['state_index'][key[0]]*self.n_goods
                      +self.names['good_index'][key[1]])
        if not self.seen[k]:
            self.seen[k] = 1
            self.sequence[self.count] = k
            self.count += 1
        self.values[k] = value

    def __getitem__(self, key):
        cdef int k = (self.names['state_index'][key[0]]*self.n_goods
                      +self.names['good_index'][key[1]])
        if not self.seen[k]:
            raise KeyError(key)
        return self.values[k]

    def keys(self):
        # Только экспорт журнала/trace; алгоритм работает с sequence напрямую.
        return [(self.names['state_names'][self.sequence[i]//self.n_goods],
                 self.names['good_names'][self.sequence[i]%self.n_goods])
                for i in range(self.count)]

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return self.count

    def items(self):
        return [(key, self[key]) for key in self.keys()]

    def __deepcopy__(self, memo):
        cdef _FlowLedger result = _FlowLedger(self.names)
        memo[id(self)] = result
        result.copy_from(self)
        return result


cdef class _ProjectFlowKernel:
    cdef public object owner
    cdef object names
    cdef int n_goods
    cdef int[::1] offsets, goods, path
    cdef double[::1] ratios, capacity, committed, rampable
    cdef unsigned char[::1] balance_ready
    cdef _FlowLedger base, trial, best

    def __init__(self, owner, names, inv):
        self.owner = owner
        self.names = names
        self.n_goods = len(names['good_names'])
        offsets, goods, ratios = [0], [], []
        for good in range(self.n_goods):
            edges = sorted(range(inv['recipe_input_offset'][good],
                                 inv['recipe_input_offset'][good+1]),
                           key=lambda edge: names['good_names'][inv['recipe_input_good'][edge]])
            for edge in edges:
                goods.append(inv['recipe_input_good'][edge])
                ratios.append(inv['recipe_input_value'][edge])
            offsets.append(len(goods))
        self.offsets = _int32(offsets)
        self.goods = _int32(goods)
        self.ratios = _float64(ratios)
        self.path = np.empty(self.n_goods+1, dtype=np.int32)
        size = len(names['state_names'])*self.n_goods
        self.capacity = np.zeros(size, dtype=np.float64)
        self.committed = np.zeros(size, dtype=np.float64)
        self.rampable = np.zeros(size, dtype=np.float64)
        self.balance_ready = np.zeros(size, dtype=np.uint8)
        self.base = _FlowLedger(names)
        self.trial = _FlowLedger(names)
        self.best = _FlowLedger(names)

    cdef void check_path(self, int good, int depth) except *:
        cdef int i
        for i in range(depth):
            if self.path[i] == good:
                raise ValueError('Цикл рецептов: '+' -> '.join(
                    [self.names['good_names'][self.path[j]] for j in range(depth)]
                    +[self.names['good_names'][good]]))

    cdef void ensure_balance(self, int state, int good) except *:
        cdef int key = state*self.n_goods+good
        if self.balance_ready[key]:
            return
        # Строка принадлежит выходной диагностике. Числовое ядро читает её
        # единожды, в тот же момент ленивого расчёта, что Python reference.
        row = self.owner.investment_flow_balance(
            self.names['state_names'][state], self.names['good_names'][good])
        self.capacity[key] = row['capacity_supply']
        self.committed[key] = row['committed']
        self.rampable[key] = row['rampable_free']
        self.balance_ready[key] = 1

    cdef double headroom(self, int state, int good, int depth) except? -1:
        cdef int edge, key = state*self.n_goods+good
        cdef double available, limit
        self.check_path(good, depth)
        self.ensure_balance(state, good)
        # Два вычитания именно в порядке V3; не объединять с committed.
        available = self.capacity[key]-self.committed[key]
        available = available-self.trial.values[key]
        if not available > 0:
            available = 0.0
        self.path[depth] = good
        for edge in range(self.offsets[good], self.offsets[good+1]):
            limit = self.headroom(state, self.goods[edge], depth+1)/self.ratios[edge]
            if limit < available:
                available = limit
        return available

    cdef bint reserve_flow(self, int state, int good, double quantity,
                           int depth) except -1:
        cdef int edge
        cdef double available = self.headroom(state, good, depth)
        if quantity > available+1e-7:
            return False
        self.path[depth] = good
        for edge in range(self.offsets[good], self.offsets[good+1]):
            if not self.reserve_flow(state, self.goods[edge], quantity*self.ratios[edge], depth+1):
                return False
        self.trial.add(state*self.n_goods+good, quantity)
        return True

    cpdef solve(self, int state, int good, double desired, _FlowLedger reserved):
        cdef int iteration, edge
        cdef bint feasible
        cdef double low = 0.0, high, middle, best = 0.0
        if not desired > 0:
            desired = 0.0
        high = desired
        self.base.copy_from(reserved)
        self.best.copy_from(self.base)
        for iteration in range(40):
            self.trial.copy_from(self.base)
            middle = (low+high)/2.0
            feasible = True
            self.path[0] = good
            for edge in range(self.offsets[good], self.offsets[good+1]):
                if not self.reserve_flow(state, self.goods[edge], middle*self.ratios[edge], 1):
                    feasible = False
                    break
            if feasible:
                low = middle
                best = middle
                self.best.copy_from(self.trial)
            else:
                high = middle
        return best, self.best

    cpdef double free_flow(self, int state, int good) except? -1:
        self.ensure_balance(state, good)
        return self.rampable[state*self.n_goods+good]


cdef class _UpstreamCache:
    cdef int[::1] goods, states, provinces, owners
    cdef double[::1] shortages
    cdef object projects, diagnostics
    cdef public int count, hits, last_slot
    cdef int capacity

    def __init__(self, int capacity):
        self.capacity = max(1, capacity)
        self.goods = np.empty(self.capacity, dtype=np.int32)
        self.states = np.empty(self.capacity, dtype=np.int32)
        self.provinces = np.empty(self.capacity, dtype=np.int32)
        self.owners = np.empty(self.capacity, dtype=np.int32)
        self.shortages = np.empty(self.capacity, dtype=np.float64)
        # Payload проектов и диагностик — ссылки на строки выходного журнала.
        self.projects = []
        self.diagnostics = []
        self.count = self.hits = 0
        self.last_slot = -1

    cpdef int lookup(self, int good, int state, double shortage, int province):
        cdef int i
        for i in range(self.count):
            if (self.goods[i] == good and self.states[i] == state
                    and self.shortages[i] == shortage and self.provinces[i] == province):
                self.hits += 1
                self.last_slot = i
                return i
        self.last_slot = -1
        return -1

    cpdef int store(self, int good, int state, double shortage, int province,
                    int owner, object project, object diagnostics):
        cdef int i = self.count
        if i == self.capacity:
            # В одном обзоре не больше одного поиска на инвестора.
            raise RuntimeError('Недостаточная ёмкость upstream cache')
        self.goods[i] = good
        self.states[i] = state
        self.shortages[i] = shortage
        self.provinces[i] = province
        self.owners[i] = owner
        self.projects.append(project)
        self.diagnostics.append(diagnostics)
        self.count += 1
        self.last_slot = i
        return i

    cpdef result(self, int slot, object accounts, object names):
        if self.owners[slot] < 0:
            return None
        return accounts[names['account_names'][self.owners[slot]]], self.projects[slot]

    def last_diagnostics(self):
        return self.diagnostics[self.last_slot] if self.last_slot >= 0 else []

    def materialize_diagnostics(self, names, previous):
        # Только выходной payload: этот dict больше не используется как кэш.
        if not self.count:
            return previous
        result = previous if previous is not None else {}
        for i in range(self.count):
            key = (names['good_names'][self.goods[i]], names['state_names'][self.states[i]],
                   self.shortages[i], names['province_names'][self.provinces[i]])
            result[key] = self.diagnostics[i]
        return result

    def __deepcopy__(self, memo):
        cdef _UpstreamCache result = _UpstreamCache(self.capacity)
        cdef int i
        memo[id(self)] = result
        for i in range(self.count):
            result.store(self.goods[i], self.states[i], self.shortages[i], self.provinces[i],
                self.owners[i], copy.deepcopy(self.projects[i], memo),
                copy.deepcopy(self.diagnostics[i], memo))
        result.hits = self.hits
        result.last_slot = self.last_slot
        return result


cdef class _FlowBalanceStore:
    """Числовой индекс ленивых строк диагностики вместо dict-кэша."""
    cdef object names, rows
    cdef unsigned char[::1] ready
    cdef int n_goods, count

    def __init__(self, names):
        self.names = names
        self.n_goods = len(names['good_names'])
        size = len(names['state_names'])*self.n_goods
        self.rows = [None]*size
        self.ready = np.zeros(size, dtype=np.uint8)
        self.count = 0

    cdef int index(self, object key) except -1:
        return self.names['state_index'][key[0]]*self.n_goods+self.names['good_index'][key[1]]

    def __contains__(self, key):
        return bool(self.ready[self.index(key)])

    def __getitem__(self, key):
        cdef int k = self.index(key)
        if not self.ready[k]:
            raise KeyError(key)
        return self.rows[k]

    def __setitem__(self, key, row):
        cdef int k = self.index(key)
        if not self.ready[k]:
            self.count += 1
        self.ready[k] = 1
        self.rows[k] = row

    def get(self, key, default=None):
        cdef int k = self.index(key)
        return self.rows[k] if self.ready[k] else default

    def __len__(self):
        return self.count

    def __deepcopy__(self, memo):
        # Совместимый диагностический snapshot, не рабочий кэш расчёта.
        result = {}
        memo[id(self)] = result
        for k in range(len(self.rows)):
            if self.ready[k]:
                result[self.names['state_names'][k//self.n_goods],
                       self.names['good_names'][k%self.n_goods]] = copy.deepcopy(self.rows[k], memo)
        return result


cdef class _ProjectRecipes:
    cdef int n_goods
    cdef int[::1] offsets, goods
    cdef double[::1] ratios
    cdef unsigned char[:, ::1] affected
    cdef object names

    def __init__(self, names, inv):
        cdef int good, edge, source, parent, intermediate
        self.names = names
        self.offsets = inv['recipe_input_offset']
        self.goods = inv['recipe_input_good']
        self.ratios = inv['recipe_input_value']
        self.n_goods = len(names['good_names'])
        self.affected = np.eye(self.n_goods, dtype=np.uint8)
        for parent in range(self.n_goods):
            for edge in range(self.offsets[parent], self.offsets[parent+1]):
                self.affected[self.goods[edge], parent] = 1
        for intermediate in range(self.n_goods):
            for source in range(self.n_goods):
                if self.affected[source, intermediate]:
                    for parent in range(self.n_goods):
                        if self.affected[intermediate, parent]:
                            self.affected[source, parent] = 1

    def affected_names(self, int good):
        return [self.names['good_names'][i] for i in range(self.n_goods) if self.affected[good, i]]

    def requirements(self, int good, double quantity):
        cdef int capacity = len(self.goods)+1
        cdef int[::1] stack = np.empty(capacity, dtype=np.int32)
        cdef double[::1] amounts = np.empty(capacity, dtype=np.float64)
        cdef int[::1] depths = np.empty(capacity, dtype=np.int32)
        cdef int[:, ::1] paths = np.empty((capacity, self.n_goods+1), dtype=np.int32)
        cdef int[::1] path = np.empty(self.n_goods+1, dtype=np.int32)
        cdef double[::1] values = np.zeros(self.n_goods, dtype=np.float64)
        cdef unsigned char[::1] seen = np.zeros(self.n_goods, dtype=np.uint8)
        cdef int[::1] order = np.empty(self.n_goods, dtype=np.int32)
        cdef int top = 1, count = 0, product, child, depth, edge, i
        cdef double amount, required
        stack[0] = good
        amounts[0] = quantity
        depths[0] = 0
        while top:
            top -= 1
            product = stack[top]
            amount = amounts[top]
            depth = depths[top]
            for i in range(depth):
                path[i] = paths[top, i]
                if path[i] == product:
                    raise ValueError('Цикл рецептов: '+' -> '.join(
                        [self.names['good_names'][paths[top, j]] for j in range(depth)]
                        +[self.names['good_names'][product]]))
            for edge in range(self.offsets[product], self.offsets[product+1]):
                child = self.goods[edge]
                required = amount*self.ratios[edge]
                if not seen[child]:
                    seen[child] = 1
                    order[count] = child
                    count += 1
                values[child] = values[child]+required
                stack[top] = child
                amounts[top] = required
                depths[top] = depth+1
                for i in range(depth):
                    paths[top, i] = path[i]
                paths[top, depth] = product
                top += 1
        # Материализация поля chain_requirements выходного проекта.
        return {self.names['good_names'][order[i]]: values[order[i]] for i in range(count)}


cdef class _ExpansionFlows:
    cdef _ProjectFlowKernel kernel
    cdef object names, parents
    cdef int n_goods
    cdef double[::1] free, required, covered
    cdef unsigned char[::1] ready, present

    def __init__(self, _ProjectFlowKernel kernel, names):
        self.kernel = kernel
        self.names = names
        self.n_goods = len(names['good_names'])
        size = len(names['state_names'])*self.n_goods
        self.free = np.zeros(size, dtype=np.float64)
        self.required = np.zeros(size, dtype=np.float64)
        self.covered = np.zeros(size, dtype=np.float64)
        self.ready = np.zeros(size, dtype=np.uint8)
        self.present = np.zeros(size, dtype=np.uint8)
        self.parents = [None]*size

    cdef void require(self, int state, int product, double quantity,
                      object decision_id, int parent, int depth) except *:
        cdef int edge, child, key
        cdef double required, covered, missing
        self.kernel.check_path(product, depth)
        self.kernel.path[depth] = product
        for edge in range(self.kernel.offsets[product], self.kernel.offsets[product+1]):
            child = self.kernel.goods[edge]
            key = state*self.n_goods+child
            required = quantity*self.kernel.ratios[edge]
            if not self.ready[key]:
                self.free[key] = self.kernel.free_flow(state, child)
                self.ready[key] = 1
            covered = min(required, self.free[key])
            self.free[key] = self.free[key]-covered
            missing = required-covered
            if missing <= 1e-7:
                continue
            if not self.present[key]:
                self.present[key] = 1
                self.parents[key] = []
            self.required[key] = self.required[key]+missing
            self.covered[key] = self.covered[key]+covered
            # Это уже строки выходного журнала, не таблица для расчёта.
            self.parents[key].append(dict(decision_id=decision_id,
                parent_good=self.names['good_names'][parent], required=required,
                covered=covered, missing=missing))
            self.require(state, child, missing, decision_id, child, depth+1)

    def add(self, int state, int good, double quantity, decision_id):
        self.require(state, good, quantity, decision_id, good, 0)

    def rows(self):
        keys = sorted((k for k in range(len(self.present)) if self.present[k]),
            key=lambda k: (self.names['state_names'][k//self.n_goods],
                           self.names['good_names'][k%self.n_goods]))
        return [dict(state_id=self.names['state_names'][key//self.n_goods],
            good=self.names['good_names'][key%self.n_goods],
            required_flow=self.required[key], covered_by_free_flow=self.covered[key],
            parents=self.parents[key]) for key in keys]
