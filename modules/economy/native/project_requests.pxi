# Решения и их последовательный commit выполняются без доступа к engine.
# Строковые ID и dict проектов принадлежат журналу материализации.
import copy
import hashlib
import numpy as np

_int32 = lambda values: np.asarray(values, dtype=np.int32)
_float64 = lambda values: np.asarray(values, dtype=np.float64)
_uint8 = lambda values: np.asarray(values, dtype=np.uint8)
EPS = 1e-7

include "project_numeric.pxi"


class _ReservationLedger:
    """Плотные числовые резервы; keys сохраняет порядок касаний defaultdict."""

    def __init__(self, keys, dtype=np.float64, values=None):
        self.index = {key: i for i, key in enumerate(keys)}
        self.data = np.zeros(len(self.index), dtype=dtype)
        self.present = np.zeros(len(self.index), dtype=np.uint8)
        self.order = []
        if values:
            for key, value in values.items():
                self[key] = value

    def replace(self, values):
        self.data.fill(0)
        self.present.fill(0)
        self.order.clear()
        for key, value in values.items():
            self[key] = value

    def __getitem__(self, key):
        i = self.index[key]
        if not self.present[i]:
            self.present[i] = 1
            self.order.append(key)
        return self.data[i].item()

    def __setitem__(self, key, value):
        i = self.index[key]
        if not self.present[i]:
            self.present[i] = 1
            self.order.append(key)
        self.data[i] = value

    def __iter__(self):
        return iter(self.order)

    def __len__(self):
        return len(self.order)

    def keys(self):
        return self.order

    def items(self):
        return [(key, self[key]) for key in self.order]

    def get(self, key, default=None):
        i = self.index.get(key)
        return self.data[i].item() if i is not None and self.present[i] else default


