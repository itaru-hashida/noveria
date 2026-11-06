"""Экономика V3: обеспеченные заявки, реальные расходы и один выпуск за месяц."""
import copy
import math
from collections import defaultdict

from .support import EPS, EconomyServices
from .food_diagnostics import FoodDiagnostics


OWNER_KINDS = ('private', 'merchant', 'treasury', 'municipal')
OWNER_BASKET = ('bread', 'fish', 'cloth')
OWNER_CONSUMPTION = 'Личное потребление собственника'
CONSUMPTION_PURPOSES = ('Базовые нужды', 'Дополнительные нужды', OWNER_CONSUMPTION)


class Economy(EconomyServices):
    OWNER_FAMILY_SIZE = 4
    OWNER_FOOD_QUALITY = 4
    RULES_VERSION = 3
    FOOD_DIAGNOSTICS = True
    INPUT_BID_REVISION = 1
    STOCK_PLANNING_REVISION = 2
    PROFIT_DISTRIBUTION_REVISION = 1
    HOUSEHOLD_TOP_UP_REVISION = 1
    BASIC_BUDGET_REVISION = 1
    OUTPUT_SMOOTHING_REVISION = 1
    ENTERPRISE_OVERHEAD_REVISION = 1

    def reset_report(self):
        super().reset_report()
        self.report.update(rules_version=3, investment_decisions=[], dividends=[],
            owner_consumption=[], owner_consumed={}, events=[],
            investment_candidates=[], expansion_intents=[],
            expansion_intent_diagnostics=[], investment_candidate_statuses=[],
            investment_shadow_gate=[], investment_candidate_capacity_limits=[])

    def log_event(self, event_type, *, entity_id=None, region_id=None, good=None,
                  cause=None, source=None, parent_event_id=None, decision_id=None,
                  project_id=None, **values):
        """Добавить компактное причинное событие текущего месяца.

        События хранятся в отчёте и потому попадают в существующее хранилище
        turn results. Идентификатор детерминирован и не влияет на RNG.
        """
        event_id = f"{self.meta['month']}:event:{len(self.report['events'])}"
        row = dict(event_id=event_id, month=self.meta['month'], event_type=event_type)
        row.update({k: v for k, v in dict(entity_id=entity_id, region_id=region_id,
                   good=good, cause=cause, source=source, parent_event_id=parent_event_id,
                   decision_id=decision_id, project_id=project_id).items() if v is not None})
        row.update({k: v for k, v in values.items() if v is not None})
        self.report['events'].append(row)
        return event_id

    def build_causal_events(self):
        """Собрать JSONL-подобный слой из уже рассчитанного месячного отчёта."""
        events = self.report['events']
        decisions = self.report.get('investment_decisions', [])
        gap_events = {}
        for decision in decisions:
            project_id = decision.get('project_id') or decision.get('investor')
            self.log_event('investment.' + decision.get('outcome', 'decision'),
                entity_id=decision.get('investor'), region_id=decision.get('province_id'),
                good=decision.get('good'), project_id=project_id,
                decision_id=project_id, cause=decision.get('reason'),
                source=decision.get('dependency_for') or 'project_candidate',
                status=decision.get('outcome'), capacity=decision.get('capacity'),
                expected_profit=decision.get('expected_profit'),
                expected_investment=decision.get('expected_investment'),
                input_evidence=decision.get('input_evidence'),
                chain_requirements=decision.get('chain_requirements'),
                blocked_input=decision.get('input_good'),
                required=decision.get('required'),
                available=decision.get('available'),
                already_reserved=decision.get('already_reserved'),
                dependency_for=decision.get('dependency_for'))

        for intent in self.report.get('expansion_intents', []):
            self.log_event('investment.expansion_intent',
                entity_id=f"{intent['state_id']}:{intent['good']}",
                good=intent['good'], source='investment_expansion_intents',
                decision_id=f"{intent['state_id']}:{intent['good']}",
                status='diagnostic_only', required_flow=intent['required_flow'],
                covered_by_free_flow=intent['covered_by_free_flow'],
                parents=intent['parents'])

        for diagnostic in self.report.get('expansion_intent_diagnostics', []):
            self.log_event('investment.expansion_precheck',
                entity_id=f"{diagnostic['state_id']}:{diagnostic['good']}",
                good=diagnostic['good'], source='investment_expansion_diagnostics',
                decision_id=f"{diagnostic['state_id']}:{diagnostic['good']}",
                status=diagnostic['status'], blockers=diagnostic['blockers'],
                supplier_count=diagnostic['supplier_count'],
                resource=diagnostic['resource'],
                input_shortages=diagnostic['input_shortages'])

        for candidate in self.report.get('investment_candidate_statuses', []):
            self.log_event('investment.candidate_status',
                entity_id=candidate.get('investor'), region_id=candidate.get('province_id'),
                good=candidate.get('good'), source='investment_candidate_statuses',
                decision_id=candidate.get('project_id'), project_id=candidate.get('project_id'),
                status=candidate['status'], blockers=candidate['blockers'],
                intents=candidate['intents'])

        for shadow in self.report.get('investment_shadow_gate', []):
            if not shadow['actual_started']:
                continue
            self.log_event('investment.shadow_gate',
                entity_id=shadow.get('investor'), region_id=shadow.get('province_id'),
                good=shadow.get('good'), source='investment_shadow_gate',
                decision_id=shadow.get('project_id'), project_id=shadow.get('project_id'),
                status=shadow['status'], would_block=shadow['would_block'],
                blockers=shadow['blockers'])

        for limit in self.report.get('investment_candidate_capacity_limits', []):
            if limit.get('sustainable_capacity', 0)+EPS >= limit.get('desired_capacity', 0):
                continue
            self.log_event('investment.candidate_capacity_shadow',
                entity_id=limit.get('investor'), region_id=limit.get('province_id'),
                good=limit.get('good'), source='investment_candidate_capacity_limits',
                decision_id=limit.get('project_id'), project_id=limit.get('project_id'),
                status=limit['status'], desired_capacity=limit['desired_capacity'],
                sustainable_capacity=limit['sustainable_capacity'],
                reduction=limit['reduction'], allocation_order=limit['allocation_order'])

        trade_groups = defaultdict(lambda: dict(count=0, quantity=0.0, gold=0.0, freight=0.0))
        for trade in self.report.get('trades', []):
            key = (trade.get('buyer'), trade.get('seller'), trade.get('good'),
                   trade.get('buyer_province'), trade.get('seller_province'), trade.get('purpose'))
            group = trade_groups[key]
            group['count'] += 1
            group['quantity'] += trade.get('quantity', 0)
            group['gold'] += trade.get('gold', 0)
            group['freight'] += trade.get('freight', 0)
        for (buyer, seller, good, buyer_region, seller_region, purpose), group in trade_groups.items():
            self.log_event('trade.executed', entity_id=f'{buyer}:{seller}:{good}',
                region_id=buyer_region, good=good, source='match',
                quantity=group['quantity'], trades_count=group['count'], seller=seller,
                buyer=buyer, seller_region=seller_region,
                price=group['gold']/group['quantity'] if group['quantity'] else None,
                freight=group['freight'],
                landed_price=(group['gold']+group['freight'])/group['quantity']
                    if group['quantity'] else None,
                gold=group['gold'], purpose=purpose)

        for order in self.report.get('orders', []):
            if order.get('side') != 'buy' or order.get('remaining', 0) <= EPS:
                continue
            observation = order.get('match_observation', {})
            cause = observation.get('reason_code') or order.get('reason') or 'unexplained_gap'
            gap_event = self.log_event('demand.unfilled', entity_id=order.get('id'),
                region_id=order.get('province_id'), good=order.get('good'),
                cause=cause, source='order.match', quantity=order.get('remaining'),
                requested=order.get('quantity'), price_limit=order.get('price'),
                purpose=order.get('purpose'), candidates=len(observation.get('candidates', [])))
            gap_events.setdefault((order.get('province_id'), order.get('good')), []).append(
                (cause, gap_event))

        for enterprise in self.enterprises.values():
            observation = enterprise.get('production_observation')
            if not observation:
                continue
            plan = observation.get('procurement_plan') or {}
            planned = enterprise.get('planned_output', 0)
            actual = enterprise.get('output', 0)
            constraints = observation.get('constraints', {})
            limiting = min(constraints, key=constraints.get) if constraints else None
            if actual + EPS < planned or limiting not in (None, 'План'):
                recipe = self.goods[enterprise['good']]
                account = self.accounts[enterprise['id']]
                input_details = {}
                for input_good, ratio in recipe.get('inputs', {}).items():
                    input_details[input_good] = dict(
                        required=planned*ratio,
                        available=account.get('stock', {}).get(input_good, 0),
                        reserved=account.get('stock_reserved', {}).get(input_good, 0),
                        consumed=actual*ratio)
                self.log_event('production.limited', entity_id=enterprise['id'],
                    region_id=enterprise.get('province_id'), good=enterprise.get('good'),
                    cause=limiting or 'unexplained_gap', source='production.constraints',
                    capacity=enterprise.get('capacity'), planned_output=planned,
                    actual_output=actual, constraints=constraints,
                    expected_margin=enterprise.get('expected_margin'),
                    wages=enterprise.get('wages'), overhead=self.enterprise_overhead(enterprise),
                    input_limited=enterprise.get('input_limited', False),
                    input_orders=plan.get('input_orders', []), input_details=input_details)

        for lifecycle in self.report.get('lifecycle', []):
            action = lifecycle.get('action')
            self.log_event('enterprise.' + str(action), entity_id=lifecycle.get('enterprise'),
                region_id=lifecycle.get('province_id'), good=lifecycle.get('good'),
                cause=lifecycle.get('reason'), source='lifecycle', **{
                    k: v for k, v in lifecycle.items()
                    if k not in ('enterprise', 'province_id', 'good', 'action', 'reason')})

        for province in self.report.get('provinces', []):
            pid = province.get('province_id')
            province_orders = [o for o in self.report.get('orders', [])
                               if o.get('province_id') == pid]
            province_trades = [t for t in self.report.get('trades', [])
                               if t.get('buyer_province') == pid or t.get('seller_province') == pid]
            for market_good in self.goods:
                buys = [o for o in province_orders if o.get('side') == 'buy' and o.get('good') == market_good]
                sells = [o for o in province_orders if o.get('side') == 'sell' and o.get('good') == market_good]
                trades = [t for t in province_trades if t.get('good') == market_good]
                demand = sum(o.get('quantity', 0) for o in buys)
                executed = sum(t.get('quantity', 0) for t in trades if t.get('buyer_province') == pid)
                offered = sum(o.get('quantity', 0) for o in sells)
                stock = sum(a.get('stock', {}).get(market_good, 0)
                            for a in self.accounts.values() if a.get('province_id') == pid)
                if not (demand or offered or trades or stock > EPS
                        or province.get('deficits', {}).get(market_good, 0) > EPS):
                    continue
                self.log_event('market.snapshot', region_id=pid, good=market_good,
                    source='market.clearance', demand=demand, executed=executed,
                    unmet=max(0, demand-executed), offered=offered,
                    sales=sum(t.get('quantity', 0) for t in trades), stock=stock,
                    price=province.get('prices', {}).get(market_good),
                    imports=sum(t.get('quantity', 0) for t in trades
                                if t.get('buyer_province') == pid and t.get('seller_province') != pid),
                    exports=sum(t.get('quantity', 0) for t in trades
                                if t.get('seller_province') == pid and t.get('buyer_province') != pid),
                    delivery=sum(t.get('freight', 0) for t in trades
                                 if t.get('buyer_province') == pid))
            for good, deficit in province.get('deficits', {}).items():
                if deficit <= EPS:
                    continue
                production = province.get('production', {}).get(good, 0)
                stock = self.report.get('stocks', {}).get(good, 0)
                links = gap_events.get((province.get('province_id'), good), [])
                cause, parent = (links[0] if links else ('unexplained_gap', None))
                self.log_event('market.deficit', region_id=province.get('province_id'),
                    good=good, cause=cause, source='market.clearance', parent_event_id=parent,
                    deficit=deficit, production=production, stock=stock,
                    demand=province.get('consumption', {}).get(good),
                    imports=province.get('import_volume', 0),
                    exports=province.get('export_volume', 0),
                    delivery=province.get('transport', 0))

        # Остаточные офферы дают объяснение «товар был, но не продался».
        for offer in self.report.get('orders', []):
            if offer.get('side') == 'sell' and offer.get('remaining', 0) > EPS:
                self.log_event('trade.offer_remaining', entity_id=offer.get('id'),
                    region_id=offer.get('province_id'), good=offer.get('good'),
                    cause='no_matching_buyer', source='market.clearance',
                    seller=offer.get('participant'), quantity=offer.get('remaining'),
                    price=offer.get('price'))

    def mark_investment_dirty(self, good=None):
        dirty = set(self.meta.get('investment_dirty', []))
        if good is None:
            dirty.update(self.goods)
        else:
            parents = self.recipe_graph().get('parents', {})
            pending = [good]
            while pending:
                current = pending.pop()
                if current in dirty:
                    continue
                dirty.add(current)
                pending.extend(parents.get(current, ()))
        self.meta['investment_dirty'] = sorted(dirty)

    def recipe_graph(self):
        graph = getattr(self, '_recipe_graph_cache', None)
        if graph is None:
            parents = {good: set() for good in self.goods}
            children = {good: dict(recipe.get('inputs', {})) for good, recipe in self.goods.items()}
            for product, inputs in children.items():
                for input_good in inputs:
                    parents.setdefault(input_good, set()).add(product)
            graph = {'parents': parents, 'children': children}
            self._recipe_graph_cache = graph
        return graph

    def recipe_requirements(self, good, quantity):
        """Полный производный спрос на входы для заданного выпуска."""
        requirements = defaultdict(float)
        stack = [(good, quantity, ())]
        while stack:
            product, amount, path = stack.pop()
            if product in path:
                raise ValueError('Цикл рецептов: '+' -> '.join(path + (product,)))
            for input_good, ratio in self.recipe_graph()['children'].get(product, {}).items():
                required = amount*ratio
                requirements[input_good] += required
                stack.append((input_good, required, path + (product,)))
        return dict(requirements)

    def investment_flow_balance(self, state_id, good):
        """Вернуть устойчивый месячный поток товара внутри государства.

        Это диагностический расчёт для будущего планировщика: он не меняет
        склад, проекты, отчёт или RNG. Запас товара намеренно не входит в
        баланс — он не является постоянным выпуском для новой мощности.
        """
        def active(firm):
            return (firm['state_id'] == state_id and firm['status'] != 'closed'
                and not firm.get('suspended')
                and firm['available_from'] <= self.meta['month'])

        def confirmed_output(firm):
            history = firm.get('sales_history', [])[-3:]
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
            if ratio:
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
            if ratio:
                demand = project['capacity']*ratio
                construction_demand += demand
                projects.append(dict(project_id=project.get('project_id'),
                    investor=account['id'], good=project['good'], ratio=ratio,
                    demand=demand))

        committed = operating_demand+construction_demand
        return dict(state_id=state_id, good=good, supply=supply,
            capacity_supply=capacity_supply,
            operating_demand=operating_demand,
            construction_demand=construction_demand, committed=committed,
            free=max(0, supply-committed), shortage=max(0, committed-supply),
            rampable_free=max(0, capacity_supply-committed),
            suppliers=suppliers, consumers=consumers, projects=projects)

    def sustainable_flow_headroom(self, state_id, good, reserved=None, path=(),
                                  flow_balances=None):
        """Максимальный дополнительный выпуск существующей цепочки.

        Свободная мощность мельницы считается доступной только если её может
        прокормить свободный поток зерна. ``reserved`` предназначен для
        будущего общего ledger одновременных проектов; сейчас метод чистый.
        """
        if good in path:
            raise ValueError('Цикл рецептов: '+' -> '.join(path+(good,)))
        if reserved is None:
            reserved = {}
        balance = (flow_balances or {}).get((state_id, good))
        if balance is None:
            balance = self.investment_flow_balance(state_id, good)
        capacity_headroom = max(0, balance['capacity_supply']-balance['committed']
            -reserved.get((state_id, good), 0))
        headroom = capacity_headroom
        inputs = []
        for input_good, ratio in sorted(self.goods[good]['inputs'].items()):
            upstream = self.sustainable_flow_headroom(state_id, input_good,
                reserved, path+(good,), flow_balances)
            output_limit = upstream['headroom']/ratio
            inputs.append(dict(good=input_good, ratio=ratio,
                input_headroom=upstream['headroom'], output_limit=output_limit))
            headroom = min(headroom, output_limit)
        return dict(state_id=state_id, good=good,
            capacity_headroom=capacity_headroom, headroom=max(0, headroom),
            inputs=inputs)

    def investment_candidate_capacity_limits(self, candidates, flow_balances=None):
        """Теневой общий лимит кандидатов по существующей цепочке выпуска.

        Это не gate и не резерв склада. Для каждого кандидата метод пытается
        зарезервировать *устойчивый поток* его входов у уже открытых
        поставщиков. Резерв мельницы рекурсивно резервирует зерно, резерв
        ткацкой фабрики — лён и так далее. Поэтому два кандидата не могут
        одновременно получить один и тот же свободный поток.

        Порядок здесь намеренно стабилен и служит только для диагностики.
        Выбор инвестиционного приоритета будет отдельным решением; этот метод
        не меняет состояние экономики, отчёт и RNG.
        """
        def reserve_flow(state_id, good, quantity, ledger, path=()):
            if good in path:
                raise ValueError('Цикл рецептов: '+' -> '.join(path+(good,)))
            available = self.sustainable_flow_headroom(state_id, good, ledger,
                path, flow_balances)['headroom']
            if quantity > available+EPS:
                return None
            trial = dict(ledger)
            for input_good, ratio in sorted(self.goods[good]['inputs'].items()):
                trial = reserve_flow(state_id, input_good, quantity*ratio, trial,
                    path+(good,))
                if trial is None:
                    return None
            key = (state_id, good)
            trial[key] = trial.get(key, 0)+quantity
            return trial

        def reserve_candidate(candidate, quantity, ledger):
            trial = dict(ledger)
            for input_good, ratio in sorted(self.goods[candidate['good']]['inputs'].items()):
                trial = reserve_flow(candidate['state_id'], input_good,
                    quantity*ratio, trial, (candidate['good'],))
                if trial is None:
                    return None
            return trial

        ledger = {}
        result = []
        ordered = sorted(candidates, key=lambda row: (
            row.get('state_id', ''), row.get('good', ''), row.get('project_id', ''),
            row.get('investor', '')))
        for order, candidate in enumerate(ordered):
            desired = max(0, candidate.get('quantity', candidate.get('capacity', 0)))
            best = 0.0
            best_ledger = dict(ledger)
            low, high = 0.0, desired
            for _ in range(40):
                middle = (low+high)/2
                trial = reserve_candidate(candidate, middle, ledger)
                if trial is None:
                    high = middle
                else:
                    low, best, best_ledger = middle, middle, trial
            ledger = best_ledger
            result.append(dict(**copy.deepcopy(candidate), allocation_order=order,
                desired_capacity=desired, sustainable_capacity=best,
                reduction=max(0, desired-best),
                status='fits_existing_flow' if desired-best <= EPS else 'capped_by_existing_flow'))
        return result

    def investment_expansion_intents(self, demands, flow_balances=None):
        """Собрать агрегированные дефициты потоков для подтверждённого спроса.

        ``demands`` содержит словари ``state_id``, ``good``, ``quantity`` и
        необязательный ``decision_id``. Метод только строит диагностику:
        ничего не резервирует, не создаёт проект и не меняет экономику.
        Один intent создаётся на пару ``(state_id, good)`` независимо от
        количества верхних решений, которые к нему привели.
        """
        free_flow = {}
        intents = {}

        def available(state_id, good):
            key = (state_id, good)
            if key not in free_flow:
                balance = (flow_balances or {}).get(key)
                if balance is None:
                    balance = self.investment_flow_balance(state_id, good)
                # Сначала используем резерв уже построенных поставщиков.
                # Новый intent создаётся только когда и он исчерпан.
                free_flow[key] = balance['rampable_free']
            return free_flow[key]

        def require(state_id, product, quantity, decision_id, parent_good, path):
            if product in path:
                raise ValueError('Цикл рецептов: '+' -> '.join(path+(product,)))
            for input_good, ratio in sorted(self.goods[product]['inputs'].items()):
                required = quantity*ratio
                key = (state_id, input_good)
                covered = min(required, available(state_id, input_good))
                free_flow[key] -= covered
                missing = required-covered
                if missing <= EPS:
                    continue
                intent = intents.setdefault(key, dict(
                    state_id=state_id, good=input_good, required_flow=0.0,
                    covered_by_free_flow=0.0, parents=[]))
                intent['required_flow'] += missing
                intent['covered_by_free_flow'] += covered
                intent['parents'].append(dict(decision_id=decision_id,
                    parent_good=parent_good, required=required,
                    covered=covered, missing=missing))
                require(state_id, input_good, missing, decision_id, input_good,
                    path+(product,))

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
                require(state_id, good, quantity, demand.get('decision_id'), good, ())
        return [intents[key] for key in sorted(intents)]

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
        """Связать верхнего кандидата с агрегированными intent'ами без решений."""
        diagnostic_by_key = {(row['state_id'], row['good']): row for row in diagnostics}
        linked = defaultdict(list)
        for intent in intents:
            diagnostic = diagnostic_by_key.get((intent['state_id'], intent['good']))
            if diagnostic is None:
                continue
            for parent in intent.get('parents', []):
                decision_id = parent.get('decision_id')
                if decision_id is not None:
                    linked[decision_id].append(diagnostic)

        result = []
        for candidate in candidates:
            rows = linked.get(candidate.get('project_id'), [])
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

    def add_stock(self, account, good, quantity, value=0):
        result = super().add_stock(account, good, quantity, value)
        self.mark_investment_dirty(good)
        return result

    def open_enterprise(self, *args, **kwargs):
        e = super().open_enterprise(*args, **kwargs)
        e.update(cost_of_sales=0, tax=0, wages=0, unviable_months=0)
        return e

    def activate_rules(self):
        previous = self.meta.get('rules_version', self.meta.get('version', 1))
        self.meta.setdefault('previous_rules_version', previous)
        self.meta.update(version=3, rules_version=3, rng_seed=self.seed,
            input_bid_revision=self.INPUT_BID_REVISION,
            stock_planning_revision=self.STOCK_PLANNING_REVISION,
            profit_distribution_revision=self.PROFIT_DISTRIBUTION_REVISION,
            household_top_up_revision=self.HOUSEHOLD_TOP_UP_REVISION,
            basic_budget_revision=self.BASIC_BUDGET_REVISION,
            output_smoothing_revision=self.OUTPUT_SMOOTHING_REVISION,
            enterprise_overhead_revision=self.ENTERPRISE_OVERHEAD_REVISION,
            rule_changes=['Обеспеченные заявки и торговля существующими товарами',
                'Налог с конечных продаж вместо налога на прибыль',
                'Выпуск по доступному спросу и неотрицательной ожидаемой марже',
                'Сохранённые стройки и оплаченное собственное расширение',
                'Возврат капитала закрытых фирм и продажа их товарных остатков',
                'Дивиденды после резервов и рыночное потребление собственников',
                'Схема Итару-Кодекса: быстрый рост и сглаженное снижение выпуска',
                'Оброк за землю и мастерскую для открытых предприятий'])
        self.meta.setdefault('investment_dirty', sorted(self.goods))
        self.cfg.setdefault('consumption_tax', 0.10)
        self.cfg.setdefault('dividend_profit_share', 0.50)
        self.cfg.setdefault('business_income_share', 1.0)
        self.cfg.setdefault('working_reserve_months', 1)
        self.cfg.setdefault('owner_consumption_rate', 0.05)
        # Асимметричный регулятор выпуска: спрос растёт быстро, спад сглаживается.
        self.cfg.setdefault('output_decline_rate', 0.25)
        self.cfg.setdefault('output_overstock_factor', 2.0)
        self.cfg.setdefault('enterprise_overhead_rate', 0.02)
        self.cfg.setdefault('project_stall_months', 12)
        if (not isinstance(self.cfg['project_stall_months'], int)
                or isinstance(self.cfg['project_stall_months'], bool)
                or self.cfg['project_stall_months'] < 1):
            raise ValueError('Срок пересмотра стройки должен быть положительным целым')
        if not 0 <= self.cfg['consumption_tax'] < 1:
            raise ValueError('Налог с потребительских продаж должен быть от 0 до 1')
        for key in ('dividend_profit_share', 'business_income_share', 'owner_consumption_rate'):
            if not math.isfinite(self.cfg[key]) or not 0 <= self.cfg[key] <= 1:
                raise ValueError('Некорректная доля: '+key)
        if not math.isfinite(self.cfg['working_reserve_months']) or self.cfg['working_reserve_months'] < 0:
            raise ValueError('Некорректный срок оборотного резерва')
        if not 0 < self.cfg['output_decline_rate'] <= 1 or self.cfg['output_overstock_factor'] < 1:
            raise ValueError('Некорректные параметры сглаживания выпуска')
        if not 0 <= self.cfg['enterprise_overhead_rate'] <= 1:
            raise ValueError('Некорректная ставка оброка предприятия')
        for a in self.accounts.values():
            a.setdefault('reserved', 0.0)
            a.setdefault('protected', 0.0)
            a.setdefault('stock_reserved', {})
        for e in self.enterprises.values():
            e.setdefault('available_from', e['opened']+1 if e['opened'] else 0)
            e.setdefault('sales_history', [])
            e.setdefault('sales_forecast', e['capacity']*self.rules['initial_sales_fraction'])
            e.setdefault('asset_value', sum(q*self.goods[g]['price'] for g, q in self.construction_bill(e['capacity']).items()))
            e.setdefault('unviable_months', 0)

    def free_cash(self, account):
        a = self.accounts[account]
        return max(0.0, a['balance']-a.get('reserved', 0)-a.get('protected', 0))

    def enterprise_overhead(self, e):
        """Ежемесячный оброк за площадку и мастерскую при открытом предприятии."""
        return (e['capacity']*self.goods[e['good']]['labor']*self.cfg['wage']
                *self.cfg.get('enterprise_overhead_rate', 0))

    def market_tile(self, account):
        return account.get('cell_id') or account['tile_id']

    def market_province(self, account):
        return self.tiles.get(self.market_tile(account), {}).get('province_id', account['province_id'])

    def project_cash_reserve(self, account, sells):
        a = self.accounts[account]
        project = a.get('project')
        if not project:
            return 0.0
        distance, _ = self.path(self.market_tile(a), project['location']['tile_id'])
        bill = self.construction_bill(project['capacity'])
        delivery = (sum(q*self.goods[g]['weight'] for g, q in bill.items())
            * distance*self.cfg['transport_rate']) if distance is not None else project.get('delivery', 0)
        needed = project['working_cash']+delivery
        for good, quantity in bill.items():
            # Купленные материалы и обеспеченные заявки не требуют повторного резерва денег.
            ordered = sum(o['remaining'] for o in self.report['orders']
                if o['participant'] == account and o['side'] == 'buy'
                and o['purpose'] == 'Строительные материалы' and o['good'] == good
                and o['status'] not in ('cancelled', 'expired') and o['reserved'] > EPS)
            missing = max(0, quantity-a['stock'].get(good, 0)-ordered)
            needed += missing*self.quoted_cost(a, good, missing, sells)[1]
        return needed

    def working_cash_reserve(self, e, sells):
        recipe = self.goods[e['good']]
        quantity = e['capacity']*self.cfg['working_reserve_months']
        needed = quantity*recipe['labor']*self.cfg['wage']
        for good, ratio in recipe['inputs'].items():
            missing = max(0, quantity*ratio-self.free_stock(e['id'], good))
            needed += missing*self.quoted_cost(self.accounts[e['id']], good, missing, sells)[1]
        return needed

    def cash_after_commitments(self, account, project_reserve, working_reserve=0):
        a = self.accounts[account]
        # protected уже содержит часть обязательств; повторно её не вычитаем.
        hold = max(a.get('protected', 0), project_reserve+working_reserve)
        return max(0, a['balance']-a.get('reserved', 0)-hold)

    def pay_dividends(self, sells):
        for e in self.enterprises.values():
            if e.get('dividend_month') == self.meta['month']:
                continue
            e.update(dividends=0.0, dividend_month=self.meta['month'])
            if e['status'] == 'closed' or self.accounts[e['owner_id']]['kind'] not in OWNER_KINDS:
                continue
            recipients = [a for a in self.accounts.values() if a['kind'] == 'household'
                and a['state_id'] == e['state_id']] if self.PROFIT_DISTRIBUTION_REVISION else []
            if self.PROFIT_DISTRIBUTION_REVISION and not sum(self.population(a) for a in recipients):
                continue
            working = self.working_cash_reserve(e, sells)
            project = self.project_cash_reserve(e['id'], sells)
            available = self.cash_after_commitments(e['id'], project, working)
            share = self.cfg['business_income_share'] if self.PROFIT_DISTRIBUTION_REVISION else self.cfg['dividend_profit_share']
            limit = share*max(0, e['profit'])
            amount = min(limit, available)
            if self.PROFIT_DISTRIBUTION_REVISION:
                self.distribute(e['id'], recipients, amount, 'Доход населения от бизнеса')
            else:
                self.transfer(e['id'], e['owner_id'], amount, 'Дивиденды')
            self.accounts[e['id']].update(working_reserve=working, project_reserve=project)
            e.update(dividends=amount, working_reserve=working, project_reserve=project,
                dividend_available_cash=available)
            row = dict(enterprise=e['id'], owner=e['owner_id'],
                owner_kind=self.accounts[e['owner_id']]['kind'], profit=e['profit'], dividends=amount,
                profit_limit=limit, available_cash=available, working_reserve=working, project_reserve=project)
            row['recipient_kind'] = 'household' if self.PROFIT_DISTRIBUTION_REVISION else self.accounts[e['owner_id']]['kind']
            self.report['dividends'].append(row)
            self.report['diagnostics'].append(dict(kind='dividend', **row))

    def owner_requests(self, sells):
        owners = [a for a in self.accounts.values() if a['kind'] in ('private', 'merchant')]
        for a in self.shuffled(owners):
            project = self.project_cash_reserve(a['id'], sells)
            available = self.cash_after_commitments(a['id'], project)
            budget = self.cfg['owner_consumption_rate']*available
            a.update(owner_free_cash=available, owner_consumption_budget=budget, project_reserve=project)
            row = dict(owner=a['id'], free_cash=available, project_reserve=project,
                budget=budget, spent=0.0, tax=0.0, freight=0.0, consumed={}, orders=[])
            self.report['owner_consumption'].append(row)
            for good in OWNER_BASKET:
                share = budget/len(OWNER_BASKET)
                price = self.reference_price(a, good, sells)
                quantity = share/price
                if good in self.cfg['basic_needs']:
                    food_cap = (self.OWNER_FAMILY_SIZE*self.OWNER_FOOD_QUALITY
                                * self.cfg['basic_needs'][good])
                    quantity = min(quantity, food_cap)
                _, ceiling, _ = self.quoted_cost(a, good, quantity, sells)
                order = self.order(a['id'], good, share/ceiling, 'buy', ceiling, OWNER_CONSUMPTION)
                if order:
                    row['orders'].append(order['id'])

    def consume_owner_purchases(self):
        for row in self.report['owner_consumption']:
            a = self.accounts[row['owner']]
            for order in self.report['orders']:
                if order['id'] not in row['orders']:
                    continue
                filled = order['quantity']-order['remaining']
                quantity = max(0, filled-order.get('consumed_quantity', 0))
                if quantity > EPS:
                    self.use_stock(a['id'], order['good'], quantity)
                    order['consumed_quantity'] = filled
                    good = order['good']
                    row['consumed'][good] = row['consumed'].get(good, 0)+quantity
                    self.report['owner_consumed'][good] = self.report['owner_consumed'].get(good, 0)+quantity
                    self.report['consumed'][good] = self.report['consumed'].get(good, 0)+quantity
            trades = [t for t in self.report['trades'] if t['buy_order'] in row['orders']]
            row.update(spent=sum(t['gold']+t['freight'] for t in trades),
                tax=sum(t['tax'] for t in trades), freight=sum(t['freight'] for t in trades),
                unfilled={o['good']: o['remaining'] for o in self.report['orders'] if o['id'] in row['orders']})
            row['unused_budget'] = max(0, row['budget']-row['spent'])
            a.update(owner_consumption_spent=row['spent'], owner_consumed=copy.deepcopy(row['consumed']),
                owner_consumption_tax=row['tax'], owner_consumption_freight=row['freight'])

    def transfer(self, sender, recipient, amount, reason):
        if amount > self.free_cash(sender)+EPS:
            raise ValueError('Деньги зарезервированы или недостаточны: '+sender)
        return super().transfer(sender, recipient, amount, reason)

    def locked_material(self, account, good):
        return self.accounts[account].get('project', {}).get('materials', {}).get(good, 0)

    def free_stock(self, account, good):
        a = self.accounts[account]
        return max(0, a['stock'].get(good, 0)-a.get('stock_reserved', {}).get(good, 0)-self.locked_material(account, good))

    def use_stock(self, account, good, quantity):
        if quantity > self.free_stock(account, good)+EPS:
            raise ValueError('Товар уже закреплён за продажей или стройкой')
        result = super().use_stock(account, good, quantity)
        self.mark_investment_dirty(good)
        return result

    def order(self, participant, good, quantity, side, price, purpose):
        if not math.isfinite(quantity) or not math.isfinite(price) or price <= 0 or quantity < 0:
            raise ValueError('Некорректная заявка')
        a = self.accounts[participant]
        if side == 'buy':
            if self.enterprises.get(participant, {}).get('status') == 'closed':
                return None
            quantity = min(quantity, self.free_cash(participant)/price)
        elif side == 'sell':
            quantity = min(quantity, self.free_stock(participant, good))
        else:
            raise ValueError('Неизвестная сторона заявки')
        if quantity <= EPS:
            return None
        o = dict(id=f'{self.meta["month"]}-{len(self.report["orders"])}', participant=participant,
            good=good, quantity=quantity, remaining=quantity, price=price, side=side,
            purpose=purpose, status='active', reason='', expires=self.meta['month'],
            final_consumption=side == 'buy' and purpose in CONSUMPTION_PURPOSES,
            state_id=a['state_id'], province_id=self.market_province(a), tile_id=self.market_tile(a),
            reserved=quantity*price if side == 'buy' else 0, backing=quantity*price if side == 'buy' else quantity)
        if side == 'buy':
            a['reserved'] = a.get('reserved', 0)+o['reserved']
        else:
            a.setdefault('stock_reserved', {})[good] = a.get('stock_reserved', {}).get(good, 0)+quantity
        self.report['orders'].append(o)
        return o

    def cancel_order(self, order, reason='Отмена заявки'):
        if order['status'] in ('cancelled', 'expired'):
            return
        filled = order['status'] == 'filled'
        a = self.accounts[order['participant']]
        if order['side'] == 'buy':
            a['reserved'] = max(0, a.get('reserved', 0)-order['reserved'])
            order['reserved'] = 0.0
        else:
            a['stock_reserved'][order['good']] = max(0, a['stock_reserved'].get(order['good'], 0)-order['remaining'])
        if not filled:
            order.update(status='cancelled', reason=reason)

    def landed(self, seller_tile, buyer_tile, good, price):
        distance, path = self.path(seller_tile, buyer_tile)
        if distance is None:
            return None
        freight = distance*self.goods[good]['weight']*self.cfg['transport_rate']
        return price+freight, freight, distance, path

    def offers(self, buyer, good, sells):
        cache = getattr(self, '_offer_index', {})
        signature = tuple((sell['id'], sell['price'], sell['tile_id'], sell['status'])
                          for sell in sells.get(good, []))
        key = (self.market_tile(buyer), good, signature)
        options = cache.get(key)
        if options is None:
            options = []
            for sell in sells.get(good, []):
                quote = self.landed(sell['tile_id'], self.market_tile(buyer), good, sell['price'])
                if quote:
                    options.append((quote[0], sell, quote))
            options.sort(key=lambda item: (item[0], item[1]['id']))
            cache[key] = options
            self._offer_index = cache
        return [(price, sell, quote) for price, sell, quote in options
                if sell['participant'] != buyer['id']
                and sell['remaining'] > EPS
                and sell['status'] not in ('cancelled', 'expired')]

    def reference_price(self, buyer, good, sells):
        offers = self.offers(buyer, good, sells)
        if offers:
            return offers[0][0]
        signal = self.market_signal(self.meta.get('market_history', []), buyer['state_id'], self.market_province(buyer), good)
        return signal['landed_price'] or self.goods[good]['price']

    def quoted_cost(self, buyer, good, quantity, sells):
        left, value, ceiling = quantity, 0.0, 0.0
        for price, sell, _ in self.offers(buyer, good, sells):
            q = min(left, sell['remaining'])
            value += q*price
            ceiling = max(ceiling, price)
            left -= q
            if left <= EPS:
                break
        if left > EPS:
            reference = self.reference_price(buyer, good, sells)
            # Ориентир следующего выпуска не является предложением товара.
            quotes = [quote[0] for firm, price in getattr(self, '_recovery_quotes', {}).get(good, [])
                if firm['id'] != buyer['id']
                and (quote := self.landed(firm['tile_id'], self.market_tile(buyer), good, price))]
            if quotes:
                reference = max(reference, min(quotes))
            value += left*reference
            ceiling = max(ceiling, reference)
        return value, max(ceiling, EPS), left <= EPS

    def input_bid_ceiling(self, buy):
        """Предельная цена автоматической закупки при зафиксированной марже."""
        firm = self.enterprises.get(buy['participant'])
        if (not firm or firm['status'] == 'closed' or firm.get('suspended')
                or not buy.get('adaptive_input_budget') or buy['side'] != 'buy'
                or buy['purpose'] != 'Сырьё' or buy.get('final_consumption')):
            return None
        plan = firm.get('production_plan', {})
        inputs = plan.get('input_ratios', {})
        prices = plan.get('input_prices', {})
        if (plan.get('month') != self.meta['month'] or plan.get('output', 0) <= EPS
                or buy['id'] not in plan.get('input_orders', [])
                or inputs.get(buy['good'], 0) <= EPS
                or not inputs.keys() <= prices.keys()
                or 'expected_net_price' not in plan or 'wage_unit' not in plan):
            return None
        other_cost = plan['wage_unit']+plan.get('fixed_unit', 0)
        for good, quantity in inputs.items():
            if good == buy['good']:
                continue
            committed = [o['price'] for o in self.report['orders']
                if o['id'] in plan['input_orders'] and o['good'] == good]
            other_cost += quantity*max([prices[good]]+committed)
        return max(0, (plan['expected_net_price']-other_cost)/inputs[buy['good']])

    def reprice_input_order(self, buy, sells):
        ceiling = self.input_bid_ceiling(buy)
        if ceiling is None or buy['remaining'] <= EPS:
            return
        a = self.accounts[buy['participant']]
        _, quote, _ = self.quoted_cost(a, buy['good'], buy['remaining'], sells)
        budget = buy['reserved']+self.free_cash(a['id'])
        price = min(quote, ceiling, budget/buy['remaining'])
        if price > buy['price']+EPS:
            extra = buy['remaining']*(price-buy['price'])
            a['reserved'] += extra
            buy['reserved'] += extra
            buy.setdefault('initial_input_limit', buy['price'])
            buy.update(price=price, backing=buy['reserved'])

    def purchase_quantity(self, buy, sells):
        """Обычный рынок не ограничивает количество сверх самой заявки."""
        return buy['remaining']

    def match(self, buy, sells):
        if buy is None or buy['status'] in ('cancelled', 'expired', 'filled'):
            return
        self.reprice_input_order(buy, sells)
        buyer = self.accounts[buy['participant']]
        if (buy.get('adaptive_household_budget') and buyer['kind'] == 'household'
                and buy.get('final_consumption') and buy['remaining'] > EPS):
            # Первая оценка корзины не является жёстким пользовательским лимитом.
            # Пересмотр возможен лишь за счёт денег вне остальных обязательств.
            _, ceiling, _ = self.quoted_cost(buyer, buy['good'], buy['remaining'], sells)
            budget = buy['reserved']+self.free_cash(buy['participant'])
            price = max(buy['price'], min(ceiling, budget/buy['remaining']))
            if price > buy['price']+EPS:
                extra = buy['remaining']*(price-buy['price'])
                buyer['reserved'] += extra
                buy['reserved'] += extra
                buy.setdefault('initial_limit', buy['price'])
                buy.update(price=price, backing=buy['reserved'])
        options = self.offers(buyer, buy['good'], sells)
        observation = self.observe_match(buy, sells, options)
        # Равная полная цена не даёт постоянного преимущества одному ID.
        ordered, index = [], 0
        while index < len(options):
            end = index+1
            while end < len(options) and math.isclose(options[index][0], options[end][0], rel_tol=1e-12, abs_tol=1e-12):
                end += 1
            ordered.extend(self.shuffled(options[index:end], key=lambda row: row[1]['id']))
            index = end
        for price, sell, quote in ordered:
            if price > buy['price']+EPS:
                continue
            q = min(buy['remaining'], sell['remaining'], buy['reserved']/price,
                self.purchase_quantity(buy, sells))
            if q <= EPS:
                continue
            seller = self.accounts[sell['participant']]
            freight, distance, path = quote[1:]
            # Освобождается только резерв исполненной части, остальной остаётся защищён.
            release = q*buy['price']
            # Допуск цены задан на единицу. На большой партии разница может
            # превысить денежный EPS: освобождаем обеспеченную фактическую сумму,
            # не затрагивая резервы других заявок и не меняя обычные сделки.
            if q*price > self.free_cash(buy['participant'])+release+EPS:
                release = min(buy['reserved'], max(release, q*price))
            buyer['reserved'] = max(0, buyer['reserved']-release)
            buy['reserved'] = max(0, buy['reserved']-release)
            seller['stock_reserved'][buy['good']] = max(0, seller['stock_reserved'][buy['good']]-q)
            value = self.use_stock(sell['participant'], buy['good'], q)
            self.add_stock(buy['participant'], buy['good'], q, q*price)
            gross = q*sell['price']
            tax = gross*self.cfg['consumption_tax'] if buy.get('final_consumption', False) else 0.0
            flow_start = len(self.report['flows'])
            trade_id = f'{self.meta["month"]}:trade:{len(self.report["trades"])}'
            self.transfer(buy['participant'], sell['participant'], gross-tax, 'Товар')
            self.transfer(buy['participant'], 'treasury:'+seller['state_id'], tax, 'Налог с конечной потребительской продажи')
            self.transfer(buy['participant'], 'carrier:'+seller['state_id'], q*freight, 'Перевозка')
            for flow in self.report['flows'][flow_start:]:
                flow.update(buy_order=buy['id'], sell_order=sell['id'], good=buy['good'], quantity=q,
                    trade_id=trade_id)
            firm = self.enterprises.get(sell['participant'])
            if firm:
                firm['revenue'] += gross
                firm['tax'] = firm.get('tax', 0)+tax
                firm['cost_of_sales'] = firm.get('cost_of_sales', 0)+value
            if buy['purpose'] == 'Строительные материалы':
                materials = buyer['project']['materials']
                materials[buy['good']] = materials.get(buy['good'], 0)+q
            buy['remaining'] = max(0, buy['remaining']-q)
            sell['remaining'] = max(0, sell['remaining']-q)
            self.report['trades'].append(dict(trade_id=trade_id, good=buy['good'], quantity=q, price=sell['price'],
                gold=gross, seller_gold=gross-tax, tax=tax, freight=q*freight, distance=distance, path=path,
                buyer=buy['participant'], seller=sell['participant'], buy_order=buy['id'], sell_order=sell['id'],
                buyer_state=buy['state_id'], seller_state=sell['state_id'],
                buyer_province=buy['province_id'], seller_province=sell['province_id'],
                purpose=buy['purpose'], final_consumption=buy.get('final_consumption', False)))
            sell['status'] = 'filled' if sell['remaining'] <= EPS else 'partial'
            if buy['remaining'] <= EPS:
                break
        buy['status'] = 'filled' if buy['remaining'] <= EPS else 'partial' if buy['remaining'] < buy['quantity'] else 'unfilled'
        self.finish_match_observation(buy, sells, observation)
        if buy['remaining'] > EPS:
            buy['reason'] = observation['reason_code']

    def observe_match(self, buy, sells, options):
        # Снимок снимается до исполнения, без изменения рынка и генератора случайных чисел.
        quotes = {sell['id']: quote for _, sell, quote in options}
        candidates = []
        for sell in sells.get(buy['good'], []):
            if sell['participant'] == buy['participant']:
                continue
            active = sell['status'] not in ('cancelled', 'expired') and sell['remaining'] > EPS
            quote = quotes.get(sell['id']) or self.landed(sell['tile_id'], buy['tile_id'], buy['good'], sell['price'])
            candidates.append(dict(sell_order=sell['id'], seller=sell['participant'],
                price=sell['price'], remaining_before=sell['remaining'], status_before=sell['status'],
                active=active, route_available=quote is not None,
                landed_price=quote[0] if quote else None, freight_per_unit=quote[1] if quote else None,
                distance=quote[2] if quote else None,
                physical_stock=self.accounts[sell['participant']]['stock'].get(buy['good'], 0)))
        observation = dict(version=1, match_index=sum('match_observation' in o for o in self.report['orders']),
            remaining_before=buy['remaining'], reserved_before=buy['reserved'], candidates=candidates)
        buy['match_observation'] = observation
        return observation

    def finish_match_observation(self, buy, sells, observation):
        by_id = {sell['id']: sell for sell in sells.get(buy['good'], [])}
        for row in observation['candidates']:
            row['remaining_after'] = by_id[row['sell_order']]['remaining']
        active = [r for r in observation['candidates'] if r['active'] and r['remaining_after'] > EPS]
        reachable = [r for r in active if r['route_available']]
        affordable = [r for r in reachable if r['landed_price'] <= buy['price']+EPS]
        holders = [dict(account=a['id'], quantity=a['stock'][buy['good']],
            stock_reserved=a.get('stock_reserved', {}).get(buy['good'], 0),
            project_locked=self.locked_material(a['id'], buy['good']))
            for a in self.accounts.values() if a['id'] != buy['participant'] and a['stock'].get(buy['good'], 0) > EPS]
        if buy['remaining'] <= EPS:
            code = 'filled'
        elif affordable:
            code = 'insufficient_order_reserve' if all(
                min(buy['remaining'], r['remaining_after'], buy['reserved']/r['landed_price']) <= EPS
                for r in affordable) else 'executable_not_matched'
        elif reachable:
            code = 'landed_price_above_limit'
        elif active:
            code = 'offer_exhausted' if any(r['route_available'] and r['remaining_after'] <= EPS
                and r['status_before'] not in ('cancelled', 'expired')
                for r in observation['candidates']) else 'no_route'
        elif any(r['remaining_before'] > EPS and r['active'] or
                 r['remaining_before'] <= EPS and by_id[r['sell_order']]['quantity'] > EPS
                 and r['status_before'] not in ('cancelled', 'expired')
                 for r in observation['candidates']):
            code = 'offer_exhausted'
        else:
            code = 'stock_not_offered' if holders else 'no_physical_stock'
        observation.update(reason_code=code, remaining_after=buy['remaining'], reserved_after=buy['reserved'])
        if buy['remaining'] > EPS:
            observation['stock_holders_after_match'] = holders

    def sell_orders(self):
        # Новый набор предложений начинается с каждого торгового прохода.
        self.__dict__.pop('_offer_index', None)
        result = defaultdict(list)
        for a in self.shuffled(self.accounts.values()):
            e = self.enterprises.get(a['id'])
            if a['kind'] == 'household' or (e and e.get('suspended')):
                continue
            for good in sorted(a['stock']):
                quantity = self.free_stock(a['id'], good)
                if e and e['status'] != 'closed' and good != e['good']:
                    recipe = self.goods[e['good']]
                    history = e.get('sales_history', [])[-3:]
                    planned = e.get('production_plan', {}).get('output', 0)
                    recent = max((h.get('output', 0) for h in history), default=e['capacity']*self.rules['initial_sales_fraction'])
                    reserve = min(e['capacity'], max(planned, recent))*self.cfg['input_buffer']
                    quantity = max(0, quantity-reserve*recipe['inputs'].get(good, 0))
                prices = a.setdefault('ask_prices', {})
                price = e['price'] if e and good == e['good'] else prices.setdefault(good, self.goods[good]['price'])
                o = self.order(a['id'], good, quantity, 'sell', price, 'Продажа запасов')
                if o:
                    result[good].append(o)
        return result

    def customer_quotes(self, e, orders=None):
        result = []
        orders = getattr(self, '_planning_orders', self.report.get('orders', [])) if orders is None else orders
        for o in orders:
            if o['side'] != 'buy' or o['good'] != e['good'] or o['participant'] == e['id'] or o['remaining'] <= EPS or o['status'] in ('cancelled', 'expired'):
                continue
            quote = self.landed(e['tile_id'], o['tile_id'], e['good'], e['price'])
            if quote:
                result.append((o, o['price']-quote[1]))
        return result

    def reachable_demand(self, e, sells):
        demand = 0.0
        remaining = {sell['id']: sell['remaining'] for offers in sells.values() for sell in offers}
        for order, limit in self.customer_quotes(e):
            if limit+EPS < e['price']:
                continue
            buyer = self.accounts[order['participant']]
            landed = self.landed(e['tile_id'], order['tile_id'], e['good'], e['price'])[0]
            volume = min(order['remaining'], order['reserved']/landed)
            for price, sell, _ in self.offers(buyer, e['good'], sells):
                if sell['participant'] == e['id'] or price >= landed-EPS:
                    continue
                covered = min(volume, remaining[sell['id']])
                volume -= covered
                remaining[sell['id']] -= covered
            demand += volume
        return demand

    def unit_cost(self, e, sells):
        recipe = self.goods[e['good']]
        return recipe['labor']*self.cfg['wage']+sum(ratio*self.reference_price(e, good, sells) for good, ratio in recipe['inputs'].items())

    def volume_input_prices(self, e, quantity, sells):
        """Средние доставленные цены входов для объёма производственного плана."""
        prices = {}
        account = self.accounts[e['id']]
        for good, ratio in self.goods[e['good']]['inputs'].items():
            missing = max(0, quantity*ratio-self.free_stock(e['id'], good))
            if missing > EPS:
                value, _, _ = self.quoted_cost(account, good, missing, sells)
                prices[good] = value/missing
            else:
                prices[good] = self.reference_price(e, good, sells)
        return prices

    def net_price(self, e):
        # Налог оценивается по доступным заявкам; при их отсутствии — осторожно.
        consumer = e['good'] in (self.cfg['basic_needs'] | self.cfg['extra_needs'])
        quantity, final_quantity = 0.0, 0.0
        for order, limit in self.customer_quotes(e):
            if limit+EPS < e['price']:
                continue
            landed = self.landed(e['tile_id'], order['tile_id'], e['good'], e['price'])[0]
            volume = min(order['remaining'], order['reserved']/max(landed, EPS))
            quantity += volume
            if order.get('final_consumption'):
                final_quantity += volume
        share = (final_quantity/quantity
            if quantity > EPS else float(consumer))
        return e['price']*(1-self.cfg['consumption_tax']*share)

    def prepare_recovery_quotes(self):
        """Одна оценка воспроизводства снизу вверх, без будущих сделок и эмиссии."""
        self._recovery_quotes = defaultdict(list)
        for e in sorted(self.enterprises.values(), key=lambda f: (self.depth(f['good']), f['id'])):
            e.pop('production_price', None)
            if e.get('suspended'):
                continue
            recipe = self.goods[e['good']]
            if recipe['resource'] and not any(t['province_id'] == e['province_id']
                    and recipe['resource'] in t['resources'] for t in self.tiles.values()):
                continue
            cost = recipe['labor']*self.cfg['wage']
            for good, ratio in recipe['inputs'].items():
                quotes = [quote[0] for firm, price in self._recovery_quotes[good]
                    if (quote := self.landed(firm['tile_id'], e['tile_id'], good, price))]
                cost += ratio*(min(quotes) if quotes else self.goods[good]['price'])
            consumer = e['good'] in (self.cfg['basic_needs'] | self.cfg['extra_needs'])
            net = 1-self.cfg['consumption_tax'] if consumer else 1
            price = (cost*(1+self.cfg['markup'])/net
                if e['price']*net < cost-EPS else e['price'])
            e['production_price'] = min(self.rules['maximum_price'], price)
            self._recovery_quotes[e['good']].append((e, e['production_price']))

    def recover_enterprises(self, sells, enterprises=None):
        """Собственник финансирует действующую мощность до нового строительства."""
        for e in self.shuffled(self.enterprises.values() if enterprises is None else enterprises):
            if e.get('suspended') or e['available_from'] > self.meta['month']:
                continue
            outlook = self.production_outlook(e, sells=sells)
            target = outlook['planned_output']
            if (target <= EPS or outlook['expected_margin'] <= EPS
                    or outlook['reachable_demand'] <= EPS):
                continue
            a = self.accounts[e['id']]
            recipe = self.goods[e['good']]
            required = target*recipe['labor']*self.cfg['wage']
            for good, ratio in recipe['inputs'].items():
                missing = max(0, target*ratio-self.free_stock(e['id'], good))
                required += missing*self.quoted_cost(a, good, missing, sells)[1]
            owner = self.accounts[e['owner_id']]
            available = self.cash_after_commitments(owner['id'], self.project_cash_reserve(owner['id'], sells))
            amount = min(max(0, required-self.free_cash(e['id'])), available)
            self.transfer(owner['id'], e['id'], amount, 'Восстановление оборотного капитала')
            if e['status'] == 'closed' and self.free_cash(e['id']) > EPS:
                e.update(status='idle', closed=None, unviable_months=0)
                for offers in sells.values():
                    for offer in offers:
                        if offer['participant'] == e['id'] and offer['good'] != e['good']:
                            self.cancel_order(offer, 'Сырьё возвращено в производство')
                self.report['lifecycle'].append(dict(enterprise=e['id'], good=e['good'], action='reopened',
                    reason='Восстановлен обеспеченный рентабельный спрос'))
            if e['status'] != 'closed':
                hold = min(self.free_cash(e['id']), required)
                a['operating_protected'] = hold
                a['protected'] += hold

    def expected_stock_sales(self, e, sells):
        """Продажа старого склада до поступления планируемого нового выпуска."""
        offered = sum(o['remaining'] for o in sells.get(e['good'], [])
            if o['participant'] == e['id'] and o['status'] not in ('cancelled', 'expired'))
        if offered <= EPS:
            return 0.0
        cache = getattr(self, '_stock_sales_cache', {})
        cached = cache.get(e['id'])
        if cached is not None:
            return cached
        reachable = self.reachable_demand(e, sells)
        history = e.get('sales_history', [])[-3:]
        average_sold = (sum(h.get('sold', 0.0) for h in history) / len(history)
                        if history else 0.0)
        expected = max(reachable, average_sold)
        result = min(self.accounts[e['id']]['stock'].get(e['good'], 0), offered, expected)
        cache[e['id']] = result
        self._stock_sales_cache = cache
        return result

    def production_outlook(self, e, sells=None):
        e = dict(e)
        sells = sells or defaultdict(list)
        cost = self.unit_cost(e, sells)
        # Рентабельную текущую цену (включая B2B без налога) повышать не требуется.
        price = e['price'] if self.net_price(e)+EPS >= cost else e.get('production_price', e['price'])
        quote = dict(e, price=price)
        observations = e.get('sales_history', [])[-3:]
        own = sum(h['sold'] for h in observations)/len(observations) if observations else e['sales_forecast']
        demand = self.reachable_demand(quote, sells)
        sold_now = sum(t['quantity'] for t in self.report['trades'] if t['seller'] == e['id'] and t['good'] == e['good'])
        forecast = max(own, sold_now+demand)
        stock = self.accounts[e['id']]['stock'].get(e['good'], 0)
        # Начальный склад сначала участвует в торгах этого месяца; новый выпуск
        # продаётся в следующем. Восполняем прогнозируемый остаток после торгов.
        expected_sales = self.expected_stock_sales(e, sells) if self.STOCK_PLANNING_REVISION else 0.0
        projected_stock = max(0, stock-expected_sales)
        e.update(sales_forecast=forecast, reachable_demand=demand,
            expected_unit_cost=cost, expected_price=self.net_price(quote), planned_price=price)
        target = max(0, min(e['capacity'], forecast-projected_stock))
        previous = e.get('planned_output', 0)
        # При обычном падении спроса мощность сокращается постепенно. Если склад
        # переполнен, быстрый спад разрешён, чтобы не накапливать лишний товар.
        # Исторические исследовательские варианты сохраняют прежнюю формулу.
        if (self.STOCK_PLANNING_REVISION >= 2 and target < previous-EPS
                and not (stock > forecast*self.cfg['output_overstock_factor'])):
            target = previous - self.cfg['output_decline_rate']*(previous-target)
            target = max(0, min(e['capacity'], target))
        fixed_unit = self.enterprise_overhead(e)/target if target > EPS else 0.0
        e.update(expected_fixed_cost=self.enterprise_overhead(e), expected_fixed_unit=fixed_unit,
                 expected_margin=e['expected_price']-e['expected_unit_cost']-fixed_unit)
        e['production_limit'] = 'Готовые запасы' if projected_stock >= forecast and forecast > EPS else 'Нет обеспеченного спроса' if forecast <= EPS else 'План'
        if e['expected_margin'] < -EPS:
            target, e['production_limit'] = 0, 'Отрицательная ожидаемая маржа'
        e['planned_output'] = target
        e['input_target_output'] = target
        return {k: e[k] for k in ('sales_forecast', 'reachable_demand', 'expected_unit_cost',
            'expected_price', 'expected_margin', 'production_limit', 'planned_output', 'input_target_output',
            'planned_price', 'expected_fixed_cost', 'expected_fixed_unit')}

    def plan(self, e, history=None, sells=None):
        e.update(self.production_outlook(e, sells))
        return e['planned_output']

    def update_prices(self):
        subjects = [(e, e.get('sales_history', []), e, 'price') for e in self.enterprises.values()]
        for a in self.accounts.values():
            if a['kind'] == 'household':
                continue
            firm = self.enterprises.get(a['id'])
            for good, quantity in a['stock'].items():
                if quantity <= EPS or (firm and firm['good'] == good):
                    continue
                prices = a.setdefault('ask_prices', {})
                price = prices.setdefault(good, self.goods[good]['price'])
                seller = dict(id=a['id'], good=good, price=price, tile_id=self.market_tile(a))
                subjects.append((seller, a.get('stock_sales_history', {}).get(good, []), prices, good))
        for e, observations, target, price_key in subjects:
            if not observations:
                continue
            last, price = observations[-1], e['price']
            limits = [limit for o, limit in self.customer_quotes(e, self.meta.get('public_orders', [])) if limit > 0]
            competitors = []
            for other in self.enterprises.values():
                if other['id'] == e['id'] or other['good'] != e['good'] or self.accounts[other['id']]['stock'].get(e['good'], 0) <= EPS:
                    continue
                quote = self.landed(other['tile_id'], e['tile_id'], e['good'], other['price'])
                if quote:
                    competitors.append(quote[0])
            if limits and last['sold'] > EPS and max(limits) > price+EPS:
                price = min(price*(1+self.rules['price_step']), max(limits))
            elif limits and max(limits) < price-EPS:
                price = max(price*(1-self.rules['price_step']), max(limits))
            elif last['sold'] <= EPS and competitors and min(competitors) < price-EPS:
                price = max(price*(1-self.rules['price_step']), min(competitors))
            elif last['sold'] <= EPS and competitors and min(competitors) <= price+EPS:
                a = self.accounts[e['id']]
                signal = self.market_signal(self.meta.get('market_history', []), a['state_id'], a['province_id'], e['good'])
                if signal['volume'] > EPS:
                    price *= 1-self.rules['price_step']
            else:
                budgets = []
                for o in self.meta.get('price_feedback', []):
                    if o['good'] != e['good'] or not o.get('unfunded_need'):
                        continue
                    quote = self.landed(e['tile_id'], o['tile_id'], e['good'], price)
                    if quote:
                        budgets.append(o['budget_price']-quote[1])
                declining = len(observations) > 1 and last['sold']+EPS < observations[-2]['sold']
                if budgets and max(budgets) > 0 and max(budgets) < price-EPS and (declining or last['sold'] <= EPS):
                    price = max(price*(1-self.rules['price_step']), max(budgets))
            target[price_key] = min(self.rules['maximum_price'], max(self.rules['minimum_price'], price))

    def household_requests(self, households, needs, purpose, sells):
        if self.BASIC_BUDGET_REVISION and purpose == 'Базовые нужды':
            for a in households:
                # Стоимость полной месячной потребности с доставкой, не цена тонны.
                goods = sorted(needs, key=lambda g: self.population(a)*needs[g]*
                    self.quoted_cost(a, g, max(0, self.population(a)*needs[g]-a['stock'].get(g, 0)), sells)[1])
                for good in goods:
                    self._balanced_household_requests([a], {good: needs[good]}, purpose, sells)
            return
        self._balanced_household_requests(households, needs, purpose, sells)

    def _balanced_household_requests(self, households, needs, purpose, sells):
        for a in households:
            requests = []
            for good, rate in needs.items():
                needed = self.population(a)*rate
                quantity = max(0, needed-a['stock'].get(good, 0))
                _, price, _ = self.quoted_cost(a, good, quantity, sells)
                requests.append((good, needed, quantity, price))
            total = sum(q*price for _, _, q, price in requests)
            fraction = min(1, self.free_cash(a['id'])/total) if total > EPS else 0
            for good, needed, quantity, price in requests:
                a.setdefault('needs', {})[good] = dict(needed=needed, consumed=0, deficit=needed)
                order = self.order(a['id'], good, quantity*fraction, 'buy', price, purpose)
                if order:
                    order.update(need_quantity=quantity, budget_price=price*fraction, unfunded_need=fraction < 1-EPS,
                        adaptive_household_budget=True)

    def household_top_up(self, households, sells):
        """Допокупка базовой еды после освобождения старых резервов, до доходов."""
        for o in self.report['orders']:
            if o['side'] == 'buy' and self.accounts[o['participant']]['kind'] == 'household':
                o['reserved_before_top_up'] = o['reserved']
                self.cancel_order(o, 'Пересмотр остатка потребительского бюджета')
                o['superseded_by_top_up'] = True
        candidates = [a for a in households if self.free_cash(a['id']) > EPS
            and any(self.free_stock(a['id'], g) + EPS < self.population(a)*rate
                for g, rate in self.cfg['basic_needs'].items())]
        for a in self.shuffled(candidates):
            for good in self.shuffled(self.cfg['basic_needs'], key=lambda g: g):
                needed = self.population(a)*self.cfg['basic_needs'][good]
                for price, sell, _ in self.offers(a, good, sells):
                    missing = max(0, needed-self.free_stock(a['id'], good))
                    if missing <= EPS or self.free_cash(a['id']) <= EPS:
                        break
                    quantity = min(missing, sell['remaining'], self.free_cash(a['id'])/price)
                    buy = self.order(a['id'], good, quantity, 'buy', price, 'Базовые нужды')
                    if buy:
                        buy['household_top_up'] = True
                        self.match(buy, sells)
                        self.cancel_order(buy, 'Окончание допокупки')
        # Новая книга остаточного спроса обеспечена оставшейся кассой.
        # Это не повторное наблюдение начального бюджета исследовательскими классами.
        first = len(self.report['orders'])
        Economy._balanced_household_requests(self, households, self.cfg['basic_needs'], 'Базовые нужды', sells)
        Economy._balanced_household_requests(self, households, self.cfg['extra_needs'], 'Дополнительные нужды', sells)
        for buy in self.report['orders'][first:]:
            buy['household_top_up_residual'] = True
            buy['household_top_up'] = True
            self.match(buy, sells)
        top_up_ids = {o['id'] for o in self.report['orders'] if o.get('household_top_up')}
        trades = [t for t in self.report['trades'] if t['buy_order'] in top_up_ids]
        self.report['household_top_up'] = dict(quantity=sum(t['quantity'] for t in trades),
            gold=sum(t['gold']+t['freight'] for t in trades),
            by_good={g: sum(t['quantity'] for t in trades if t['good'] == g)
                for g in self.cfg['basic_needs']})

    def depth(self, good):
        inputs = self.goods[good]['inputs']
        return 1+max((self.depth(g) for g in inputs), default=-1)

    def input_requests(self, e, sells):
        a = self.accounts[e['id']]
        input_prices = {good: self.reference_price(a, good, sells)
            for good in self.goods[e['good']]['inputs']}
        a['protected'] = max(0, a.get('protected', 0)-a.pop('operating_protected', 0))
        target = self.plan(e, sells=sells)
        a, recipe = self.accounts[e['id']], self.goods[e['good']]
        cash = self.free_cash(e['id'])
        requested = target
        first_order = len(self.report['orders'])

        def cash_needed(quantity):
            needed = self.enterprise_overhead(e)+quantity*recipe['labor']*self.cfg['wage']
            for good, ratio in recipe['inputs'].items():
                missing = max(0, quantity*ratio-self.free_stock(e['id'], good))
                needed += missing*self.quoted_cost(a, good, missing, sells)[1]
            return needed

        # Оплаченное ранее сырьё влияет на маржу, но не требует второй оплаты из кассы.
        if cash_needed(target) > cash:
            low, high = 0.0, target
            for _ in range(40):
                middle = (low+high)/2
                if cash_needed(middle) <= cash:
                    low = middle
                else:
                    high = middle
            target = low
        input_prices = self.volume_input_prices(e, target, sells)
        # Единственный окончательный план: выпуск не сможет увеличить его после закупок.
        e['planned_output'] = e['input_target_output'] = target
        if target+EPS < requested:
            e['production_limit'] = 'Деньги на выпуск'
        e['production_plan'] = dict(month=self.meta['month'], output=target,
            sale_price=e['planned_price'],
            expected_net_price=e['expected_price'], input_prices=input_prices,
            input_ratios=dict(recipe['inputs']), wage_unit=recipe['labor']*self.cfg['wage'],
            fixed_unit=e.get('expected_fixed_unit', 0),
            requested_output=requested, free_cash=cash, known_order_count=first_order,
            reachable_demand=e['reachable_demand'], sales_forecast=e['sales_forecast'],
            expected_margin=e['expected_margin'], reason=e['production_limit'])
        a['payroll_protected'] = target*recipe['labor']*self.cfg['wage']
        a['protected'] += a['payroll_protected']
        for good, ratio in recipe['inputs'].items():
            quantity = max(0, target*ratio-self.free_stock(e['id'], good))
            _, price, _ = self.quoted_cost(a, good, quantity, sells)
            order = self.order(e['id'], good, quantity, 'buy', price, 'Сырьё')
            if order:
                order['adaptive_input_budget'] = True
        e['production_plan']['input_orders'] = [o['id'] for o in self.report['orders'][first_order:]]

    def investment_demand(self, candidate, sells):
        """Сначала пригодные мощности и начатые проекты, затем новая стройка."""
        suppliers = []
        cache = getattr(self, '_investment_supply_cache', None)
        cached = cache.get(candidate['good']) if cache is not None else None
        for firm in (() if cached is not None else self.enterprises.values()):
            if firm['good'] != candidate['good'] or firm['status'] == 'closed' or firm.get('suspended'):
                continue
            cost = self.unit_cost(firm, sells)
            price = firm['price'] if self.net_price(firm)+EPS >= cost else firm.get('production_price', firm['price'])
            if self.net_price(dict(firm, price=price))+EPS < cost:
                continue
            owner = self.accounts[firm['owner_id']]
            funds = self.free_cash(firm['id'])+self.cash_after_commitments(owner['id'],
                self.project_cash_reserve(owner['id'], sells))
            if funds <= EPS:
                continue
            labor = sum(self.population(a)*self.cfg['labor_share'] for a in self.workforce(firm))
            recipe = self.goods[firm['good']]
            capacity = min(firm['capacity'], labor/recipe['labor'])
            if recipe['resource']:
                resource = sum(self.cfg['extraction_per_tile'] for t in self.tiles.values()
                    if t['province_id'] == firm['province_id'] and recipe['resource'] in t['resources'])
                capacity = min(capacity, resource)
            suppliers.append([firm, price, capacity])
        if cached is not None:
            suppliers = [list(s) for s in cached]
        elif cache is not None:
            cache[candidate['good']] = [tuple(s) for s in suppliers]
        for a in self.accounts.values():
            p = a.get('project')
            if p and p['good'] == candidate['good']:
                suppliers.append([dict(p['location']), p['expected_price'], p['capacity']])
        residual = 0.0
        cheap = {o['id']: o['remaining'] for offers in sells.values() for o in offers}
        for order, limit in self.customer_quotes(candidate):
            if limit+EPS < candidate['price']:
                continue
            buyer = self.accounts[order['participant']]
            landed = self.landed(candidate['tile_id'], order['tile_id'], candidate['good'], candidate['price'])[0]
            volume = min(order['remaining'], order['reserved']/landed)
            for price, sell, _ in self.offers(buyer, candidate['good'], sells):
                if price > order['price']+EPS:
                    continue
                covered = min(volume, cheap[sell['id']])
                cheap[sell['id']] -= covered
                volume -= covered
            for supply in suppliers:
                quote = self.landed(supply[0]['tile_id'], order['tile_id'], candidate['good'], supply[1])
                if quote and quote[0] <= order['price']+EPS:
                    covered = min(volume, supply[2])
                    supply[2] -= covered
                    volume -= covered
            residual += volume
        return residual

    def project_candidate(self, investor, sells, derived_demand=None, demand_region=None,
                          parent_project_id=None, diagnostics=None):
        candidates = []
        history = self.meta.get('market_history', [])
        markets = {(o['province_id'], o['good']) for o in self.report['orders'] if o['side'] == 'buy' and o['state_id'] == investor['state_id'] and o['province_id']}
        markets |= {(r['province_id'], r['good']) for h in history for r in h['markets'] if r['state_id'] == investor['state_id'] and r['province_id']}
        # Рынок сбыта и площадка добычи могут находиться в разных провинциях.
        resource_goods = {good for _, good in markets if self.goods[good]['resource']}
        markets |= {(t['province_id'], good) for t in self.tiles.values() for good in resource_goods
            if t['province_id'] and self.owners.get(t['province_id']) == investor['state_id']
            and self.goods[good]['resource'] in t['resources']}
        if derived_demand:
            for good, amount in derived_demand.items():
                if amount > EPS and demand_region:
                    markets.add((demand_region, good))
        expansion = self.enterprises.get(investor['id'])
        for pid, good in sorted(markets):
            if expansion and (good != expansion['good'] or pid != expansion['province_id']):
                continue
            loc = {k: expansion[k] for k in ('state_id', 'province_id', 'city_id', 'tile_id')} if expansion else self.location(pid)
            if loc['state_id'] != investor['state_id']:
                continue
            recipe = self.goods[good]
            if recipe['resource']:
                tiles = [t for t in self.tiles.values() if t['province_id'] == pid and recipe['resource'] in t['resources']]
                if not tiles:
                    continue
                loc.update(tile_id=expansion['tile_id'] if expansion else self.shuffled(tiles)[0]['id'], city_id=None)
            quote_firm = dict(id=investor['id'], good=good, price=expansion['price'] if expansion else self.goods[good]['price'], **loc)
            limits = [limit for _, limit in self.customer_quotes(quote_firm) if limit > 0]
            signal = self.market_signal(history, investor['state_id'], pid, good)
            price = max(limits) if limits else signal['price'] or self.goods[good]['price']
            quote_firm['price'] = price
            demand = max(0, self.investment_demand(quote_firm, sells)-investor['stock'].get(good, 0))
            if derived_demand and good in derived_demand:
                demand = max(demand, derived_demand[good])
            if demand < self.cfg['minimum_capacity']:
                if diagnostics is not None:
                    diagnostics.append(dict(investor=investor['id'], good=good,
                        province_id=pid, stage='demand', demand=demand,
                        minimum=self.cfg['minimum_capacity']))
                continue
            capacity = min(demand, derived_demand[good]) if derived_demand and good in derived_demand else demand
            labor = sum(self.population(a)*self.cfg['labor_share'] for a in self.accounts.values()
                if a['kind'] == 'household' and a['state_id'] == loc['state_id'] and a['province_id'] == pid)
            capacity = min(capacity, labor/recipe['labor'])
            if recipe['resource']:
                used = sum(e['capacity'] for e in self.enterprises.values() if e['status'] != 'closed' and e['province_id'] == pid and self.goods[e['good']]['resource'] == recipe['resource'])
                capacity = min(capacity, max(0, len(tiles)*self.cfg['extraction_per_tile']-used))
            if capacity < self.cfg['minimum_capacity']:
                if diagnostics is not None:
                    diagnostics.append(dict(investor=investor['id'], good=good,
                        province_id=pid, stage='capacity', capacity=capacity,
                        minimum=self.cfg['minimum_capacity']))
                continue
            unit = self.unit_cost(quote_firm, sells)
            fixed_unit = recipe['labor']*self.cfg['wage']*self.cfg.get('enterprise_overhead_rate', 0)
            margin = self.net_price(quote_firm)-unit-fixed_unit
            if margin <= EPS:
                if diagnostics is not None:
                    diagnostics.append(dict(investor=investor['id'], good=good,
                        province_id=pid, stage='margin', margin=margin,
                        unit_cost=unit+fixed_unit, price=self.net_price(quote_firm)))
                continue
            distance, _ = self.path(self.market_tile(investor), loc['tile_id'])
            if distance is None:
                if diagnostics is not None:
                    diagnostics.append(dict(investor=investor['id'], good=good,
                        province_id=pid, stage='route'))
                continue
            bill = self.construction_bill(1)
            material = sum(q*self.quoted_cost(investor, g, q, sells)[0]/max(q, EPS) for g, q in bill.items())
            delivery = sum(q*self.goods[g]['weight'] for g, q in bill.items())*distance*self.cfg['transport_rate']
            available = self.free_cash(investor['id'])
            startup = 0 if expansion else self.cfg['startup_cash']
            capacity = min(capacity, max(0, available-startup)/max(material+delivery+unit, EPS))
            if capacity < self.cfg['minimum_capacity']:
                if diagnostics is not None:
                    diagnostics.append(dict(investor=investor['id'], good=good,
                        province_id=pid, stage='funding_capacity', capacity=capacity,
                        minimum=self.cfg['minimum_capacity']))
                continue
            working = 0 if expansion else max(startup, capacity*(unit+fixed_unit))
            investment = capacity*(material+delivery)+working
            profit = capacity*margin
            self.profile(investor)
            horizon = self.cfg['investment_payback_months']*(0.5+investor['risk_tolerance'])
            if investment > available+EPS or profit*horizon < investment:
                if diagnostics is not None:
                    diagnostics.append(dict(investor=investor['id'], good=good,
                        province_id=pid, stage='payback', investment=investment,
                        available=available, profit=profit, horizon=horizon))
                continue
            # Финансирование выше может уменьшить мощность. Требования цепочки
            # должны соответствовать именно итоговому проекту, а не ранней
            # оценке спроса.
            chain_requirements = self.recipe_requirements(good, capacity)
            project_id = f"{investor['id']}:{good}:{pid}:{self.meta['month']}"
            candidates.append(dict(good=good, capacity=capacity, location=loc, materials={},
                working_cash=working, expected_price=price, expected_profit=profit,
                expected_investment=investment, expected_unit_cost=unit+fixed_unit, expansion=bool(expansion),
                created=self.meta['month'], chain_requirements=chain_requirements,
                project_id=project_id,
                parent_project_id=parent_project_id,
                derived_demand=derived_demand.get(good) if derived_demand else None,
                dependency_expires=self.meta['month']+self.cfg.get('project_stall_months', 12)
                    if parent_project_id else None))
        return max(candidates, key=lambda p: p['expected_profit']*self.cfg['investment_payback_months']-p['expected_investment']) if candidates else None

    def upstream_expansion_candidate(self, good, state_id, sells, shortage,
                                     province_id, parent_project_id):
        """Найти расширяемую мощность для дефицитного звена цепочки."""
        cache = getattr(self, '_upstream_candidate_cache', {})
        key = (good, state_id, round(shortage, 8), province_id)
        if key in cache:
            return cache[key]
        candidates = []
        diagnostics = []
        for firm in self.enterprises.values():
            if (firm['good'] != good or firm['status'] == 'closed'
                    or firm.get('suspended') or firm['state_id'] != state_id):
                continue
            account = self.accounts[firm['id']]
            if account.get('project'):
                continue
            project = self.project_candidate(account, sells,
                # Расширение происходит в провинции самого поставщика;
                # доставка до верхнего проекта проверяется его маржой.
                derived_demand={good: shortage}, demand_region=firm['province_id'],
                parent_project_id=parent_project_id, diagnostics=diagnostics)
            if project and project['good'] == good and project['expansion']:
                candidates.append((account, project))
        result = max(candidates, key=lambda row: row[1]['capacity'], default=None)
        cache[key] = result
        self._upstream_candidate_diagnostics = getattr(self, '_upstream_candidate_diagnostics', {})
        self._upstream_candidate_diagnostics[key] = diagnostics
        self._upstream_candidate_cache = cache
        return result

    def project_requests(self, sells):
        due = self.meta['month'] % self.cfg['investment_interval'] == 0
        evaluate = due or bool(self.meta.get('investment_dirty'))
        investors = [a for a in self.accounts.values() if a['kind'] in ('private', 'merchant', 'treasury', 'municipal') or
            (a['kind'] == 'enterprise' and self.enterprises[a['id']]['status'] != 'closed' and self.enterprises[a['id']]['available_from'] <= self.meta['month'])]
        # Последующие инвесторы учитывают уже объявленные проекты; торгов ещё нет.
        self._investment_supply_cache = {}
        self._investment_input_reserved = defaultdict(float)
        self._upstream_candidate_cache = {}
        diagnostic_demands = []
        flow_balances = None
        if evaluate:
            state_ids = sorted({a['state_id'] for a in self.accounts.values() if a.get('state_id')})
            flow_balances = {(state_id, good): self.investment_flow_balance(state_id, good)
                for state_id in state_ids for good in self.goods}
        queue = self.meta.setdefault('investment_queue', [])
        waiting = []
        for item in queue:
            investor = self.accounts.get(item.get('investor'))
            project = item.get('project')
            if not investor or investor.get('project') or not project:
                continue
            ready = all(project['capacity']*ratio <=
                sum(sell['remaining'] for _, sell, _ in self.offers(investor, good, sells))+EPS
                for good, ratio in self.goods[project['good']]['inputs'].items())
            if ready:
                investor['project'] = project
                self.report['investment_decisions'].append(dict(
                    kind='investment_decision', investor=investor['id'], outcome='resumed',
                    **copy.deepcopy(project)))
            else:
                waiting.append(item)
        queue[:] = waiting
        for a in self.shuffled(investors):
            project = self.project_candidate(a, sells) if evaluate and 'project' not in a else None
            if project:
                demand = dict(state_id=a['state_id'], good=project['good'],
                    quantity=project['capacity'], decision_id=project['project_id'],
                    project_id=project['project_id'], investor=a['id'],
                    province_id=project['location']['province_id'])
                diagnostic_demands.append(demand)
                self.report['investment_candidates'].append(copy.deepcopy(demand))
                feasible = True
                for input_good, ratio in self.goods[project['good']]['inputs'].items():
                    required = project['capacity']*ratio
                    available = sum(sell['remaining'] for _, sell, _ in self.offers(a, input_good, sells))
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
                            candidate_diagnostics=getattr(self, '_upstream_candidate_diagnostics', {}).get(
                                (input_good, a['state_id'], round(shortage, 8), project['location']['province_id']), []))
                    if upstream:
                        owner, upstream_project = upstream
                        if not owner.get('project'):
                            owner['project'] = upstream_project
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
                        queue.append(dict(created=self.meta['month'], investor=a['id'],
                                          project=copy.deepcopy(project)))
                    project = None
            if project:
                for input_good, ratio in self.goods[project['good']]['inputs'].items():
                    self._investment_input_reserved[input_good] += project['capacity']*ratio
                evidence = {}
                for input_good, ratio in self.goods[project['good']]['inputs'].items():
                    required = project['capacity']*ratio
                    available = sum(sell['remaining'] for _, sell, _ in self.offers(a, input_good, sells))
                    signal = self.market_signal(self.meta.get('market_history', []), a['state_id'],
                                                project['location']['province_id'], input_good)
                    evidence[input_good] = dict(required=round(required, 6),
                        available=round(available, 6), shortage=round(max(0, required-available), 6),
                        historical_unmet=round(signal['unmet'], 6), historical_volume=round(signal['volume'], 6))
                project['input_evidence'] = evidence
                a['project'] = project
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
        self.__dict__.pop('_investment_supply_cache', None)
        self.__dict__.pop('_upstream_candidate_cache', None)
        self.__dict__.pop('_investment_input_reserved', None)
        self.meta['investment_dirty'] = []
        for a in self.shuffled(investors):
            project = a.get('project')
            if not project:
                continue
            progress = sum(project['materials'].values())
            if progress > project.get('progress_quantity', 0)+EPS:
                project['last_progress'] = self.meta['month']
            project['progress_quantity'] = progress
            project.setdefault('last_progress', project['created'])
            if self.meta['month']-project['last_progress'] >= self.cfg['project_stall_months']:
                del a['project']
                a['protected'] = a.get('operating_protected', 0)+a.get('payroll_protected', 0)
                self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'],
                    outcome='cancelled', reason='Стройка не продвигается'))
                continue
            good = project['good']
            loc = project['location']
            e = dict(id=a['id'], good=good, price=project['expected_price'], **loc)
            margin = (self.net_price(e)-self.unit_cost(e, sells)
                -self.goods[good]['labor']*self.cfg['wage']*self.cfg.get('enterprise_overhead_rate', 0))
            # Отмена требует наблюдаемой отрицательной маржи, отсутствие одного материала не отменяет проект.
            signal = self.market_signal(self.meta.get('market_history', []), a['state_id'], loc['province_id'], good)
            absent = (len(self.meta.get('market_history', [])) >= 3 and self.meta['month']-project['created'] >= 3
                and signal['volume'] <= EPS and signal['unmet'] <= EPS and self.reachable_demand(e, sells) <= EPS)
            if due and (margin < -EPS or absent):
                del a['project']
                a['protected'] = a.get('operating_protected', 0)+a.get('payroll_protected', 0)
                self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='cancelled', reason='Отрицательная маржа или устойчивое отсутствие спроса'))
                continue
            distance, _ = self.path(self.market_tile(a), loc['tile_id'])
            if distance is None:
                continue
            bill = self.construction_bill(project['capacity'])
            delivery = sum(q*self.goods[g]['weight'] for g, q in bill.items())*distance*self.cfg['transport_rate']
            project['delivery'] = delivery
            # Сначала склад инвестора, затем площадка: обе перевозки реальны и входят в оценку.
            a['protected'] = a.get('operating_protected', 0)+a.get('payroll_protected', 0)+project['working_cash']+delivery
            if a['protected']+a.get('reserved', 0) > a['balance']+EPS:
                a['protected'] = max(0, a['balance']-a.get('reserved', 0))
            for g, quantity in bill.items():
                held = project['materials'].get(g, 0)
                assign = min(max(0, quantity-held), max(0, a['stock'].get(g, 0)-held))
                release = max(0, assign-self.free_stock(a['id'], g))
                for offer in sells.get(g, []):
                    if offer['participant'] != a['id'] or offer['status'] in ('cancelled', 'expired'):
                        continue
                    take = min(release, offer['remaining'])
                    if take <= EPS:
                        continue
                    # С рынка снимается только закреплённая часть, свободный остаток остаётся в продаже.
                    offer['quantity'] -= take
                    offer['remaining'] -= take
                    offer['backing'] -= take
                    a['stock_reserved'][g] = max(0, a['stock_reserved'].get(g, 0)-take)
                    release -= take
                    if offer['remaining'] <= EPS:
                        self.cancel_order(offer, 'Материалы закреплены за собственной стройкой')
                assign = min(max(0, quantity-held), self.free_stock(a['id'], g))
                project['materials'][g] = held+assign
                missing = max(0, quantity-project['materials'][g])
                _, price, _ = self.quoted_cost(a, g, missing, sells)
                self.order(a['id'], g, missing, 'buy', price, 'Строительные материалы')

    def finish_projects(self):
        for a in list(self.accounts.values()):
            p = a.get('project')
            if not p:
                continue
            bill = self.construction_bill(p['capacity'])
            need_cash = p['working_cash']+p.get('delivery', 0)
            if any(p['materials'].get(g, 0)+EPS < q for g, q in bill.items()) or self.free_cash(a['id'])+a.get('protected', 0)+EPS < need_cash:
                self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='pending', **copy.deepcopy(p)))
                continue
            a.pop('project')
            a['protected'] = 0
            self.transfer(a['id'], 'carrier:'+a['state_id'], p.get('delivery', 0), 'Доставка строительных материалов на площадку')
            asset = p.get('delivery', 0)
            for g, q in bill.items():
                asset += self.use_stock(a['id'], g, q)
                self.report['construction_used'][g] = self.report['construction_used'].get(g, 0)+q
            if p['expansion']:
                e = self.enterprises[a['id']]
                e['capacity'] += p['capacity']
                e['asset_value'] += asset
                self.report['lifecycle'].append(dict(enterprise=e['id'], action='expanded', good=e['good'], capacity_added=p['capacity'], reason='Оплаченное выгодное расширение'))
            else:
                loc = p['location']
                key = (a['id'], loc['province_id'], loc['tile_id'], p['capacity'])
                self.construction_receipts[key] = dict(working_cash=p['working_cash'], materials=bill, asset_value=asset)
                e = self.open_enterprise(p['good'], loc, a['id'], p['capacity'], sales_estimate=p['capacity'])
                e['price'] = p['expected_price']
            self.report['investment_decisions'].append(dict(kind='investment_decision', investor=a['id'], outcome='expanded' if p['expansion'] else 'built', enterprise=e['id']))

    def produce(self, existing, sells):
        labor, resources = defaultdict(float), defaultdict(float)
        for a in self.accounts.values():
            if a['kind'] == 'household':
                labor[a['state_id'], a['province_id']] += self.population(a)*self.cfg['labor_share']
        for t in self.tiles.values():
            for resource in t['resources']:
                resources[t['province_id'], resource] += self.cfg['extraction_per_tile']
        self.report['labor_available'] = [dict(state_id=s, province_id=p, workers=q) for (s, p), q in labor.items()]
        for e in self.shuffled(existing):
            if e['status'] == 'closed' or e.get('suspended') or e['available_from'] > self.meta['month']:
                continue
            plan = e.get('production_plan', {})
            planned = plan['output'] if plan.get('month') == self.meta['month'] else 0.0
            e['planned_output'] = planned
            recipe, a = self.goods[e['good']], self.accounts[e['id']]
            # Закупочные резервы уже сняты, проектный капитал остаётся защищён.
            project_hold = a.get('project', {}).get('working_cash', 0)+a.get('project', {}).get('delivery', 0)
            a['protected'] = project_hold
            constraints = {'План': planned, 'Мощность': e['capacity'], 'Труд': labor[e['state_id'], e['province_id']]/recipe['labor'],
                'Деньги на зарплату': self.free_cash(e['id'])/(recipe['labor']*self.cfg['wage'])}
            if recipe['resource']:
                constraints['Природный ресурс'] = resources[e['province_id'], recipe['resource']]
            for g, ratio in recipe['inputs'].items():
                constraints['Сырьё: '+g] = self.free_stock(e['id'], g)/ratio
            e['production_observation'] = dict(month=self.meta['month'], constraints=dict(constraints),
                stocks_before=dict(a['stock']), free_cash_before=self.free_cash(e['id']),
                procurement_plan=copy.deepcopy(plan) if plan.get('month') == self.meta['month'] else None,
                expected_margin=e['expected_margin'], expected_unit_cost=e['expected_unit_cost'],
                expected_price=e['expected_price'])
            reason = min(constraints, key=constraints.get)
            q = max(0, constraints[reason])
            e['input_limited'] = any(k.startswith('Сырьё') and v+EPS < planned for k, v in constraints.items())
            if planned > EPS:
                e['production_limit'] = reason
            e['reason'] = e['production_limit']
            if q <= EPS:
                e['status'] = 'idle'
                continue
            workers = q*recipe['labor']
            labor[e['state_id'], e['province_id']] -= workers
            if recipe['resource']:
                resources[e['province_id'], recipe['resource']] -= q
            wages = workers*self.cfg['wage']
            self.distribute(e['id'], self.workforce(e), wages, 'Заработная плата')
            cost = wages
            for g, ratio in recipe['inputs'].items():
                cost += self.use_stock(e['id'], g, q*ratio)
                self.report['inputs_used'][g] = self.report['inputs_used'].get(g, 0)+q*ratio
            # Цена нового выпуска заменяет цену распродажи только у пустого склада.
            if a['stock'].get(e['good'], 0) <= EPS:
                e['price'] = plan.get('sale_price', e['price'])
            self.add_stock(e['id'], e['good'], q, cost)
            e.update(status='active', workers=workers, output=q, production_cost=cost, unit_cost=cost/q, wages=wages)
            self.report['production'][e['good']] = self.report['production'].get(e['good'], 0)+q

    def return_capital(self, e):
        a = self.accounts[e['id']]
        a['protected'] = 0
        self.transfer(e['id'], e['owner_id'], self.free_cash(e['id']), 'Возврат капитала закрытого предприятия')

    def close_enterprise(self, e, unpaid=0):
        for o in self.report['orders']:
            if o['participant'] == e['id'] and o['side'] == 'buy':
                self.cancel_order(o, 'Предприятие закрыто')
        self.accounts[e['id']].pop('project', None)
        e.update(status='closed', closed=self.meta['month'], closure_reason='Двенадцать месяцев подряд убыточной работы', reason='Прекращение нежизнеспособной деятельности')
        self.return_capital(e)
        self.report['lifecycle'].append(dict(enterprise=e['id'], good=e['good'], action='closed', reason=e['reason']))

    def settle(self, enterprises, households):
        for e in enterprises:
            due = self.enterprise_overhead(e) if e['status'] != 'closed' else 0.0
            paid = min(due, self.free_cash(e['id']))
            if paid > EPS:
                self.transfer(e['id'], 'treasury:'+e['state_id'], paid, 'Оброк за землю и мастерскую')
            e.update(fixed_cost_due=due, fixed_cost_paid=paid)
            e['expenses'] = e['cost_of_sales']+due
            e['profit'] = e['revenue']-e['tax']-e['expenses']
            e['loss_months'] = e['loss_months']+1 if e['profit'] < -EPS else 0
            e['profitable_months'] = e['profitable_months']+1 if e['profit'] > EPS else 0
            sold = sum(t['quantity'] for t in self.report['trades'] if t['seller'] == e['id'])
            if e['status'] == 'closed':
                self.return_capital(e)
                continue
            unviable = e['loss_months'] > 0 and e['available_from'] <= self.meta['month']
            e['unviable_months'] = e.get('unviable_months', 0)+1 if unviable else 0
            if e['unviable_months'] >= self.cfg['loss_months_to_close']:
                self.close_enterprise(e)
        for a in self.shuffled(self.accounts.values()):
            rate = {'carrier': self.cfg['carrier_distribution'], 'treasury': self.cfg['public_spending']}.get(a['kind'], 0)
            if rate:
                recipients = [h for h in households if h['state_id'] == a['state_id']]
                self.distribute(a['id'], recipients, min(a['balance']*rate, self.free_cash(a['id'])),
                    'Оплата общественных услуг' if a['kind'] == 'treasury' else 'Оплата транспортных услуг населению')

    def record_market(self):
        rows = defaultdict(lambda: dict(volume=0, trade_gold=0, freight=0, requested=0, unmet_solvent=0, limit_value=0, reserved_unmet=0))
        public = []
        for o in self.report['orders']:
            if o['side'] != 'buy' or (o['status'] == 'cancelled' and not o.get('superseded_by_top_up')):
                continue
            quantity = o['quantity']-o['remaining'] if o.get('superseded_by_top_up') else o['quantity']
            remaining = 0 if o.get('superseded_by_top_up') else o['remaining']
            row = rows[o['state_id'], o['province_id'], o['good']]
            row['requested'] += quantity
            row['limit_value'] += quantity*o['price']
            row['unmet_solvent'] += min(o['remaining'], o['reserved']/o['price'])
            row['reserved_unmet'] += o['reserved']
            if remaining > EPS:
                public.append(copy.deepcopy(o))
            o['unmet_solvent'] = min(o['remaining'], o['reserved']/o['price'])
        for t in self.report['trades']:
            row = rows[t['buyer_state'], t['buyer_province'], t['good']]
            row['volume'] += t['quantity']
            row['trade_gold'] += t['gold']
            row['freight'] += t['freight']
        self.meta['market_history'] = (self.meta.get('market_history', [])+[dict(month=self.meta['month'], markets=[dict(state_id=s, province_id=p, good=g, **r) for (s, p, g), r in sorted(rows.items(), key=lambda x: tuple(v or '' for v in x[0]))])])[-3:]
        self.meta['public_orders'] = public
        # Ценовой сигнал отражает исходный бюджет, а не нулевую кассу после покупок.
        # Допокупка заменяет заявки, но не отменяет ещё неудовлетворённую потребность.
        feedback = {}
        for o in self.report['orders']:
            if o['side'] != 'buy' or not o.get('unfunded_need'):
                continue
            if o['status'] == 'cancelled' and not o.get('superseded_by_top_up'):
                continue
            a = self.accounts[o['participant']]
            needed = a.get('needs', {}).get(o['good'], {}).get('needed', 0)
            if needed <= self.free_stock(a['id'], o['good'])+EPS:
                continue
            feedback.setdefault((o['participant'], o['good']), copy.deepcopy(o))
        self.meta['price_feedback'] = list(feedback.values())

    def step(self):
        self.__dict__.pop('_planning_orders', None)
        self.__dict__.pop('_recovery_quotes', None)
        self.__dict__.pop('_stock_sales_cache', None)
        self.__dict__.pop('_offer_index', None)
        self.activate_rules()
        self.accounts = dict(sorted(self.accounts.items()))
        self.enterprises = dict(sorted(self.enterprises.items()))
        self.meta['month'] += 1
        self.reset_report()
        self.report.update(rules_version=3, investment_decisions=[])
        self.sync_locations()
        before = self.total_stock()
        self.report['opening_balances'] = {a['id']: a['balance'] for a in self.accounts.values()}
        self.report['gold_before'] = self.gold()
        prior_cash = {a['id']: a['balance'] for a in self.accounts.values() if a['kind'] == 'household'}
        for a in self.accounts.values():
            hold = a.get('project', {}).get('working_cash', 0)+a.get('project', {}).get('delivery', 0)
            a.update(reserved=0.0, protected=min(a['balance'], hold), payroll_protected=0.0,
                operating_protected=0.0, stock_reserved={})
        existing = list(self.enterprises.values())
        for e in existing:
            e.pop('production_observation', None)
            e.update(revenue=0, tax=0, expenses=0, profit=0, output=0, workers=0, wages=0, cost_of_sales=0,
                production_cost=0, input_limited=False, fixed_cost_due=0, fixed_cost_paid=0,
                dividends=0.0, working_reserve=0.0, project_reserve=0.0)
        self.update_prices()
        self.prepare_recovery_quotes()
        sells = self.sell_orders()
        households = self.shuffled(a for a in self.accounts.values() if a['kind'] == 'household')
        food_trace = FoodDiagnostics(self, households, sells) if self.FOOD_DIAGNOSTICS else None
        self.household_requests(households, self.cfg['basic_needs'], 'Базовые нужды', sells)
        self.household_requests(households, self.cfg['extra_needs'], 'Дополнительные нужды', sells)
        self.owner_requests(sells)
        self.recover_enterprises(sells)
        # Строительные заявки реальны и обеспечены до расчёта потребностей в сырье.
        self.project_requests(sells)
        # Один проход от конечных товаров ко входам, без рекурсивного перепланирования.
        for e in sorted(self.shuffled(existing), key=lambda e: -self.depth(e['good'])):
            a = self.accounts[e['id']]
            if e['status'] == 'closed' or (self.free_cash(e['id']) <= EPS
                    and a.get('operating_protected', 0) <= EPS):
                # Спрос на входы появляется после планирования следующего звена цепочки.
                self.recover_enterprises(sells, [e])
            if e['status'] != 'closed' and not e.get('suspended') and e['available_from'] <= self.meta['month']:
                self.input_requests(e, sells)
        buys = [o for o in self.report['orders'] if o['side'] == 'buy']
        for buy in self.shuffled(buys):
            self.match(buy, sells)
        if self.HOUSEHOLD_TOP_UP_REVISION:
            self.household_top_up(households, sells)
        self.record_market()
        if food_trace:
            food_trace.clearing(self, sells)
        self._planning_orders = copy.deepcopy(self.report['orders'])
        planning_sells = copy.deepcopy(sells)
        # Снимок спроса сделан до поздних доходов; остатки заявок больше не блокируют кассу.
        for o in self.report['orders']:
            if o['side'] == 'buy':
                o['reserved_at_clearing'] = o['reserved']
            execution_status, execution_reason = o['status'], o['reason']
            self.cancel_order(o, 'Окончание торгов месяца')
            if o['status'] == 'cancelled':
                o['status'] = 'expired'
                o['execution_status'] = execution_status
                o['reason'] = execution_reason or 'Окончание торгов месяца'
        self.produce(existing, planning_sells)
        self.finish_projects()
        self.consume_owner_purchases()
        for a in households:
            for good, need in a.get('needs', {}).items():
                consumed = min(need['needed'], self.free_stock(a['id'], good))
                self.use_stock(a['id'], good, consumed)
                need.update(consumed=consumed, deficit=need['needed']-consumed)
                self.report['consumed'][good] = self.report['consumed'].get(good, 0)+consumed
        for e in self.enterprises.values():
            offers = [o for o in self.report['orders'] if o['side'] == 'sell' and o['participant'] == e['id'] and o['good'] == e['good']]
            sold = sum(o['quantity']-o['remaining'] for o in offers)
            e['sales_history'] = (e.get('sales_history', [])+[dict(month=self.meta['month'], sold=sold,
                offered=sum(o['quantity'] for o in offers), output=e['output'], input_limited=e.get('input_limited', False),
                additional_demand=e.get('reachable_demand', 0) > EPS)])[-3:]
        for a in self.accounts.values():
            for good in a['stock']:
                history = a.setdefault('stock_sales_history', {}).get(good, [])
                sold = sum(t['quantity'] for t in self.report['trades'] if t['seller'] == a['id'] and t['good'] == good)
                a['stock_sales_history'][good] = (history+[dict(sold=sold)])[-3:]
            hold = a.get('project', {}).get('working_cash', 0)+a.get('project', {}).get('delivery', 0)
            a['protected'] = min(a['balance'], hold)
        self.settle(list(self.enterprises.values()), households)
        self.pay_dividends(planning_sells)
        for a in self.accounts.values():
            a['project_reserve'] = self.project_cash_reserve(a['id'], planning_sells)
        self.report['diagnostics'].extend(self.report['investment_decisions'])
        self.report['diagnostics'].extend(dict(kind='owner_consumption', **copy.deepcopy(row))
            for row in self.report['owner_consumption'])
        self.summarize(prior_cash)
        self.report['account_flows'] = []
        sectors = defaultdict(float)
        incoming_by_account, outgoing_by_account = defaultdict(float), defaultdict(float)
        for f in self.report['flows']:
            incoming_by_account[f['recipient']] += f['gold']
            outgoing_by_account[f['sender']] += f['gold']
        for a in self.accounts.values():
            incoming = incoming_by_account[a['id']]
            outgoing = outgoing_by_account[a['id']]
            self.report['account_flows'].append(dict(id=a['id'], kind=a['kind'], incoming=incoming, outgoing=outgoing, net=incoming-outgoing))
        for f in self.report['flows']:
            sectors[self.accounts[f['sender']]['kind'], self.accounts[f['recipient']]['kind']] += f['gold']
        self.report['sector_flows'] = [dict(sender_kind=s, recipient_kind=r, gold=q) for (s, r), q in sorted(sectors.items())]
        self.report['gold_after'] = self.gold()
        self.report['balances'] = {kind: sum(a['balance'] for a in self.accounts.values() if a['kind'] == kind) for kind in sorted({a['kind'] for a in self.accounts.values()})}
        total_gold = self.gold()
        self.report['money_shares'] = {kind: balance/total_gold if total_gold else 0
            for kind, balance in self.report['balances'].items()}
        self.report['diagnostics'].extend(dict(kind='money_share', sector=kind, balance=balance,
            share=self.report['money_shares'][kind]) for kind, balance in self.report['balances'].items())
        self.report['stocks'] = self.total_stock()
        self.report['active_enterprises'] = sum(e['status'] == 'active' for e in self.enterprises.values())
        self.report['open_enterprises'] = sum(e['status'] != 'closed' for e in self.enterprises.values())
        self.meta['rng_state'] = self.rng.getstate()
        self.validate(before)
        if food_trace:
            self.report['food_diagnostics'] = food_trace.finish(self)
        self.build_causal_events()
        return self.report

    def validate(self, before=None):
        super().validate(before)
        for a in self.accounts.values():
            if any(not math.isfinite(a.get(k, 0)) or a.get(k, 0) < -EPS for k in ('reserved', 'protected')) or a.get('reserved', 0)+a.get('protected', 0) > a['balance']+EPS:
                raise ValueError('Нарушен резерв средств')
            for g in a.get('stock_reserved', {}).keys() | a.get('project', {}).get('materials', {}).keys():
                q = a.get('stock_reserved', {}).get(g, 0)
                if q < -EPS or q+self.locked_material(a['id'], g) > a['stock'].get(g, 0)+EPS:
                    raise ValueError('Нарушен резерв товаров')


def initialize(service):
    if not service.db.campaign():
        return False
    with service.db.transaction():
        if service.db.economy_records('EconomyMeta'):
            return False
        engine = Economy.load(service.db)
        engine.bootstrap()
        engine.save(service.db)
        event = service.db.record_event(service.event('economy_initialized',
            dict(gold=engine.gold(), enterprises=len(engine.enterprises), rules_version=3), [], source='economy', summary='Начальное состояние экономики V3'))
    service.notify(event)
    return True


def monthly_phase(service, ctx):
    engine = Economy.load(service.db)
    if not engine.accounts:
        engine.bootstrap()
    report = engine.step()
    engine.save(service.db)
    ctx['changes'].append(f'Экономика V3: {report["active_enterprises"]} предприятий, {len(report["trades"])} сделок')
    event = service.db.record_event(service.event('economy_month',
        dict(month=report['month'], invariants=report['invariants'], trades=len(report['trades']), rules_version=3), [],
        source='economy', action_id=ctx['command_id'], summary=ctx['changes'][-1]))
    ctx['events'].append(event)
    return len(engine.enterprises)