class _ProjectBatch:
    def __init__(self, inv, support, names, snapshot, rng):
        self.inv = inv
        self.support = support
        support['batch'] = self
        self.suppliers_initialized = np.zeros(len(inv['good_labor']), dtype=np.uint8)
        self.names = names
        self.rng = rng
        self.accounts = snapshot['accounts']
        self.enterprises = snapshot['enterprises']
        self.goods = snapshot['goods']
        self.cfg = snapshot['cfg']
        self.tiles = snapshot['tiles']
        self.owners = snapshot['owners']
        self.meta = snapshot['meta']
        self.report = snapshot['report']
        self.initial_state = snapshot['state']
        states = list(names['state_index'])
        provinces = list(names['province_index'])
        resources = list(names['resource_index'])
        self.initial_state['input_reserved'] = _ReservationLedger(list(self.goods))
        self.initial_state['labor_reserved'] = _ReservationLedger(
            [(state, province) for state in states for province in provinces])
        self.initial_state['resource_reserved'] = _ReservationLedger(
            [(province, resource) for province in provinces for resource in resources])
        self.initial_state['supply_revision'] = _ReservationLedger(list(self.goods), np.int32)
        self.initial_state['flow_reserved'] = _FlowLedger(names)
        self.initial_state['flow_balances'] = _FlowBalanceStore(names)
        self.flow_kernel = _ProjectFlowKernel(self, names, inv)
        self.recipes = _ProjectRecipes(names, inv)
        self.available_inputs = np.zeros(len(names['good_names']), dtype=np.float64)
        self.upstream_cache = _UpstreamCache(len(self.accounts))
        self.initial_state['upstream_candidates'] = self.upstream_cache
        self.project_table = _ProjectTable(names, max(16, len(self.accounts)*4))
        for account in self.accounts.values():
            if account.get('project'):
                self.project_table.append(names, account['project'])
        self.queue_table = _QueueTable(names, self.meta.get('investment_queue', []),
                                       max(16, len(self.accounts)*2))
        self.decision_buffer = _DecisionBuffer(max(64, len(self.accounts)*12))
        self.dynamic_cache = [None]*len(self.accounts)
        self.live_dynamic = dict(
            lr_state=_int32([names['state_index'][state] for state, province in self.initial_state['labor_reserved'].index]),
            lr_province=_int32([names['province_index'][province] for state, province in self.initial_state['labor_reserved'].index]),
            lr_value=self.initial_state['labor_reserved'].data,
            lr_count=len(self.initial_state['labor_reserved'].index),
            rr_province=_int32([names['province_index'][province] for province, resource in self.initial_state['resource_reserved'].index]),
            rr_resource=_int32([names['resource_index'][resource] for province, resource in self.initial_state['resource_reserved'].index]),
            rr_value=self.initial_state['resource_reserved'].data,
            rr_count=len(self.initial_state['resource_reserved'].index),
            la_state=inv['labor_available_state'], la_province=inv['labor_available_province'],
            la_value=inv['labor_available_value'], la_count=len(inv['labor_available_value']),
            ru_province=inv['resource_used_province'], ru_resource=inv['resource_used_resource'],
            ru_value=inv['resource_used_value'], ru_count=len(inv['resource_used_value']))
        self._refresh_project_arrays()
        self._investment_dirty_goods = snapshot['dirty_goods']
        self.trace = snapshot["trace"]
        self.journal = []
        self.execution_seq = 0
        self.investor_order = []
        self.reservations = None
        self.prior_upstream_diagnostics = snapshot['upstream_diagnostics']

    def rng_digest(self):
        return hashlib.sha256(repr(self.rng.getstate()).encode()).hexdigest()

    def reservation_snapshot(self):
        state = getattr(self, '_investment_state', None)
        if state is None:
            return None
        return dict(flow=dict(state['flow_reserved']), inputs=dict(state['input_reserved']),
            labor=dict(state['labor_reserved']), resources=dict(state['resource_reserved']),
            supply_revision=dict(state['supply_revision']), dirty_goods=set(state['dirty_goods']),
            projects=copy.deepcopy(state['index']['projects']),
            project_revision=state['index']['project_revision'])

    def record_candidate(self, investor, project, rng_before):
        self.decision_buffer.append(self.execution_seq, 1,
            self.names['account_index'][investor['id']], -1,
            dict(project=copy.deepcopy(project), rng_before=rng_before,
                 rng_after=self.rng_digest()) if self.trace else
            dict(project=copy.deepcopy(project)))
        self.execution_seq += 1

    def build_investment_state(self):
        return self.initial_state

    def report_detail(self):
        return self.meta['report_detail']

    def shuffled(self, values, key=lambda item: item['id']):
        result = sorted(values, key=key)
        self.rng.shuffle(result)
        return result

    def available_input(self, buyer, good):
        inv, sup = self.inv, self.support
        ai = self.names['account_index'][buyer['id']]
        gi = self.names['good_index'][good]
        tile = int(inv['account_tile'][ai])
        rows = _lazy_book(inv, sup, tile, gi)
        # Keep Python 3.12's compensated float-sum behavior from the former
        # packed generator while the rows themselves are queried lazily.
        return sum(float(row[2]) for row in rows
                   if row[3] and row[1] != ai and row[2] > EPS)

    def _refresh_project_arrays(self):
        """Живой индекс меняется только при commit, а не перед инвестором."""
        offsets, tiles, prices, capacities = [0], [], [], []
        for good in self.goods:
            rows = self.project_table.rows_for(self.names['good_index'][good])
            for tile, price, capacity in rows:
                tiles.append(tile)
                prices.append(price)
                capacities.append(capacity)
            offsets.append(len(tiles))
        self.live_dynamic.update(pj_offset=_int32(offsets), pj_tile=_int32(tiles),
            pj_price=_float64(prices), pj_capacity=_float64(capacities))

    def candidate_dynamic(self, int account, allowed_goods):
        names = self.names
        cached = self.dynamic_cache[account]
        if cached is None:
            investor = self.accounts[names['account_names'][account]]
            markets = self.initial_state['index']['markets'].get(investor['state_id'], ())
            firm = self.enterprises.get(investor['id'])
            cached = dict(
                investor_account=account,
                mk_province=_int32([names['province_index'][pid] for pid, good in markets]),
                mk_good=_int32([names['good_index'][good] for pid, good in markets]),
                mk_count=len(markets),
                exp_present=firm is not None,
                exp_good=names['good_index'][firm['good']] if firm else -1,
                exp_province=names['province_index'][firm['province_id']] if firm else -1,
                exp_state=names['state_index'][firm['state_id']] if firm else -1,
                exp_tile=names['tile_index'][firm['tile_id']] if firm else -1,
                exp_price=firm['price'] if firm else np.nan,
                exp_city_present=bool(firm.get('city_id')) if firm else False,
                month=self.meta['month'])
            self.dynamic_cache[account] = cached
        dyn = dict(cached)
        dyn.update(self.live_dynamic)
        dyn['al_mask'] = _uint8([allowed_goods is None or good in allowed_goods for good in self.goods])
        dyn['al_present'] = allowed_goods is not None
        return dyn

    def touch_capacity(self, state, province, resource):
        state_name = self.names['state_names'][state]
        province_name = self.names['province_names'][province]
        self._investment_state['labor_reserved'][state_name, province_name]
        if resource >= 0:
            resource_name = list(self.names['resource_index'])[resource]
            self._investment_state['resource_reserved'][province_name, resource_name]

    def ensure_suppliers(self, int good):
        """V3 запоминает поставщиков при первом запросе товара, после очереди."""
        cdef double quantity, missing, value, ceiling, reserve, distance, delivery
        cdef double own_cash, owner_cash, funds
        cdef int owner, firm, tile, gi, i, j
        cdef dict inv = self.inv
        cdef dict sup = self.support
        good_name = self.names['good_names'][good]
        self._investment_state['supply_revision'][good_name]
        self._investment_index['projects'][good_name]
        if self.suppliers_initialized[good]:
            return
        self.suppliers_initialized[good] = 1
        for i in range(sup['sup_offset'][good], sup['sup_offset'][good+1]):
            owner = sup['sup_owner'][i]
            firm = sup['sup_account'][i]
            project = self.accounts[self.names['account_names'][owner]].get('project')
            reserve = 0.0
            if project:
                tile = inv['account_tile'][owner]
                distance = sup['distances'][tile, self.names['tile_index'][project['location']['tile_id']]]
                bill = [(int(g), float(q)*project['capacity']/inv['minimum_capacity'])
                        for g, q in zip(inv['construction_good'], inv['construction_value'])]
                delivery = (sum(q*float(inv['good_weight'][g]) for g, q in bill)
                            *distance*inv['transport_rate']) if not isnan(distance) else project.get('delivery', 0)
                reserve = project['working_cash']+delivery
                for gi, quantity in bill:
                    ordered = sum(float(inv['order_remaining'][j]) for j in range(inv['order_count'])
                        if inv['order_participant'][j] == owner and inv['order_side_buy'][j]
                        and inv['order_construction'][j] and inv['order_good'][j] == gi
                        and inv['order_status_active'][j] and inv['order_reserved'][j] > EPS)
                    missing = max(0, quantity-inv['account_stock'][owner, gi]-ordered)
                    _quoted_cost_native(inv, sup, tile, gi, owner, missing, &value, &ceiling)
                    reserve += missing*ceiling
            own_cash = max(0, inv['account_balance'][firm]-inv['account_reserved'][firm]
                               -inv['account_protected'][firm])
            owner_cash = max(0, inv['account_balance'][owner]-inv['account_reserved'][owner]
                                 -max(inv['account_protected'][owner], reserve))
            funds = own_cash+owner_cash
            if funds <= EPS:
                sup['sup_capacity'][i] = 0.0

    def market_signal_values(self, state, province, good):
        si = self.names['state_index'][state]
        pi = self.names['province_index'][province]
        gi = self.names['good_index'][good]
        return (max(0, _sig_field(self.support, si, pi, gi, 0)),
                max(0, _sig_field(self.support, si, pi, gi, 1)))

    def project_candidate(self, investor, sells, derived_demand=None,
                          demand_region=None, parent_project_id=None,
                          diagnostics=None, allowed_goods=None):
        names = self.names
        ai = names['account_index'][investor['id']]
        dyn = self.candidate_dynamic(ai, allowed_goods)
        derived = derived_demand or {}
        dg, amount, region = -1, 0.0, -1
        if derived:
            good, amount = next(iter(derived.items()))
            dg = names['good_index'][good]
            region = names['province_index'][demand_region]
            markets = set(zip(dyn['mk_province'].tolist(), dyn['mk_good'].tolist()))
            markets.add((region, dg))
            markets = sorted(markets, key=lambda pair: (
                names['province_names'][pair[0]], names['good_names'][pair[1]]))
            dyn['mk_province'] = _int32([p for p, g in markets])
            dyn['mk_good'] = _int32([g for p, g in markets])
            dyn['mk_count'] = len(markets)
        rng_before = self.rng_digest() if self.trace else None
        result = evaluate_project_candidate(self.inv, self.support, dyn, ai,
            self.rng, dg, amount, region, parent_project_id is not None)
        self.inv['account_optimism'][ai] = result['optimism']
        self.inv['account_risk_tolerance'][ai] = result['risk_tolerance']
        if not isnan(result['optimism']):
            investor['optimism'] = result['optimism']
        if not isnan(result['risk_tolerance']):
            investor['risk_tolerance'] = result['risk_tolerance']
        if diagnostics is not None:
            stages = {1: ('demand', ('demand', 'minimum')),
                      2: ('capacity', ('capacity', 'minimum')),
                      3: ('margin', ('margin', 'unit_cost', 'price')),
                      4: ('route', ()),
                      5: ('funding_capacity', ('capacity', 'minimum')),
                      6: ('payback', ('investment', 'available', 'profit', 'horizon'))}
            for pid, good, stage, v1, v2, v3, v4 in result['diagnostics']:
                stage_name, keys = stages[stage]
                row = dict(investor=investor['id'], good=names['good_names'][good],
                    province_id=names['province_names'][pid], stage=stage_name)
                row.update(zip(keys, (v1, v2, v3, v4)))
                diagnostics.append(row)
        if not result['candidate_present']:
            self.record_candidate(investor, None, rng_before)
            return None
        good = names['good_names'][result['good']]
        province = names['province_names'][result['location_province']]
        city = (None if self.goods[good]['resource'] else
                self.enterprises[investor['id']].get('city_id') if result['expansion']
                else names['province_city'][province])
        loc = dict(state_id=names['state_names'][result['location_state']],
            province_id=province, city_id=city,
            tile_id=names['tile_names'][result['location_tile']])
        project = dict(good=good, capacity=result['capacity'], location=loc,
            materials={}, working_cash=result['working_cash'],
            expected_price=result['expected_price'], expected_profit=result['expected_profit'],
            expected_investment=result['expected_investment'],
            expected_unit_cost=result['expected_unit_cost'], expansion=result['expansion'],
            created=result['created'],
            chain_requirements={names['good_names'][g]: q for g, q in result['chain']},
            project_id=f"{investor['id']}:{good}:{province}:{self.meta['month']}",
            parent_project_id=parent_project_id,
            derived_demand=derived.get(good) if derived else None,
            dependency_expires=result['dependency_expires'])
        self.record_candidate(investor, project, rng_before)
        return project

    def recipe_requirements(self, good, quantity):
        return self.recipes.requirements(self.names['good_index'][good], quantity)

    def reserve_investment_project(self, project):
        """Применить решение к ledger до обработки следующего инвестора."""
        state = getattr(self, '_investment_state', None)
        if state is None:
            return
        recipe, loc = self.goods[project['good']], project['location']
        capacity = project['capacity']
        state['labor_reserved'][loc['state_id'], loc['province_id']] += capacity*recipe['labor']
        if recipe['resource']:
            state['resource_reserved'][loc['province_id'], recipe['resource']] += capacity
        state['supply_revision'][project['good']] += 1
        state['dirty_goods'].update(self.recipes.affected_names(self.names['good_index'][project['good']]))

    def register_investment_project(self, project):
        state = getattr(self, '_investment_state', None)
        index = state['index'] if state is not None else getattr(self, '_investment_index', None)
        if index is not None:
            index['projects'][project['good']].append((dict(project['location']),
                project['expected_price'], project['capacity']))
        self.project_table.append(self.names, project)
        if index is not None:
            index['project_revision'] += 1
        self.reserve_investment_project(project)
        self._refresh_project_arrays()
        self.decision_buffer.append(self.execution_seq, 2,
            self.names['account_index'][project.get('investor', project.get('owner_id', ''))]
            if project.get('investor', project.get('owner_id')) in self.names['account_index'] else -1,
            self.project_table.count-1,
            dict(project=copy.deepcopy(project),
                 reservations=self.reservation_snapshot()) if self.trace else
            dict(project=copy.deepcopy(project)))
        self.execution_seq += 1

    def investment_flow_balance(self, state_id, good, full=False):
        """Вернуть устойчивый месячный поток товара внутри государства.

        Это диагностический расчёт для будущего планировщика: он не меняет
        склад, проекты, отчёт или RNG. Запас товара намеренно не входит в
        баланс — он не является постоянным выпуском для новой мощности.
        """
        state = getattr(self, '_investment_state', None)
        key = (state_id, good)
        if state is not None and key in state['flow_balances']:
            return state['flow_balances'][key]

        def active(firm):
            return (firm['state_id'] == state_id and firm['status'] != 'closed'
                and not firm.get('suspended')
                and firm['available_from'] <= self.meta['month'])

        def confirmed_output(firm):
            history = firm.get('sales_history', [])[max(0, len(firm.get('sales_history', []))-3):]
            actual = max((row.get('output', 0) for row in history), default=0)
            return min(firm['capacity'], max(0, firm.get('planned_output', 0), actual))

        suppliers = []
        consumers = []
        supply = capacity_supply = operating_demand = 0.0
        for firm in self.enterprises.values():
            if not active(firm):
                continue
            output = confirmed_output(firm)
            if firm['good'] == good:
                supply += output
                capacity_supply += firm['capacity']
                suppliers.append(dict(enterprise_id=firm['id'], output=output,
                    capacity=firm['capacity']))
            ratio = self.goods[firm['good']]['inputs'].get(good, 0)
            if ratio or full and good in self.goods[firm['good']]['inputs']:
                demand = output*ratio
                operating_demand += demand
                consumers.append(dict(enterprise_id=firm['id'], output=output,
                    ratio=ratio, demand=demand))

        construction_demand = 0.0
        projects = []
        for account in self.accounts.values():
            project = account.get('project')
            if not project or account['state_id'] != state_id:
                continue
            ratio = self.goods[project['good']]['inputs'].get(good, 0)
            if ratio or full and good in self.goods[project['good']]['inputs']:
                demand = project['capacity']*ratio
                construction_demand += demand
                projects.append(dict(project_id=project.get('project_id'),
                    investor=account['id'], good=project['good'], ratio=ratio,
                    demand=demand))

        committed = operating_demand+construction_demand
        result = dict(state_id=state_id, good=good, supply=supply,
            capacity_supply=capacity_supply,
            operating_demand=operating_demand,
            construction_demand=construction_demand, committed=committed,
            free=max(0, supply-committed), shortage=max(0, committed-supply),
            rampable_free=max(0, capacity_supply-committed),
            suppliers=suppliers, consumers=consumers, projects=projects)
        if state is not None:
            state['flow_balances'][key] = result
        return result

    def investment_flow_balances(self, state_ids):
        # Один и тот же ленивый числовой индекс. Для full все строки
        # фиксируются до возобновления очереди, в исходном порядке ключей.
        for state_id in state_ids:
            for good in self.goods:
                self.investment_flow_balance(state_id, good, full=True)
        return self._investment_state['flow_balances']

    def sustainable_candidate_capacity(self, candidate, reserved, flow_balances=None):
        return self.flow_kernel.solve(self.names['state_index'][candidate['state_id']],
            self.names['good_index'][candidate['good']],
            candidate.get('quantity', candidate.get('capacity', 0)), reserved)

    def limit_project_capacity(self, project, capacity):
        """Уменьшить ещё не начатый проект без изменения его экономики на единицу."""
        previous = project['capacity']
        if capacity+EPS >= previous:
            return False
        previous_working = project['working_cash']
        construction = max(0, project['expected_investment']-previous_working)
        working = 0 if project['expansion'] else max(self.cfg['startup_cash'],
            capacity*project['expected_unit_cost'])
        # Это диагностическая исходная величина; нормализация убирает шум
        # порядка сложения в индексированных и прямых расчётах.
        project.update(requested_capacity=round(previous, 12), capacity=capacity,
            working_cash=working,
            expected_profit=project['expected_profit']*capacity/previous,
            expected_investment=construction*capacity/previous+working,
            chain_requirements=self.recipe_requirements(project['good'], capacity),
            capacity_limit='Устойчивый поток входов')
        return True

    def investment_candidate_capacity_limits(self, candidates, flow_balances=None):
        """Теневой общий лимит кандидатов по существующей цепочке выпуска."""
        ledger = _FlowLedger(self.names)
        result = []
        ordered = sorted(candidates, key=lambda row: (
            row.get('state_id', ''), row.get('good', ''), row.get('project_id', ''),
            row.get('investor', '')))
        for order, candidate in enumerate(ordered):
            desired = max(0, candidate.get('quantity', candidate.get('capacity', 0)))
            best, ledger = self.sustainable_candidate_capacity(candidate, ledger,
                flow_balances)
            result.append(dict(**copy.deepcopy(candidate), allocation_order=order,
                desired_capacity=desired, sustainable_capacity=best,
                reduction=max(0, desired-best),
                status='fits_existing_flow' if desired-best <= EPS else 'capped_by_existing_flow'))
        return result

    def investment_expansion_intents(self, demands, flow_balances=None):
        accumulator = _ExpansionFlows(self.flow_kernel, self.names)
        ordered = sorted(demands, key=lambda row: (
            row['state_id'], row['good'], str(row.get('decision_id', ''))))
        for demand in ordered:
            state_id, good = demand['state_id'], demand['good']
            quantity = demand['quantity']
            if good not in self.goods:
                raise KeyError('Неизвестный товар: '+str(good))
            if quantity < -EPS:
                raise ValueError('Потребность не может быть отрицательной')
            if quantity > EPS:
                accumulator.add(self.names['state_index'][state_id],
                    self.names['good_index'][good], quantity, demand.get('decision_id'))
        return accumulator.rows()

    def investment_expansion_diagnostics(self, intents):
        """Проверить физические ограничения агрегированных intent'ов.

        Это предварительный диагностический слой: он не принимает решение о
        стройке и намеренно не подменяет будущие проверки денег, маржи или
        конкретного маршрута. Он отвечает только на вопрос, есть ли открытый
        поставщик, свободен ли общий природный ресурс и хватает ли потоков
        прямых входов.
        """
        result = []
        for intent in sorted(intents, key=lambda row: (row['state_id'], row['good'])):
            state_id, good = intent['state_id'], intent['good']
            required_flow = intent['required_flow']
            recipe = self.goods[good]
            suppliers = [firm for firm in self.enterprises.values()
                if firm['good'] == good and firm['state_id'] == state_id
                and firm['status'] != 'closed' and not firm.get('suspended')]
            blockers = []
            if not suppliers:
                blockers.append('no_open_supplier')

            resource = None
            if recipe['resource']:
                total = sum(self.cfg['extraction_per_tile'] for tile in self.tiles.values()
                    if tile.get('province_id') and self.owners.get(tile['province_id']) == state_id
                    and recipe['resource'] in tile['resources'])
                used = sum(firm['capacity'] for firm in self.enterprises.values()
                    if firm['state_id'] == state_id and firm['status'] != 'closed'
                    and self.goods[firm['good']]['resource'] == recipe['resource'])
                resource = dict(tag=recipe['resource'], total_capacity=total,
                    allocated_capacity=used, free_capacity=max(0, total-used))
                if resource['free_capacity']+EPS < required_flow:
                    blockers.append('resource_capacity')

            input_shortages = []
            for input_good, ratio in sorted(recipe['inputs'].items()):
                required = required_flow*ratio
                free = self.investment_flow_balance(state_id, input_good)['rampable_free']
                shortage = max(0, required-free)
                if shortage > EPS:
                    input_shortages.append(dict(good=input_good, required=required,
                        free=free, shortage=shortage))
            if input_shortages:
                blockers.append('input_flow')

            result.append(dict(**copy.deepcopy(intent), supplier_count=len(suppliers),
                resource=resource, input_shortages=input_shortages,
                blockers=blockers,
                status='blocked' if blockers else 'potentially_expandable'))
        return result

    def investment_candidate_statuses(self, candidates, intents, diagnostics):
        # Связи строк журнала хранятся последовательно; lookup-dict нет.
        linked_ids, linked_rows = [], []
        for intent in intents:
            diagnostic = next((row for row in diagnostics
                if row['state_id'] == intent['state_id'] and row['good'] == intent['good']), None)
            if diagnostic is None:
                continue
            for parent in intent.get('parents', []):
                decision_id = parent.get('decision_id')
                if decision_id is not None:
                    linked_ids.append(decision_id)
                    linked_rows.append(diagnostic)
        result = []
        for candidate in candidates:
            rows = [row for decision_id, row in zip(linked_ids, linked_rows)
                    if decision_id == candidate.get('project_id')]
            blockers = sorted({blocker for row in rows for blocker in row['blockers']})
            status = 'blocked' if blockers else 'waiting_for_intent' if rows else 'ready'
            result.append(dict(**copy.deepcopy(candidate), status=status, blockers=blockers,
                intents=sorted(f"{row['state_id']}:{row['good']}" for row in rows)))
        return result

    def investment_shadow_gate(self, statuses, decisions):
        """Записать, что запретил бы будущий gate, не влияя на решения."""
        started = {row.get('project_id') for row in decisions
            if row.get('outcome') == 'started' and row.get('project_id')}
        result = []
        for status in statuses:
            project_id = status.get('project_id')
            result.append(dict(**copy.deepcopy(status),
                actual_started=project_id in started,
                would_block=status['status'] == 'blocked'))
        return result

    def upstream_expansion_candidate(self, good, state_id, sells, shortage,
                                     province_id, parent_project_id):
        cache = self.upstream_cache
        gi = self.names['good_index'][good]
        si = self.names['state_index'][state_id]
        pi = self.names['province_index'][province_id]
        rounded = round(shortage, 8)
        slot = cache.lookup(gi, si, rounded, pi)
        if slot >= 0:
            return cache.result(slot, self.accounts, self.names)
        best_owner = -1
        best_project = None
        best_capacity = 0.0
        diagnostics = []
        for firm in self.enterprises.values():
            if (firm['good'] != good or firm['status'] == 'closed'
                    or firm.get('suspended') or firm['state_id'] != state_id):
                continue
            account = self.accounts[firm['id']]
            if account.get('project'):
                continue
            project = self.project_candidate(account, sells,
                derived_demand={good: shortage}, demand_region=firm['province_id'],
                parent_project_id=parent_project_id, diagnostics=diagnostics,
                allowed_goods={good})
            if (project and project['good'] == good and project['expansion']
                    and (best_project is None or project['capacity'] > best_capacity)):
                best_owner = self.names['account_index'][account['id']]
                best_project = project
                best_capacity = project['capacity']
        slot = cache.store(gi, si, rounded, pi, best_owner, best_project, diagnostics)
        return cache.result(slot, self.accounts, self.names)

    def log_event(self, event_type, *, entity_id=None, region_id=None, good=None,
                  cause=None, source=None, parent_event_id=None, decision_id=None,
                  project_id=None, **values):
        """Добавить компактное причинное событие текущего месяца.

        События хранятся в отчёте и потому попадают в существующее хранилище
        turn results. Идентификатор детерминирован и не влияет на RNG.
        """
        if self.report_detail() != 'deep':
            return None
        event_id = f"{self.meta['month']}:event:{len(self.report['events'])}"
        row = dict(event_id=event_id, month=self.meta['month'], event_type=event_type)
        row.update({k: v for k, v in dict(entity_id=entity_id, region_id=region_id,
                   good=good, cause=cause, source=source, parent_event_id=parent_event_id,
                   decision_id=decision_id, project_id=project_id).items() if v is not None})
        row.update({k: v for k, v in values.items() if v is not None})
        self.report['events'].append(row)
        return event_id

    def project_requests(self, sells):
        due = self.meta['month'] % self.cfg['investment_interval'] == 0
        dirty_goods = set(getattr(self, '_investment_dirty_goods',
            self.meta.get('investment_dirty', [])))
        evaluate = due or bool(dirty_goods)
        allowed_goods = None if due else dirty_goods
        if evaluate:
            # Всё, что возникнет во время этого обзора, относится уже к
            # следующему месяцу, а не возвращает нас в полный цикл сейчас.
            self._investment_dirty_goods = set()
            if due:
                self.meta['investment_full_reconcile_month'] = self.meta['month']
        self.report['investment_review'] = dict(
            mode='full' if due else 'targeted' if evaluate else 'skipped',
            dirty_goods=sorted(dirty_goods),
            month=self.meta['month'])
        investors = [a for a in self.accounts.values() if a['kind'] in ('private', 'merchant', 'treasury', 'municipal') or
            (a['kind'] == 'enterprise' and self.enterprises[a['id']]['status'] != 'closed' and self.enterprises[a['id']]['available_from'] <= self.meta['month'])]
        # Дорогой срез строится лишь для полного либо адресного обзора. Очередь
        # ниже может возобновиться и без него: она смотрит только на офферы.
        if evaluate:
            self._investment_state = self.build_investment_state()
            self._investment_supply_cache = self._investment_state['supply_cache']
            self._investment_index = self._investment_state['index']
            self._investment_demand_cache = self._investment_state['demand_cache']
            self._investment_demand_cache_hits = self._investment_demand_cache_misses = 0
            self._investment_input_reserved = self._investment_state['input_reserved']
        diagnostic_demands = []
        flow_balances = None
        if due:
            state_ids = sorted({a['state_id'] for a in self.accounts.values() if a.get('state_id')})
            flow_balances = self.investment_flow_balances(state_ids)
        elif evaluate:
            flow_balances = self._investment_state['flow_balances']
        queue = self.meta.setdefault('investment_queue', [])
        self.queue_table = _QueueTable(self.names, queue, max(16, len(self.accounts)*2))
        waiting = []
        for qslot in range(self.queue_table.count):
            item = self.queue_table.payload[qslot]
            investor = self.accounts.get(item.get('investor'))
            project = item.get('project')
            if not investor or investor.get('project') or not project:
                continue
            ready = all(project['capacity']*ratio <=
                self.available_input(investor, good)+EPS
                for good, ratio in self.goods[project['good']]['inputs'].items())
            if ready:
                investor['project'] = project
                self.register_investment_project(project)
                self.report['investment_decisions'].append(dict(
                    kind='investment_decision', investor=investor['id'], outcome='resumed',
                    **copy.deepcopy(project)))
                self.decision_buffer.append(self.execution_seq, 3,
                    self.names['account_index'][investor['id']], self.project_table.count-1)
            else:
                waiting.append(item)
        queue[:] = waiting
        ordered = self.shuffled(investors)
        self.investor_order = [a["id"] for a in ordered]
        for a in ordered:
            project = (self.project_candidate(a, sells, allowed_goods=allowed_goods)
                if evaluate and 'project' not in a else None)
            if project:
                demand = dict(state_id=a['state_id'], good=project['good'],
                    quantity=project['capacity'], decision_id=project['project_id'],
                    project_id=project['project_id'], investor=a['id'],
                    province_id=project['location']['province_id'])
                diagnostic_demands.append(demand)
                self.report['investment_candidates'].append(copy.deepcopy(demand))
                sustainable_capacity, flow_trial = self.flow_kernel.solve(
                    self.names['state_index'][a['state_id']],
                    self.names['good_index'][project['good']], project['capacity'],
                    self._investment_state['flow_reserved'])
                if sustainable_capacity+EPS < project['capacity']:
                    if sustainable_capacity < self.cfg['minimum_capacity']:
                        self.report['investment_decisions'].append(dict(
                            kind='investment_decision', investor=a['id'], outcome='rejected',
                            good=project['good'], capacity=project['capacity'],
                            reason='Недостаточный устойчивый поток входов',
                            sustainable_capacity=sustainable_capacity,
                            project_id=project['project_id']))
                        project = None
                    else:
                        self.limit_project_capacity(project, sustainable_capacity)
                feasible = True
                available_inputs = self.available_inputs
                for input_good, ratio in self.goods[project['good']]['inputs'].items() if project else ():
                    required = project['capacity']*ratio
                    available = self.available_input(a, input_good)
                    available_inputs[self.names['good_index'][input_good]] = available
                    free = available-self._investment_input_reserved[input_good]
                    if required > free+EPS:
                        feasible = False
                        self.report['investment_decisions'].append(dict(
                            kind='investment_decision', investor=a['id'], outcome='rejected',
                            good=project['good'], reason='Недостаточный общий резерв входного сырья',
                            input_good=input_good, required=required, available=available,
                            already_reserved=self._investment_input_reserved[input_good]))
                        break
                if not feasible:
                    # Расширение upstream выполняется только при подтверждённом
                    # спросе на это звено; сам поиск кэширован на месяц.
                    shortage = max(0, required-free)
                    upstream = self.upstream_expansion_candidate(
                        input_good, a['state_id'], sells, shortage,
                        project['location']['province_id'], project['project_id'])
                    if not upstream:
                        self.log_event('investment.upstream_unavailable',
                            entity_id=a['id'], region_id=project['location']['province_id'],
                            good=input_good, cause='no_profitable_or_sized_candidate',
                            source='upstream_expansion_candidate',
                            parent_event_id=f"{a['id']}:{project['good']}:{self.meta['month']}",
                            required=required, available=available,
                            already_reserved=self._investment_input_reserved[input_good],
                            bounded_shortage=shortage, parent_good=project['good'],
                            candidate_diagnostics=self.upstream_cache.last_diagnostics())
                    if upstream:
                        owner, upstream_project = upstream
                        if not owner.get('project'):
                            owner['project'] = upstream_project
                            self.register_investment_project(upstream_project)
                            upstream_decision = copy.deepcopy(upstream_project)
                            upstream_decision.update(kind='investment_decision',
                                investor=owner['id'], outcome='started',
                                dependency_for=project['good'],
                                parent_project_id=project.get('project_id'))
                            self.report['investment_decisions'].append(upstream_decision)
                    queue_key = (a['id'], project['good'], project['location']['province_id'])
                    queued_keys = {(x.get('investor'), x.get('project', {}).get('good'),
                                    x.get('project', {}).get('location', {}).get('province_id'))
                                   for x in queue}
                    if queue_key not in queued_keys:
                        queued = dict(created=self.meta['month'], investor=a['id'],
                                      project=copy.deepcopy(project))
                        queue.append(queued)
                        self.queue_table.append(self.names, queued)
                    project = None
            if project:
                self._investment_state['flow_reserved'].replace(flow_trial)
                for input_good, ratio in self.goods[project['good']]['inputs'].items():
                    self._investment_input_reserved[input_good] += project['capacity']*ratio
                evidence = {}
                for input_good, ratio in self.goods[project['good']]['inputs'].items():
                    required = project['capacity']*ratio
                    available = float(available_inputs[self.names['good_index'][input_good]])
                    signal = self.market_signal_values(a['state_id'],
                        project['location']['province_id'], input_good)
                    evidence[input_good] = dict(required=round(required, 6),
                        available=round(available, 6), shortage=round(max(0, required-available), 6),
                        historical_unmet=round(signal[1], 6), historical_volume=round(signal[0], 6))
                project['input_evidence'] = evidence
                a['project'] = project
                self.register_investment_project(project)
                self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='started', **copy.deepcopy(project)))
        if evaluate:
            self.report['expansion_intents'] = self.investment_expansion_intents(
                diagnostic_demands, flow_balances)
            self.report['expansion_intent_diagnostics'] = self.investment_expansion_diagnostics(
                self.report['expansion_intents'])
            self.report['investment_candidate_statuses'] = self.investment_candidate_statuses(
                self.report['investment_candidates'], self.report['expansion_intents'],
                self.report['expansion_intent_diagnostics'])
            self.report['investment_shadow_gate'] = self.investment_shadow_gate(
                self.report['investment_candidate_statuses'], self.report['investment_decisions'])
            self.report['investment_candidate_capacity_limits'] = \
                self.investment_candidate_capacity_limits(self.report['investment_candidates'],
                    flow_balances)
        if evaluate:
            self.reservations = self._investment_state
        if evaluate:
            self.__dict__.pop('_investment_supply_cache', None)
            self.__dict__.pop('_investment_index', None)
            self.__dict__.pop('_investment_demand_cache', None)
            self.__dict__.pop('_investment_input_reserved', None)
            self.__dict__.pop('_investment_state', None)
            self.__dict__.pop('_investment_customer_limits', None)
        return due, [a["id"] for a in investors]

def native_project_requests(dict inv, dict support, dict names, dict snapshot, object rng):
    """Единственная внешняя native-точка входа всего инвестиционного обзора."""
    batch = _ProjectBatch(inv, support, names, snapshot, rng)
    due, investors = batch.project_requests(None)
    support.pop('batch', None)
    batch.flow_kernel.owner = None
    return dict(accounts=batch.accounts, meta=batch.meta, report=batch.report,
        dirty_goods=batch._investment_dirty_goods,
        upstream_diagnostics=batch.upstream_cache.materialize_diagnostics(
            names, batch.prior_upstream_diagnostics),
        reservations=batch.reservations, journal=None,
        decision_buffer=batch.decision_buffer,
        project_table=batch.project_table, queue_table=batch.queue_table,
        investor_order=batch.investor_order, due=due, investors=investors)
