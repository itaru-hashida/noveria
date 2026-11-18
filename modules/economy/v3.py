"""Экономика V3: обеспеченные заявки, реальные расходы и один выпуск за месяц."""
import copy
import math
import time
from collections import defaultdict

from .support import EPS, EconomyServices
from .native import cancel_boundary as _cancel
from .native import cash_boundary as _cash
from .native import consumption_boundary as _consumption
from .native import demand_boundary as _demand
from .native import construction_buy_boundary as _construction_buy
from .native import delivery_boundary as _delivery
from .native import diagnostics_boundary as _diagnostics
from .native import event_buffer_boundary as _event_buffer
from .native import event_writer as _event_writer
from .native import enterprise_settlement_boundary as _enterprise_settlement
from .native import enterprise_state_boundary as _enterprise_state
from .native import dividend_boundary as _dividend
from .native import match_boundary as _match
from .native import market_book_boundary as _market_book_native
from .native import material_finalize_boundary as _material_finalize
from .native import money_boundary as _money
from .native import net_price_boundary as _net_price
from .native import offer_boundary as _offer
from .native import order_boundary as _order
from .native import owner_consumption_boundary as _owner_consumption
from .native import plan_boundary as _plan
from .native import production_boundary as _production
from .native import production_outlook_boundary as _production_outlook
from .native import quote_boundary as _quote
from .native import report_history_boundary as _report_history
from .native import project_completion_boundary as _project_completion
from .native import settlement_boundary as _settlement
from .native import stock_boundary as _stock
from .native import stock_store_boundary as _stock_store
from .native import topup_boundary as _topup
from .native import trade_boundary as _trade
from .native import wage_boundary as _wage
from .native import world_boundary as _world
from .food_diagnostics import FoodDiagnostics

_offer.configure(EPS)
_money.configure(EPS)


OWNER_KINDS = ('private', 'merchant', 'treasury', 'municipal')
OWNER_BASKET = ('bread', 'fish', 'cloth')
OWNER_CONSUMPTION = 'Личное потребление собственника'
CONSUMPTION_PURPOSES = ('Базовые нужды', 'Дополнительные нужды', OWNER_CONSUMPTION)


def project_finances(capacity, available, startup, material, delivery,
                     unit, fixed_unit, equipment_capital, margin, expansion):
    """Чистый расчёт финансирования кандидата после рыночных котировок.

    Числовое тело (мощность по кассе, оборотный капитал, вложения и прибыль)
    живёт в ``money_boundary``: там же Python-эталон и fallback без собранного
    .pyd. Имя остаётся публичным: ``shadow.investment_batch`` зовёт его напрямую.
    """
    return _money.project_finances(capacity, available, startup, material,
                                   delivery, unit, fixed_unit, equipment_capital,
                                   margin, expansion, EPS)


# Совместимость: имя остаётся доступным в модуле, но реализация одна — та,
# что привязана в native-границе (Cython, иначе Python-эталон).
OfferView = _offer.OfferView


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
    QUOTE_CACHE = True

    @property
    def event_recorder(self):
        """Синхронный буфер событий месяца: записи, фильтры, батчи.

        Живёт на движке, а не в отчёте: это поток вывода, а не состояние.
        Хранит лишь числа и ссылки на уже созданные строки ``report['events']``,
        поэтому второго ledger'а не появляется. Фоновая часть — отдельный
        ``event_writer``; игровой цикл его не ждёт.
        """
        recorder = self.__dict__.get('_event_recorder')
        if recorder is None:
            recorder = self.__dict__['_event_recorder'] = \
                _event_buffer.EventRecorder(self.report_detail(),
                                            batch_size=256, capacity=4096,
                                            drop_diagnostic=False)
        return recorder

    @property
    def event_writer(self):
        """Фоновый writer вывода или None, если приложение его не подключило.

        Подключается снаружи (``engine.event_writer = writer``) и только он
        касается диска. Без writer'а буфер всё равно ведёт учёт: события
        отчёта не теряются, потому что канонический источник — сам отчёт.
        """
        return self.__dict__.get('_event_writer')

    @event_writer.setter
    def event_writer(self, writer):
        self.__dict__['_event_writer'] = writer

    def report_detail(self):
        # Игра работает в none: deep добавляет снимок каждой книги и причинный
        # слой, которые нужны только аудиту и лаборатории. Диагностика не должна
        # быть платой за каждый ход; тест test_detailed_report_is_a_superset...
        # подтверждает, что счета, предприятия и сделки в none те же.
        detail = self.meta.get('report_detail', 'none')
        if detail not in ('none', 'standard', 'deep'):
            raise ValueError('Неизвестная детализация отчёта: '+str(detail))
        return detail

    def reset_report(self):
        # Рыночные кэши живут строго один отчётный месяц. Это также нужно
        # лабораторным сценариям, которые вручную начинают следующий месяц.
        self.clear_market_books()
        self.invalidate_landed_cache()
        self.invalidate_production_outlook_cache()
        self.invalidate_stock_sales_cache()
        for key in ('_customer_order_index', '_physical_stock_totals',
                    '_match_observation_count', '_customer_quotes_cache',
                    '_customer_demand_rows_cache', '_quoted_cost_cache',
                    '_reference_price_cache', '_unit_cost_cache',
                    '_market_signal_cache', '_route_order_components'):
            self.__dict__.pop(key, None)
        self._market_quote_version = 0
        self._demand_revision = defaultdict(int)
        # Месячные счётчики: версии книг по товару и число сделок по продавцу.
        self._market_quote_versions = defaultdict(int)
        self._market_quotes_epoch = 0
        self._trades_by_seller = defaultdict(int)
        super().reset_report()
        # Граница заявок держит живой список report['orders']: у нового отчёта
        # новый список, поэтому ссылку надо взять заново, иначе мутации уходили
        # бы в заявки прошлого месяца.
        self._order_store = _order.OrderStore(self.report['orders'])
        if self.report_detail() == 'none':
            self.report.pop('provinces', None)
            self.report.pop('prices', None)
        self.report.update(rules_version=3, investment_decisions=[], dividends=[],
            owner_consumption=[], owner_consumed={}, events=[],
            investment_candidates=[], expansion_intents=[],
            expansion_intent_diagnostics=[], investment_candidate_statuses=[],
            investment_shadow_gate=[], investment_candidate_capacity_limits=[], migration=[],
            public_projects=[], construction_labor=[], market_boards=[])

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
        # Синхронная часть пайплайна: буфер получает ту же строку отчёта и
        # ставит её в порядок вывода. Ядро Cython (event_buffer_kernel) лишь
        # считает, фильтрует и батчит — строки, JSON и диск остаются фону
        # (event_writer). Поэтому порядок, количество и содержимое
        # report['events'] не меняются, а replay-событие нельзя потерять.
        self.event_recorder.record(row, kind=_event_buffer.REPLAY,
                                   channel=_event_buffer.EVENTS)
        return event_id

    def build_causal_events(self):
        """Собрать JSONL-подобный слой из уже рассчитанного месячного отчёта."""
        events = self.report['events']
        decisions = self.report.get('investment_decisions', [])
        gap_events = {}
        demography_cache = self.meta.get('demography', {}).get('provinces', {})
        prior_population_events = {pid: row.get('last_population_event_id')
            for pid, row in demography_cache.items()}
        for row in self.report.get('demography', []):
            moved = row.get('migration_in', 0) or row.get('migration_out', 0)
            cause = ('food_stress_and_migration' if row.get('hunger_deaths', 0) > EPS and moved
                     else 'food_stress' if row.get('hunger_deaths', 0) > EPS
                     else 'migration' if moved else 'natural_change')
            event_id = self.log_event('demography.population_change', region_id=row['province_id'],
                cause=cause, source='demography.update',
                parent_event_id=prior_population_events.get(row['province_id']),
                population_before=row['population_before'], population_after=row['population_after'],
                population_delta=row['population_after']-row['population_before'],
                births=row['births'], baseline_deaths=row['baseline_deaths'],
                hunger_deaths=row['hunger_deaths'], food_stress=row['food_stress'],
                migration_in=row.get('migration_in', 0), migration_out=row.get('migration_out', 0),
                market_food_shortfall=row['market_food_shortfall'],
                protected_by_subsistence=row['protected_by_subsistence'])
            demography_cache.setdefault(row['province_id'], {})['last_population_event_id'] = event_id
        for migration in self.report.get('migration', []):
            self.log_event('demography.migration', entity_id=(migration['source_province']+':'
                +migration['destination_province']), region_id=migration['source_province'],
                cause=migration['cause'], source='migration.apply',
                parent_event_id=prior_population_events.get(migration['source_province']),
                destination_region_id=migration['destination_province'], state_id=migration['state_id'],
                people=migration['people'], source_stress=migration['source_stress'],
                destination_stress=migration['destination_stress'], distance=migration['distance'],
                gold=migration['gold'], stock=migration['stock'])
        for decision in decisions:
            project_id = decision.get('project_id') or decision.get('investor')
            self.log_event('investment.' + decision.get('outcome', 'decision'),
                entity_id=decision.get('investor'), region_id=decision.get('province_id'),
                good=decision.get('good'), project_id=project_id,
                decision_id=project_id, cause=decision.get('reason'),
                source=decision.get('dependency_for') or 'project_candidate',
                status=decision.get('outcome'), capacity=decision.get('capacity'),
                requested_capacity=decision.get('requested_capacity'),
                capacity_limit=decision.get('capacity_limit'),
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

        # Числовые counters группировки сделок (count, quantity, gold, freight)
        # считает diagnostics_boundary: она же держит Python-эталон и fallback
        # без собранного .pyd. Порядок групп — прежний (первое появление ключа),
        # поэтому порядок и количество trade.executed событий не меняются;
        # сами события по-прежнему создаёт log_event.
        for (buyer, seller, good, buyer_region, seller_region, purpose), count, quantity, gold, freight in _diagnostics.aggregate_trade_groups(self.report.get('trades', [])):
            self.log_event('trade.executed', entity_id=f'{buyer}:{seller}:{good}',
                region_id=buyer_region, good=good, source='match',
                quantity=quantity, trades_count=count, seller=seller,
                buyer=buyer, seller_region=seller_region,
                price=gold/quantity if quantity else None,
                freight=freight,
                landed_price=(gold+freight)/quantity
                    if quantity else None,
                gold=gold, purpose=purpose)

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
            recipe = self.goods[enterprise['good']]
            if actual + EPS < planned or limiting not in (None, 'План'):
                account = self.accounts[enterprise['id']]
                input_details = {}
                for input_good, ratio in recipe.get('inputs', {}).items():
                    input_details[input_good] = dict(
                        required=planned*ratio,
                        available=account.get('stock', {}).get(input_good, 0),
                        reserved=account.get('stock_reserved', {}).get(input_good, 0),
                        consumed=actual*ratio)
                equipment_details = {good: dict(required=planned*ratio,
                    available=account.get('stock', {}).get(good, 0),
                    reserved=account.get('stock_reserved', {}).get(good, 0),
                    worn=self.equipment_wear(recipe, actual).get(good, 0))
                    for good, ratio in recipe.get('equipment', {}).items()}
                self.log_event('production.limited', entity_id=enterprise['id'],
                    region_id=enterprise.get('province_id'), good=enterprise.get('good'),
                    cause=limiting or 'unexplained_gap', source='production.constraints',
                    capacity=enterprise.get('capacity'), planned_output=planned,
                    actual_output=actual, constraints=constraints,
                    expected_margin=enterprise.get('expected_margin'),
                    wages=enterprise.get('wages'), overhead=self.enterprise_overhead(enterprise),
                    input_limited=enterprise.get('input_limited', False),
                    input_orders=plan.get('input_orders', []), input_details=input_details,
                    equipment_details=equipment_details)
            for equipment_good, worn in enterprise.get('equipment_wear', {}).items():
                if worn > EPS:
                    self.log_event('equipment.wear', entity_id=enterprise['id'],
                        region_id=enterprise.get('province_id'), good=equipment_good,
                        cause='production', source='production.consume',
                        output_good=enterprise.get('good'), output=actual, worn=worn,
                        deployed=self.equipment_requirements(recipe, actual).get(equipment_good, 0))

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
                demography = self.report.get('demography_by_province', {}).get(pid, {})
                self.log_event('market.snapshot', region_id=pid, good=market_good,
                    parent_event_id=prior_population_events.get(pid),
                    source='market.clearance', demand=demand, executed=executed,
                    unmet=max(0, demand-executed), offered=offered,
                    sales=sum(t.get('quantity', 0) for t in trades), stock=stock,
                    population_demand_basis=demography.get('population_before', province.get('population')),
                    population_after_month=demography.get('population_after', province.get('population')),
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
        """Пометить товар и его потребителей для точечного обзора инвестиций."""
        dirty = getattr(self, '_investment_dirty_goods', None)
        if dirty is None:
            dirty = set(self.meta.get('investment_dirty', []))
            self._investment_dirty_goods = dirty
        dirty.update(self.goods if good is None else self.recipe_graph()['affected'][good])

    def persist_investment_state(self):
        """Сериализовать dirty один раз в конце месяца, а не при каждом товаре."""
        dirty = sorted(getattr(self, '_investment_dirty_goods',
                              set(self.meta.get('investment_dirty', []))))
        self.meta['investment_dirty'] = dirty
        self.meta['investment_state'] = dict(month=self.meta['month'],
            dirty_goods=dirty, full_reconcile_month=self.meta.get('investment_full_reconcile_month'))

    def recipe_graph(self):
        graph = getattr(self, '_recipe_graph_cache', None)
        if graph is None:
            parents = {good: set() for good in self.goods}
            children = {good: dict(recipe.get('inputs', {})) for good, recipe in self.goods.items()}
            for product, inputs in children.items():
                for input_good in inputs:
                    parents.setdefault(input_good, set()).add(product)
            affected = {}
            for source in self.goods:
                seen, pending = set(), [source]
                while pending:
                    current = pending.pop()
                    if current in seen:
                        continue
                    seen.add(current)
                    pending.extend(parents.get(current, ()))
                affected[source] = frozenset(seen)
            graph = {'parents': parents, 'children': children, 'affected': affected}
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

    def build_investment_index(self):
        """Неизменяемый срез рынка для одного прохода инвестиций.

        Резервы проектов сюда не входят: они остаются живыми и обновляются в
        project_requests(), чтобы второй кандидат не увидел мощность первого.
        """
        history = self.meta.get('market_history', [])
        markets = defaultdict(set)
        for order in self.report['orders']:
            if order['side'] == 'buy' and order.get('province_id'):
                markets[order['state_id']].add((order['province_id'], order['good']))
        for month in history:
            for row in month['markets']:
                markets[row['state_id']].add((row['province_id'], row['good']))
        resource_goods = {state_id: {good for _, good in rows if self.goods[good]['resource']}
                          for state_id, rows in markets.items()}
        resource_tiles = defaultdict(list)
        for tile in self.tiles.values():
            state_id = self.owners.get(tile['province_id'])
            if state_id is None:
                continue
            for resource in tile['resources']:
                resource_tiles[state_id, tile['province_id'], resource].append(tile)
            for good in resource_goods.get(state_id, ()):
                if self.goods[good]['resource'] in tile['resources']:
                    markets[state_id].add((tile['province_id'], good))
        labor_values = defaultdict(list)
        for account in self.accounts.values():
            if account['kind'] == 'household':
                labor_values[account['state_id'], account['province_id']].append(
                    self.population(account)*self.cfg['labor_share'])
        labor = {key: sum(values) for key, values in labor_values.items()}
        resource_values = defaultdict(list)
        for firm in self.enterprises.values():
            if firm['status'] != 'closed' and self.goods[firm['good']]['resource']:
                resource_values[firm['province_id'], self.goods[firm['good']]['resource']].append(firm['capacity'])
        resource_used = {key: sum(values) for key, values in resource_values.items()}
        projects = defaultdict(list)
        buyer_orders = defaultdict(list)
        for order in self.report['orders']:
            if order['side'] == 'buy':
                buyer_orders[order['participant'], order['good']].append(order['id'])
        for account in self.accounts.values():
            project = account.get('project')
            if project:
                projects[project['good']].append((dict(project['location']),
                    project['expected_price'], project['capacity']))
        return dict(markets={state_id: tuple(sorted(rows)) for state_id, rows in markets.items()},
            resource_tiles=resource_tiles, labor=labor, resource_used=resource_used,
            projects=projects, buyer_orders={key: tuple(value) for key, value in buyer_orders.items()},
            project_revision=0)

    def build_investment_state(self):
        """Единый изменяемый ledger одного инвестиционного обзора.

        Снимок рынка неизменяем, а резервы, проекты и рассчитанные потоки
        принадлежат этому объекту. Поэтому следующий инвестор видит решение
        предыдущего без нового прохода по миру.
        """
        return dict(index=self.build_investment_index(), supply_cache={}, demand_cache={},
            flow_balances={}, flow_reserved={}, input_reserved=defaultdict(float),
            labor_reserved=defaultdict(float), resource_reserved=defaultdict(float),
            supply_revision=defaultdict(int), dirty_goods=set(), upstream_candidates={})

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
        state['dirty_goods'].update(self.recipe_graph()['affected'][project['good']])

    def register_investment_project(self, project):
        state = getattr(self, '_investment_state', None)
        index = state['index'] if state is not None else getattr(self, '_investment_index', None)
        if index is not None:
            index['projects'][project['good']].append((dict(project['location']),
                project['expected_price'], project['capacity']))
            index['project_revision'] += 1
        self.reserve_investment_project(project)

    def investment_flow_balance(self, state_id, good):
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
        """Построить все устойчивые потоки одним проходом по миру.

        Полный инвестиционный обзор требует каждую пару ``государство ×
        товар``. Вызывать для неё ``investment_flow_balance`` отдельно значит
        повторно проходить все предприятия и проекты. Здесь сохраняется тот же
        диагностический состав строк, но предприятия и проекты читаются по
        одному разу.
        """
        state_ids = tuple(state_ids)
        # Числовые накопления строк (supply, capacity_supply, operating_demand,
        # construction_demand, committed, free, shortage, rampable_free) и
        # диагностические списки считает money_boundary: там же Python-эталон и
        # fallback без собранного .pyd. Плоские строки предприятий и проектов
        # готовит граница в прежнем порядке обхода, поэтому состав и порядок
        # записей не меняются, а второго ledger'а нет.
        balances = _money.flows(self, state_ids)
        state = getattr(self, '_investment_state', None)
        if state is not None:
            state['flow_balances'].update(balances)
        return balances

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

    def sustainable_candidate_capacity(self, candidate, reserved=None, flow_balances=None):
        """Вернуть обеспеченную мощность кандидата и ledger после её резерва.

        В отличие от склада это резерв постоянной производственной цепочки.
        Вызывающий применяет возвращённый ledger только для действительно
        начатого проекта.
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

        ledger = dict(reserved or {})
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
        return best, best_ledger

    def limit_project_capacity(self, project, capacity):
        """Уменьшить ещё не начатый проект без изменения его экономики на единицу."""
        # Числовой срез (requested_capacity, capacity, working_cash,
        # expected_profit, expected_investment) считает project_completion_
        # boundary: она же держит Python-эталон и fallback без собранного
        # .pyd. chain_requirements считает прежний хук движка, текстовая
        # причина остаётся в границе.
        return _project_completion.limit_project_capacity(self, project, capacity)

    def investment_candidate_capacity_limits(self, candidates, flow_balances=None):
        """Теневой общий лимит кандидатов по существующей цепочке выпуска."""
        ledger = {}
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
        # Индекс физических запасов обновляет stock-ядро: оно видит before/after
        # без повторного чтения словаря на каждой операции.
        return _stock.add_stock(self, account, good, quantity, value)

    @property
    def stock_store(self):
        accounts = self.accounts
        store = self.__dict__.get('_stock_store')
        if store is None or store.accounts is not accounts:
            store = self._stock_store = _stock_store.StockStore(accounts)
        return store

    def _update_physical_stock_total(self, good, before, after):
        """Обновить ленивый индекс физических запасов, если он уже построен."""
        totals = getattr(self, '_physical_stock_totals', None)
        if totals is None or good not in totals:
            return
        row = totals[good]
        row['quantity'] += after-before
        row['count'] += int(after > EPS)-int(before > EPS)

    def physical_stock_summary(self, buyer_id, good):
        """Запасы товара у остальных счетов: один полный обход на товар за месяц."""
        totals = getattr(self, '_physical_stock_totals', {})
        row = totals.get(good)
        if row is None:
            row = dict(quantity=0.0, count=0)
            for account in self.accounts.values():
                quantity = account['stock'].get(good, 0)
                row['quantity'] += quantity
                row['count'] += int(quantity > EPS)
            totals[good] = row
            self._physical_stock_totals = totals
        own = self.accounts[buyer_id]['stock'].get(good, 0)
        return dict(quantity=max(0, row['quantity']-own),
            count=max(0, row['count']-int(own > EPS)))

    def open_enterprise(self, *args, **kwargs):
        e = super().open_enterprise(*args, **kwargs)
        e.update(cost_of_sales=0, tax=0, wages=0, unviable_months=0)
        # ``initial`` допустим и позиционно: стартовый мир не должен сам
        # создавать адресную проверку инвестиций при bootstrap.
        initial = kwargs.get('initial', args[4] if len(args) > 4 else False)
        if not initial:
            self.mark_investment_dirty(e['good'])
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
        self.cfg.setdefault('demography', {'enabled': False})
        self.cfg.setdefault('migration', {'enabled': False})
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
        demography = self.cfg['demography']
        if not isinstance(demography, dict):
            raise ValueError('Некорректные параметры демографии')
        demography.setdefault('enabled', False)
        if demography['enabled']:
            defaults = dict(birth_rate=0.003, base_death_rate=0.0028,
                            stress_decay=0.85, hunger_death_rate=0.006,
                            subsistence_share=0.25)
            for key, value in defaults.items():
                demography.setdefault(key, value)
            for key in ('birth_rate', 'base_death_rate', 'hunger_death_rate'):
                if not math.isfinite(demography[key]) or demography[key] < 0:
                    raise ValueError('Некорректный параметр демографии: '+key)
            for key in ('stress_decay', 'subsistence_share'):
                if not math.isfinite(demography[key]) or not 0 <= demography[key] <= 1:
                    raise ValueError('Некорректный параметр демографии: '+key)
        migration = self.cfg['migration']
        if not isinstance(migration, dict):
            raise ValueError('Некорректные параметры миграции')
        migration.setdefault('enabled', False)
        if migration['enabled']:
            defaults = dict(stress_threshold=.10, minimum_advantage=.05,
                            pressure_rate=.004, max_monthly_rate=.002,
                            destination_max_rate=.002)
            for key, value in defaults.items():
                migration.setdefault(key, value)
            for key, value in migration.items():
                if key != 'enabled' and (not math.isfinite(value) or value < 0):
                    raise ValueError('Некорректный параметр миграции: '+key)
            if migration['stress_threshold'] >= 1:
                raise ValueError('Порог миграции должен быть меньше единицы')
        for product, recipe in self.goods.items():
            equipment = recipe.setdefault('equipment', {})
            if not isinstance(equipment, dict):
                raise ValueError('Некорректное оборудование рецепта: '+product)
            for good, ratio in equipment.items():
                durable = self.goods.get(good, {}).get('durable')
                if (not durable or not math.isfinite(ratio) or ratio <= 0
                        or not math.isfinite(durable.get('wear_rate', -1))
                        or not 0 <= durable['wear_rate'] <= 1):
                    raise ValueError('Некорректное оборудование рецепта: '+product+':'+good)
            if recipe['resource']:
                share = recipe.setdefault('seed_share', 1)
                owner = recipe.setdefault('extraction_owner', 'private')
                recipe.setdefault('seed_initial', True)
                if not math.isfinite(share) or not 0 < share <= 1:
                    raise ValueError('Некорректная начальная доля добычи: '+product)
                if owner not in ('private', 'treasury'):
                    raise ValueError('Некорректный начальный владелец добычи: '+product)
                if not isinstance(recipe['seed_initial'], bool):
                    raise ValueError('Некорректный признак начальной добычи: '+product)
        institutions = self.cfg.setdefault('institutions', {})
        if not isinstance(institutions, dict):
            raise ValueError('Некорректные учреждения')
        for kind, blueprint in institutions.items():
            if not isinstance(kind, str) or not kind or not isinstance(blueprint, dict):
                raise ValueError('Некорректный тип учреждения')
            if not isinstance(blueprint.get('name'), str) or not blueprint['name'].strip():
                raise ValueError('У учреждения нет названия: '+kind)
            for field in ('materials', 'equipment', 'equipment_wear'):
                values = blueprint.setdefault(field, {})
                if not isinstance(values, dict):
                    raise ValueError('Некорректная смета учреждения: '+kind)
                for good, quantity in values.items():
                    if good not in self.goods or not math.isfinite(quantity) or quantity < 0:
                        raise ValueError('Некорректная смета учреждения: '+kind+':'+str(good))
            for good, quantity in blueprint['materials'].items():
                if quantity <= 0:
                    raise ValueError('Некорректный материал учреждения: '+kind+':'+good)
            for good, quantity in blueprint['equipment'].items():
                if quantity <= 0 or not self.goods[good].get('durable'):
                    raise ValueError('Некорректное оснащение учреждения: '+kind+':'+good)
            for good, quantity in blueprint['equipment_wear'].items():
                if good not in blueprint['equipment'] or quantity < 0:
                    raise ValueError('Некорректный износ учреждения: '+kind+':'+good)
            for field in ('work_required', 'max_workers'):
                value = blueprint.get(field)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError('Некорректный труд учреждения: '+kind)
            if blueprint['max_workers'] > blueprint['work_required']:
                raise ValueError('Бригада больше всего объёма работ: '+kind)
        modes = self.cfg.setdefault('construction_modes', {
            'normal': dict(wage_multiplier=1, lodging_allowance=0, support_goods={})})
        if not isinstance(modes, dict) or 'normal' not in modes:
            raise ValueError('Некорректные режимы строительства')
        for mode, values in modes.items():
            if not isinstance(values, dict):
                raise ValueError('Некорректный режим строительства: '+str(mode))
            wage_multiplier = values.get('wage_multiplier')
            lodging = values.get('lodging_allowance', 0)
            support = values.setdefault('support_goods', {})
            if (not math.isfinite(wage_multiplier) or wage_multiplier < 1
                    or not math.isfinite(lodging) or lodging < 0 or not isinstance(support, dict)):
                raise ValueError('Некорректный режим строительства: '+str(mode))
            for good, quantity in support.items():
                if good not in self.goods or not math.isfinite(quantity) or quantity < 0:
                    raise ValueError('Некорректное содержание строителей: '+str(mode)+':'+str(good))
        # Инициализация резерва тоже идёт через CashStore: ``step`` вызывает
        # ``activate_rules`` уже в native-режиме, и прямая запись в dict стала
        # бы вторым писателем поля. Условие прежнее — только отсутствующий ключ,
        # поэтому существующее значение не перезаписывается.
        cash = self.cash_store
        for a in self.accounts.values():
            if 'reserved' not in a:
                cash.set_reserved_cash(a['id'], 0.0)
            if 'protected' not in a:
                cash.set_protected_cash(a['id'], 0.0)
            self.stock_store.ensure_reserved(a['id'])
        for e in self.enterprises.values():
            e.setdefault('available_from', e['opened']+1 if e['opened'] else 0)
            e.setdefault('sales_history', [])
            e.setdefault('sales_forecast', e['capacity']*self.rules['initial_sales_fraction'])
            e.setdefault('asset_value', sum(q*self.goods[g]['price'] for g, q in self.construction_bill(e['capacity']).items()))
            e.setdefault('unviable_months', 0)

    def free_cash(self, account):
        """Свободная касса счёта — из единственного владельца полей.

        Формула живёт в ``CashStore.free_cash``: ``max(0.0, balance-reserved
        -protected)``. Здесь остаётся только адаптер, поэтому второй копии
        расчёта нет ни в Python, ни в native-границах.
        """
        return self.cash_store.free_cash(account)

    def enterprise_overhead(self, e):
        """Ежемесячный оброк за площадку и мастерскую при открытом предприятии."""
        # Произведение capacity*labor*wage*rate считает money_boundary: она же
        # держит Python-эталон и fallback без собранного .pyd. Порядок
        # сомножителей прежний, поэтому последний бит не меняется.
        return _money.enterprise_overhead(
            e['capacity'], self.goods[e['good']]['labor'], self.cfg['wage'],
            self.cfg.get('enterprise_overhead_rate', 0))

    def update_demography(self):
        """Агрегированная демография после фактического потребления месяца.

        Численность остаётся в SocialGroup. В meta хранится только сглаженный
        продовольственный стресс провинции и дробный остаток округления.
        Самопрокорм не создаёт товар: он защищает часть населения от рыночного
        дефицита при расчёте смертности.
        """
        cfg = self.cfg['demography']
        if not cfg.get('enabled'):
            return
        cache = self.meta.setdefault('demography', {})
        provinces = cache.setdefault('provinces', {})
        groups = cache.setdefault('groups', {})
        # Числовое тело (стресс, рождения, смерти, округление) — в
        # settlement_kernel: там же Python-эталон и fallback без собранного .pyd.
        # Ядро пишет те же поля тех же живых dict-ов, а строки отчёта собирает
        # и складывает этот метод, как и прежде.
        rows = _settlement.run_demography(
            self.entities, list(self.accounts.values()),
            sorted(e['id'] for e in self.entities.values() if e['kind'] == 'Province'),
            self.cfg['basic_needs'], provinces, groups, self.population, cfg,
            self.meta['month'], EPS)
        by_province = {}
        for row in rows:
            self.report['demography'].append(row)
            by_province[row['province_id']] = row
        self.report['demography_by_province'] = by_province

    def _province_population(self, province_id):
        # Компенсированная сумма населения провинции — в settlement_kernel.
        return _settlement.province_population(list(self.accounts.values()),
                                               province_id, self.population)

    def _split_people(self, total, households):
        """Точное целочисленное деление переселенцев пропорционально группам."""
        # Раскладка остатка по убыванию дробной части и по id — в ядре.
        return _settlement.split_people(households, total, self.population)

    def _migration_targets(self, source, destination):
        category = self.entities[source['group_id']]['properties'].get('category')
        households = [a for a in self.accounts.values()
                      if a['kind'] == 'household' and a['province_id'] == destination]
        matching = [a for a in households
                    if self.entities[a['group_id']]['properties'].get('category') == category]
        return matching or households

    def _move_household_share(self, source, destination, people):
        """Перенести людей и пропорциональную долю их денег и домашнего склада."""
        source_population = self.population(source)
        if people <= 0 or source_population <= 0:
            return dict(gold=0.0, stock={})
        targets = self._migration_targets(source, destination)
        if not targets:
            return dict(gold=0.0, stock={})
        target_split = self._split_people(people, targets)
        if sum(target_split.values()) != people:
            raise ValueError('Переселение потеряло население')
        share = people/source_population
        self.entities[source['group_id']]['properties']['population'] -= people
        for target in targets:
            moved = target_split.get(target['id'], 0)
            if moved:
                self.entities[target['group_id']]['properties']['population'] += moved
        gold = source['balance']*share
        for target in targets:
            moved = target_split.get(target['id'], 0)
            if moved:
                self.transfer(source['id'], target['id'], gold*moved/people, 'Переселение внутри государства')
        stock = {}
        for good, quantity in list(source['stock'].items()):
            moved_quantity = quantity*share
            if moved_quantity <= EPS:
                continue
            moved_value = source['values'].get(good, 0)*share
            before = source['stock'].get(good, 0)
            self.stock_store.set_stock(source['id'], good, max(0, quantity-moved_quantity))
            self._update_physical_stock_total(good, before, source['stock'][good])
            source['values'][good] = max(0, source['values'].get(good, 0)-moved_value)
            stock[good] = moved_quantity
            for target in targets:
                moved = target_split.get(target['id'], 0)
                if moved:
                    self.add_stock(target['id'], good, moved_quantity*moved/people,
                                   moved_value*moved/people)
        return dict(gold=gold, stock=stock)

    def apply_migration(self):
        """Медленное переселение между провинциями одного государства."""
        cfg = self.cfg['migration']
        rows = self.report.get('demography_by_province', {})
        if not cfg.get('enabled') or not rows:
            return
        # Числовое тело (давление, выбор цели, комната и число людей) — в
        # settlement_kernel: там же Python-эталон и fallback без собранного
        # .pyd. RNG перемешивания, маршруты, переводы и склад остаются
        # Python-хуками границы, поэтому порядок сделок и RNG прежние.
        moved_rows = _settlement.run_migration(rows, _settlement.MigrationHooks(self),
                                               cfg, EPS)
        self.report['migration'].extend(moved_rows)
        for row in rows.values():
            row['population_after'] = self._province_population(row['province_id'])

    def market_tile(self, account):
        return account.get('cell_id') or account['tile_id']

    def market_province(self, account):
        return self.tiles.get(self.market_tile(account), {}).get('province_id', account['province_id'])

    def project_cash_reserve(self, account, sells):
        # Смета, доставка, учёт купленного и недостача по котировке — в
        # money_boundary: она же держит эталонную Python-ветку и fallback без
        # собранного .pyd. Маршрут, смета и quoted_cost остаются хуками.
        return _money.project_cash_reserve(self, account, sells)

    def working_cash_reserve(self, e, sells):
        # Числовой срез уже живёт в dividend_kernel: граница переиспользует
        # его с теми же хуками, второй копии расчёта нет.
        return _money.working_cash_reserve(self, e, sells)

    def cash_after_commitments(self, account, project_reserve, working_reserve=0):
        # Числовая часть (два max в прежнем порядке) — в dividend_kernel;
        # граница лишь читает balance/reserved/protected через те же хуки.
        return _money.cash_after_commitments(self, account, project_reserve,
                                             working_reserve)

    def pay_dividends(self, sells):
        # Числовой срез (резерв, доступная касса, предел, доли) считает ядро;
        # выплаты, отчёт, события и RNG остаются здесь, в прежнем transfer.
        _dividend.pay_dividends(self, sells)

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
        # Числовой срез вынесен в owner_consumption_boundary: она же держит
        # эталонную Python-ветку и fallback без собранного .pyd. Порядок строк
        # и заявок, склад через существующую stock boundary и все side effects
        # остаются за Python-хуками границы.
        _owner_consumption.consume_owner_purchases(self, EPS)

    def transfer(self, sender, recipient, amount, reason):
        """Перевод — единственная точка изменения ``balance``.

        Тело живёт в ``EconomyServices.transfer``: там же владелец полей —
        ``CashStore``, колонки остаются источником истины, а словарь счёта
        получает зеркало. Здесь остаётся только дополнительный guard свободной
        кассы: у ``Economy`` зарезервированные и защищённые деньги двигать
        нельзя, а у сервиса такого ограничения нет. Порядок прежний: guard
        свободной кассы, затем конечность и знак, отсечение нуля, отсечение по
        балансу, списание и зачисление, одна запись в ``report['flows']``.
        """
        if amount > self.free_cash(sender)+EPS:
            raise ValueError('Деньги зарезервированы или недостаточны: '+sender)
        return EconomyServices.transfer(self, sender, recipient, amount, reason)

    def locked_material(self, account, good):
        # Числовой обход проекта, публичных строек, оснащения и поддержки —
        # в stock_boundary: там же Python-эталон и fallback без собранного .pyd.
        return _stock.locked_material(self.accounts[account], good)

    def start_public_project(self, city_id, kind, mode='normal'):
        """Создать муниципальный проект учреждения по статической смете."""
        self.activate_rules()
        city = self.entities.get(city_id)
        if not city or city['kind'] != 'City':
            raise ValueError('Город для стройки не найден')
        blueprint = self.cfg['institutions'].get(kind)
        if blueprint is None:
            raise ValueError('Неизвестный тип учреждения: '+str(kind))
        if mode not in self.cfg['construction_modes']:
            raise ValueError('Неизвестный режим строительства: '+str(mode))
        account = self.accounts.get('municipal:'+city_id)
        if not account:
            raise ValueError('У города нет муниципальной кассы')
        if account.get('public_projects'):
            raise ValueError('У города уже есть активная стройка')
        sequence = self.meta.get('public_project_sequence', 0)+1
        self.meta['public_project_sequence'] = sequence
        loc = self.location(city_id)
        project_id = f'public:{city_id}:{sequence}'
        project = dict(id=project_id, institution_id=f'institution-{city_id}-{sequence:05d}',
            kind=kind, name=blueprint['name'], city_id=city_id, location=loc, mode=mode,
            materials={}, equipment_held={}, support_stock={}, material_bill=copy.deepcopy(blueprint['materials']),
            equipment=copy.deepcopy(blueprint['equipment']),
            equipment_wear=copy.deepcopy(blueprint['equipment_wear']),
            work_required=float(blueprint['work_required']), work_done=0.0,
            max_workers=float(blueprint['max_workers']), created=self.meta['month'],
            status='procurement')
        account['public_projects'] = [project]
        self.log_event('public_project.started', entity_id=project_id, region_id=loc['province_id'],
            cause='municipal_order', source='public_construction', project_id=project_id,
            institution_kind=kind, city_id=city_id, mode=mode, material_bill=project['material_bill'],
            equipment=project['equipment'], work_required=project['work_required'])
        return copy.deepcopy(project)

    def _claim_public_stock(self, account, project, bucket, good, required, sells):
        """Закрепить уже купленный товар за муниципальной стройкой."""
        held = project[bucket].get(good, 0)
        wanted = max(0, required-held)
        if wanted <= EPS:
            return held
        assign = min(wanted, max(0, account['stock'].get(good, 0)-self.locked_material(account['id'], good)))
        release = max(0, assign-self.free_stock(account['id'], good))
        store = self.order_store
        for offer in sells.get(good, []):
            if release <= EPS:
                break
            if offer['participant'] != account['id'] or offer['status'] in ('cancelled', 'expired'):
                continue
            take = min(release, offer['remaining'])
            offer['quantity'] -= take
            store.set_remaining(offer, offer['remaining']-take)
            offer['backing'] -= take
            self.stock_store.release_reserved(account['id'], good, take)
            release -= take
            self.mark_market_quotes_dirty(good)
            if offer['remaining'] <= EPS:
                self.cancel_order(offer, 'Материалы закреплены за городской стройкой')
        assign = min(wanted, self.free_stock(account['id'], good))
        project[bucket][good] = held+assign
        return project[bucket][good]

    def public_project_requests(self, sells):
        """Закупки городских строек идут через тот же рынок, что и все товары."""
        for account in sorted(self.accounts.values(), key=lambda a: a['id']):
            for project in account.get('public_projects', []):
                for bucket, requirements, purpose in (
                    ('materials', project['material_bill'], 'Материалы городской стройки'),
                    ('equipment_held', project['equipment'], 'Оснащение городской стройки')):
                    for good, required in requirements.items():
                        held = self._claim_public_stock(account, project, bucket, good, required, sells)
                        missing = max(0, required-held)
                        if missing > EPS:
                            _, price, _ = self.quoted_cost(account, good, missing, sells)
                            order = self.order(account['id'], good, missing, 'buy', price, purpose)
                            if order:
                                order['project_id'] = project['id']
                support = self.cfg['construction_modes'][project['mode']]['support_goods']
                for good, per_worker in support.items():
                    required = project['max_workers']*per_worker
                    held = self._claim_public_stock(account, project, 'support_stock', good, required, sells)
                    missing = max(0, required-held)
                    if missing > EPS:
                        _, price, _ = self.quoted_cost(account, good, missing, sells)
                        order = self.order(account['id'], good, missing, 'buy', price,
                            'Содержание ускоренной стройки')
                        if order:
                            order['project_id'] = project['id']

    def reserve_expedited_construction_labor(self):
        """До производства забронировать дорогие бригады, вытесняющие обычный труд."""
        # Труд провинции, готовность, предел поддержки и минимум пяти аргументов
        # считает money_boundary: она же держит эталонную Python-ветку и
        # fallback без собранного .pyd. Порядок счетов и RNG остаются здесь.
        _money.reserve_expedited_construction_labor(self)

    def advance_public_projects(self):
        """Постройки используют только работников, не нанятых предприятиями в этом месяце."""
        # Числовой срез (готовность, работники, wage bill, надбавка за жильё)
        # вынесен в wage_boundary: она держит Python-эталон и fallback без
        # собранного .pyd. Выплаты идут прежним distribute, а склад, износ,
        # отчёт, завершение стройки и порядок счетов остаются за Python-хуками.
        _wage.advance_public_projects(self)

    def _complete_public_project(self, account, project):
        asset_value = 0.0
        for good, quantity in project['material_bill'].items():
            project['materials'][good] -= quantity
            asset_value += self.use_stock(account['id'], good, quantity)
            self.report['construction_used'][good] = self.report['construction_used'].get(good, 0)+quantity
        properties = dict(kind=project['kind'], status='active', level=1,
            city_id=project['city_id'], state_id=project['location']['state_id'],
            province_id=project['location']['province_id'], tile_id=project['location']['tile_id'],
            built_month=self.meta['month'], asset_value=asset_value,
            construction_project_id=project['id'])
        institution = dict(id=project['institution_id'], name=project['name'], properties=properties)
        self.entities[institution['id']] = dict(institution, kind='Institution')
        self.pending_institutions.append(institution)
        self.report['lifecycle'].append(dict(enterprise=institution['id'], action='built',
            good=project['kind'], province_id=project['location']['province_id'],
            reason='Муниципальная стройка завершена'))
        self.log_event('public_project.completed', entity_id=institution['id'],
            region_id=project['location']['province_id'], cause='work_completed',
            source='public_construction', project_id=project['id'], institution_kind=project['kind'],
            city_id=project['city_id'], asset_value=asset_value)

    def free_stock(self, account, good):
        # ``max(0, stock - reserved - locked_material)`` считает stock_boundary:
        # там же Python-эталон и fallback без собранного .pyd.
        return _stock.free_stock(self.accounts[account], good)

    def use_stock(self, account, good, quantity):
        if quantity > self.free_stock(account, good)+EPS:
            raise ValueError('Товар уже закреплён за продажей или стройкой')
        return _stock.use_stock(self, account, good, quantity)

    @property
    def order_store(self):
        """Единая граница мутаций заявок: живой список ``report['orders']``.

        Хранилище не копирует заявки: оно получает сам список и меняет поля
        тех же словарей. Ссылка живёт месяц и перепроверяется по тождеству
        списка — если отчёт заменили, граница пересоздаётся, иначе она писала
        бы в список прошлого месяца. Горячие циклы берут ссылку один раз на
        вызов, чтобы не платить property на каждую мутацию.
        """
        orders = self.report['orders']
        store = self.__dict__.get('_order_store')
        if store is None or store.orders is not orders:
            store = self._order_store = _order.OrderStore(orders)
        return store

    @property
    def cash_store(self):
        """Единая граница денежных мутаций: живой словарь ``self.accounts``.

        Хранилище не копирует счета: оно получает сам словарь и меняет
        ``balance``/``reserved``/``protected`` тех же словарей. Ссылка
        перепроверяется по тождеству словаря — ``step`` пересобирает
        ``self.accounts`` сортировкой, и без сверки граница писала бы в счета
        прошлого месяца. Горячие циклы берут ссылку один раз на вызов.

        Пересборка словаря — не импорт значений: если окно владения открыто,
        старые колонки сначала сверяются с теми же счетами, и только потом
        новое хранилище продолжает работу в том же строгом режиме. Иначе одна
        сортировка словаря выключала бы проверку до конца месяца.
        """
        accounts = self.accounts
        store = self.__dict__.get('_cash_store')
        if store is None:
            store = self._cash_store = _cash.CashStore(accounts)
        elif store.accounts is not accounts:
            store = self._cash_store = self._handover_cash_store(store, accounts)
        return store

    def _handover_cash_store(self, previous, accounts):
        """Перепривязать владельца к пересобранному словарю счетов.

        ``step`` и ``restore_state`` заменяют ``self.accounts`` целиком.
        Пересборка сортировкой — это те же живые счета, поэтому колонки
        обязаны совпасть с dict: расхождение означает запись мимо владельца и
        падает при открытом окне. Замена счетов новыми объектами — импорт
        (checkpoint, откат хода), и значения берутся из словаря как есть.
        """
        runtime = bool(getattr(previous, 'runtime', False))
        if runtime:
            previous.verify()
        store = _cash.CashStore(accounts)
        if runtime:
            store.begin_runtime()
        return store

    @property
    def world_state(self):
        """Единый контейнер состояния: ссылки на уже существующие хранилища.

        Собственного состояния здесь нет. ``WorldState`` хранит те же
        ``OrderStore``/``CashStore``, которые отдают ``order_store`` и
        ``cash_store``, и живой native update state, поэтому старые обращения
        продолжают работать без изменений. Контейнер живёт между ходами и
        перепривязывается по тождеству ``report['orders']`` и ``accounts``:
        ``step`` пересобирает и отчёт, и счета, а значения игры контейнер не
        трогает. Создаётся лениво, при первом обращении.
        """
        state = self.__dict__.get('_world_state')
        if state is None:
            state = self._world_state = _world.WorldState()
        return state.sync(self)

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
            self.cash_store.add_reserved_cash(participant, o['reserved'])
        else:
            self.stock_store.add_reserved(participant, good, quantity)
        self.order_store.append(o)
        if side == 'sell':
            self.mark_market_quotes_dirty(good)
        else:
            self._demand_revision[good] += 1
        return o

    def mark_market_quotes_dirty(self, good=None):
        """Сбросить точные котировки после изменения доступного предложения.

        Версия ведётся и общая, и по товару. Общая нужна кэшам, которые
        смотрят на книгу целиком; по товару — прогнозу выпуска, которому важны
        только книги его входов и его собственного товара. Без этого сделка по
        рыбе сбрасывала расчёт хлебопекарни, и кэш почти не попадал.
        """
        self._market_quote_version = getattr(self, '_market_quote_version', 0)+1
        versions = self.__dict__.setdefault('_market_quote_versions', defaultdict(int))
        if good is None:
            # Полная инвалидация: книгу пересобрали целиком, точечных версий мало.
            self._market_quotes_epoch = getattr(self, '_market_quotes_epoch', 0)+1
        else:
            versions[good] += 1
        self.__dict__.pop('_quoted_cost_cache', None)
        self.__dict__.pop('_reference_price_cache', None)

    def cancel_order(self, order, reason='Отмена заявки'):
        if order['status'] in ('cancelled', 'expired'):
            return
        filled = order['status'] == 'filled'
        a = self.accounts[order['participant']]
        # cancel_order вызывается на каждую заявку закрытия рынка: ссылка на
        # границу берётся один раз, а не на каждое поле.
        store = self.order_store
        if order['side'] == 'buy':
            self.cash_store.release_reserved_cash(order['participant'], order['reserved'])
            store.set_reserved(order, 0.0)
            self._demand_revision[order['good']] += 1
        else:
            self.stock_store.release_reserved(order['participant'], order['good'], order['remaining'])
            self.mark_market_quotes_dirty(order['good'])
        if not filled:
            store.set_status(order, 'cancelled')
            order['reason'] = reason

    def freight_factors(self, good):
        """Вес товара и тариф перевозки — пара для расчёта доставки.

        Оба значения — авторские константы контента: игра их не меняет.
        Запоминаем их парой, чтобы не ходить в два словаря на каждый из 105
        тысяч вызовов landed. Произведение НЕ предвычисляется: выражение
        было ``distance*weight*rate`` и считается слева направо, а
        ``distance*(weight*rate)`` отличается в последнем бите и разводит
        траекторию с первого месяца.
        """
        factors = self.__dict__.setdefault('_freight_factors', {})
        pair = factors.get(good)
        if pair is None:
            pair = (self.goods[good]['weight'], self.cfg['transport_rate'])
            factors[good] = pair
        return pair

    def landed(self, seller_tile, buyer_tile, good, price):
        """Полная цена товара по неизменной в течение месяца сети.

        Расчёт вынесен в market_book_boundary: она держит Cython-ядро и
        буквальный Python-эталон как fallback. Кэш остаётся кэшем движка
        (``_landed_cache``), ключ и hit/miss-семантика прежние, второго кэша
        нет. Функция ничего не мутирует, кроме собственного кэша.
        """
        return _market_book_native.landed(self, seller_tile, buyer_tile, good,
                                          price)

    def _customer_orders(self, orders, good):
        """Индекс заявок покупателей по товару для одной фазы рынка.

        Числовой срез (отбор пар ``side/good``) — в demand_boundary; ключ кэша,
        его проверка тождества списка и запись остаются здесь, как и прежде.
        """
        return _demand._customer_orders(self, orders, good)

    # После клиринга исходные заявки ещё будут отменены, но планирование
    # следующей фазы должно видеть их состояние на момент закрытия рынка.
    # Раньше здесь делался deepcopy всего графа диагностических объектов.
    _PLANNING_ORDER_FIELDS = (
        'id', 'participant', 'good', 'quantity', 'remaining', 'price', 'side',
        'purpose', 'status', 'reason', 'expires', 'final_consumption',
        'state_id', 'province_id', 'tile_id', 'reserved', 'backing',
        'adaptive_input_budget', 'superseded_by_top_up')

    def _planning_order_snapshot(self, order):
        """Компактная копия заявки для пострыночного планирования.

        Только скаляры, которые читают offers/customer_quotes/quoted_cost и
        project_cash_reserve. Вложенные match_observation и пути не нужны.
        """
        return {field: order[field] for field in self._PLANNING_ORDER_FIELDS
                if field in order}

    def _planning_market_snapshot(self, sells):
        """Снимок книг без рекурсивного копирования диагностических данных."""
        return {good: [self._planning_order_snapshot(order) for order in orders]
                for good, orders in sells.items()}

    def _customer_quote_index(self, tile_id, good, orders):
        """Доставка от площадки к покупателям без цены производителя.

        Обход заявок и ``landed`` — в demand_boundary; ключ кэша котировок,
        проверка тождества списка и запись остаются здесь, как и прежде.
        """
        return _demand._customer_quote_index(self, tile_id, good, orders)

    def _orders_in_route_component(self, orders, destination_tile):
        """Вернуть только заявки из физически достижимой части карты.

        Группировка строится один раз для конкретного списка заявок. Живые
        объекты заявок сохраняются: сделки продолжают менять те же остатки.
        Ключ кэша и ``route_component`` остаются в demand_boundary.
        """
        return _demand._orders_in_route_component(self, orders, destination_tile)

    def _market_book(self, buyer, good, sells, record=False):
        """Одна отсортированная доска предложений на рынок назначения.

        Цена доставки зависит от назначения, поэтому книга общая не для всего
        товара, а для пары (товар, торговая площадка покупателя). Сами заявки
        остаются живыми объектами: сделки меняют только remaining/status.

        Построение книги (фильтр достижимых офферов, landed price, потоки
        площадок и точная сортировка по ``(landed_price, sell_id)``) вынесено
        в market_book_boundary. Кэш книги остаётся кэшем движка, а хуками
        остаются ``market_tile``/``route_component``/``_market_source_groups``,
        ``_record_market_board`` и инвалидация.
        """
        books = getattr(self, '_market_books', {})
        sell_list = sells.get(good, [])
        key = (id(sell_list), self.market_tile(buyer), good)
        entry = books.get(key)
        if _market_book_native.market_book_is_fresh(
                entry, sell_list, self.__dict__.get('_market_quotes_epoch', 0)):
            # Книга переживает замену хранилища (например, отчёт заменили), а
            # номера слотов принадлежат конкретному OrderStore. Ссылка берётся
            # прямо из __dict__, без property: попадание книги бывает десятки
            # тысяч раз за месяц, и проверка через property стоила бы дороже
            # самого чтения. При подмене номера пересчитываются.
            if entry.get('slot_store') is not self.__dict__.get('_order_store'):
                entry['slot_store'] = self.__dict__.get('_order_store')
                entry['slots'] = None
                entry['slot_version'] = None
            if record and entry['board_id'] is None:
                self._record_market_board(entry, buyer, good)
            return entry

        buyer_tile = self.market_tile(buyer)
        # Один явный native call site: ядро собирает options, а фильтрация
        # достижимости, потоки площадок и расчёт landed остаются хуками
        # движка (метод передаётся привязанным, чтобы наследники сохраняли
        # собственное переопределение landed).
        landed = self.landed
        options = _market_book_native.build_options_batch(
            self, sell_list, buyer_tile, good, landed,
            additive_freight=getattr(landed, '__func__', None) is Economy.landed)
        entry = dict(source=sell_list, options=options,
            quotes={sell['id']: quote for _, sell, quote in options},
            board_id=None, first_live=0,
            # Книга запоминает эпоху котировок: смена эпохи означает, что
            # книгу пересобрали в обход invalidate_market_books.
            quote_epoch=self.__dict__.get('_market_quotes_epoch', 0),
            # Книга запоминает хранилище, из которого пришли слоты заявок:
            # представление читает колонки по номерам, а не по id(order).
            slot_store=self.order_store, slots=None, slot_version=None)
        self.store_market_book(key, entry)
        if record:
            self._record_market_board(entry, buyer, good)
        return entry

    def _market_source_groups(self, sell_list, destination_tile):
        """Отсортированные потоки офферов по площадкам продавцов.

        Цена внутри одной площадки уже отсортирована по цене продавца и ID.
        Доставка добавляет площадке одну и ту же константу, поэтому потоки
        можно сливать кучей, не сортируя общий список заново для каждого рынка.
        """
        cache = getattr(self, '_market_source_index', {})
        key = (id(sell_list), len(sell_list))
        entry = cache.get(key)
        groups = entry[1] if entry is not None and entry[0] is sell_list else None
        if groups is None:
            by_tile = defaultdict(list)
            for sell in sell_list:
                by_tile[sell['tile_id']].append(sell)
            by_component = defaultdict(list)
            for source_tile, source_sells in by_tile.items():
                component = self.route_component(source_tile)
                by_component[component].append((source_tile, tuple(sorted(
                    source_sells, key=lambda sell: (sell['price'], sell['id'])))))
            groups = {component: tuple(source_rows)
                      for component, source_rows in by_component.items()}
            cache[key] = (sell_list, groups)
            self._market_source_index = cache
        return groups.get(self.route_component(destination_tile), ())

    def invalidate_market_books(self):
        """Явно сбросить книги после изменения цены, тайла или состава офферов.

        Обычный ход этого не требует: набор офферов создаётся заново в
        sell_orders(). Метод нужен редактору мира и лабораторным сценариям,
        которые вручную меняют уже опубликованный оффер.
        """
        self.clear_market_books()
        self.mark_market_quotes_dirty()

    def _record_market_board(self, book, buyer, good):
        by_id = {sell['id']: quote for _, sell, quote in book['options']}
        snapshot = []
        for sell in book['source']:
            quote = by_id.get(sell['id'])
            snapshot.append(dict(sell_order=sell['id'], seller=sell['participant'],
                price=sell['price'], quantity=sell['quantity'], remaining=sell['remaining'],
                status=sell['status'], province_id=sell['province_id'], tile_id=sell['tile_id'],
                route_available=quote is not None, landed_price=quote[0] if quote else None,
                freight_per_unit=quote[1] if quote else None,
                distance=quote[2] if quote else None))
        board_id = f'{self.meta["month"]}:board:{len(self.report["market_boards"])}'
        self.report['market_boards'].append(dict(board_id=board_id, month=self.meta['month'],
            good=good, buyer_tile=self.market_tile(buyer),
            created_trade_index=len(self.report['trades']), offers=snapshot))
        book['board_id'] = board_id

    def offers(self, buyer, good, sells, local_only=False):
        book = self._market_book(buyer, good, sells)
        # Представление живых офферов строит native-модуль (иначе эталон на
        # Python): книга и её заявки остаются теми же объектами движка.
        return _offer.OfferView(book, buyer['id'], buyer['province_id'], local_only)

    def replay_market_board(self, board_id, trade_index=None):
        """Восстановить остатки книги перед указанной сделкой из snapshot+events."""
        board = next(row for row in self.report['market_boards'] if row['board_id'] == board_id)
        limit = len(self.report['trades']) if trade_index is None else trade_index
        state = {row['sell_order']: dict(row) for row in board['offers']}
        store = self.order_store
        for trade in self.report['trades'][board['created_trade_index']:limit]:
            row = state.get(trade['sell_order'])
            if row is None:
                continue
            # Восстановленная строка — диагностическая копия оффера, но поля
            # заявки меняются той же границей: прямой записи в обход неё нет.
            store.set_remaining(row, max(0, row['remaining']-trade['quantity']))
            store.set_status(row, 'filled' if row['remaining'] <= EPS else 'partial')
        return list(state.values())

    def reference_price(self, buyer, good, sells):
        # Числовой срез (книга, ориентир рынка, кэш котировок) вынесен в
        # quote_boundary: она же держит эталонную Python-ветку и fallback без
        # собранного .pyd. Книга, landed и market_signal остаются хуками.
        return _quote.reference_price(self, buyer, good, sells)

    def quoted_cost(self, buyer, good, quantity, sells):
        # Книга, ориентир восстановления и кэш котировок — в quote_boundary;
        # порядок обхода книги, int/float-типы и ранний выход сохранены.
        return _quote.quoted_cost(self, buyer, good, quantity, sells)

    def input_bid_ceiling(self, buy):
        """Предельная цена автоматической закупки при зафиксированной марже."""
        # Проверки плана и отбор заявок по входам — в quote_boundary: ядро
        # считает только сложение и деление, чтение состояния остаётся здесь.
        return _quote.input_bid_ceiling(self, buy)

    def reprice_input_order(self, buy, sells):
        # Число (цена и доплата) считает ядро; резерв кассы, поля заявки и
        # ревизия спроса остаются здесь, в прежних stores.
        _quote.reprice_input_order(self, buy, sells)

    def purchase_quantity(self, buy, sells):
        """Обычный рынок не ограничивает количество сверх самой заявки."""
        return buy['remaining']

    def match(self, buy, sells, local_only=False):
        # Один buyer order целиком (цена, книга, сделки, статус и наблюдение)
        # выполняет native-граница: Python-тело осталось только эталоном в
        # native/match_boundary.py, чтобы у правила не было двух копий.
        _match.run_buyer(self, buy, sells, EPS, _trade.commit_trade_atomic,
            _match.run_match, local_only)

    def observe_match(self, buy, sells, options):
        # Полный список продавцов уже записан один раз в market_boards. Здесь
        # остаются только координаты для точного воспроизведения состояния.
        deep = self.report_detail() == 'deep'
        book = self._market_book(self.accounts[buy['participant']], buy['good'], sells, record=deep)
        if not deep:
            return {'_book': book}
        match_index = getattr(self, '_match_observation_count', 0)
        self._match_observation_count = match_index+1
        observation = dict(version=2, board_id=book['board_id'],
            match_index=match_index,
            trade_index_before=len(self.report['trades']), remaining_before=buy['remaining'],
            reserved_before=buy['reserved'])
        # Живой объект нужен только до finish_match_observation и не попадает в БД.
        observation['_book'] = book
        buy['match_observation'] = observation
        return observation

    def finish_match_observation(self, buy, sells, observation):
        book = observation.pop('_book')
        deep = 'version' in observation
        if buy['remaining'] <= EPS:
            code = 'filled'
        else:
            reachable, affordable, historical_offer = False, [], False
            exhausted_reachable = False
            active_offer = False
            for sell in book['source']:
                if sell['participant'] == buy['participant']:
                    continue
                if sell['quantity'] > EPS and sell['status'] not in ('cancelled', 'expired'):
                    historical_offer = True
                quote = book['quotes'].get(sell['id'])
                if sell['status'] in ('cancelled', 'expired') or sell['remaining'] <= EPS:
                    exhausted_reachable = exhausted_reachable or quote is not None and sell['quantity'] > EPS
                    continue
                active_offer = True
                if quote is None:
                    continue
                reachable = True
                if quote[0] <= buy['price']+EPS:
                    affordable.append((quote[0], sell))
            if affordable:
                code = 'insufficient_order_reserve' if all(
                    min(buy['remaining'], sell['remaining'], buy['reserved']/price) <= EPS
                    for price, sell in affordable) else 'executable_not_matched'
            elif reachable:
                code = 'landed_price_above_limit'
            elif active_offer or historical_offer:
                code = 'offer_exhausted' if exhausted_reachable else 'no_route' if active_offer else 'offer_exhausted'
            else:
                holders = self.physical_stock_summary(buy['participant'], buy['good'])
                code = 'stock_not_offered' if holders['count'] else 'no_physical_stock'
        observation['reason_code'] = code
        if deep:
            observation.update(trade_index_after=len(self.report['trades']),
                remaining_after=buy['remaining'], reserved_after=buy['reserved'])
        if deep and buy['remaining'] > EPS:
            observation['stock_holders_after_match'] = self.physical_stock_summary(
                buy['participant'], buy['good'])

    def sell_orders(self, exclude=()):
        # Новый набор предложений начинается с каждого торгового прохода.
        # exclude использует только lagged_batch, чтобы не продать склад фирмы
        # одновременно старым sell_orders и её новым FirmPlan.
        excluded = set(exclude) if exclude else None
        self.invalidate_market_books()
        result = defaultdict(list)
        for a in self.shuffled(self.accounts.values()):
            e = self.enterprises.get(a['id'])
            if (excluded is not None and a['id'] in excluded) or a['kind'] == 'household' \
                    or (e and e.get('suspended')):
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
                    deployed = min(e['capacity'], max(planned, recent))
                    quantity = max(0, quantity-self.equipment_requirements(recipe, deployed).get(good, 0))
                prices = a.setdefault('ask_prices', {})
                price = e['price'] if e and good == e['good'] else prices.setdefault(good, self.goods[good]['price'])
                o = self.order(a['id'], good, quantity, 'sell', price, 'Продажа запасов')
                if o:
                    result[good].append(o)
        return result

    def customer_quotes(self, e, orders=None):
        # Числовой срез (предел платёжеспособности каждой заявки) считает
        # demand_boundary: она же держит эталонную Python-ветку и fallback без
        # собранного .pyd. Индекс котировок, landed и кэши остаются хуками.
        return _demand.customer_quotes(self, e, orders)

    def investment_customer_limit(self, e):
        """Лучшая цена спроса для кандидата без повторного скана заявок.

        Во время одного инвестиционного обзора заявки не исполняются и не
        отменяются. Для каждой площадки и товара достаточно запомнить лучшие
        цены по покупателям; две верхние позволяют исключить самого инвестора.
        """
        cache = getattr(self, '_investment_customer_limits', None)
        if cache is None:
            cache = {}
            self._investment_customer_limits = cache
        key = (e['tile_id'], e['good'])
        leaders = cache.get(key)
        if leaders is None:
            by_participant = {}
            orders = self.report.get('orders', [])
            for order, freight in self._customer_quote_index(e['tile_id'], e['good'], orders):
                if order['remaining'] <= EPS or order['status'] in ('cancelled', 'expired'):
                    continue
                limit = order['price']-freight
                by_participant[order['participant']] = max(
                    limit, by_participant.get(order['participant'], -math.inf))
            leaders = tuple(sorted(
                ((limit, participant) for participant, limit in by_participant.items()),
                reverse=True)[:2])
            cache[key] = leaders
        return next((limit for limit, participant in leaders
                     if participant != e['id'] and limit > 0), None)

    def customer_demand_groups(self, e, orders=None):
        """Агрегировать одинаковый спрос для оценки нового предложения.

        Реальные заявки и порядок сделок не меняются. Группировка используется
        только чистым прогнозом reachable_demand.

        Предел заявки (`limit = цена заявки − доставка`) от цены кандидата НЕ
        зависит, зависит только её предел платёжеспособности: `reserved` покрывает
        объём по `e['price']+freight`. Поэтому дешёвая часть (участник, площадка,
        цена заявки, предел, доставка) считается один раз на смену книги, а
        денежный объём — на каждую цену. Ответ остаётся прежним: отсев по цене
        здесь воспроизводит проверку `limit+EPS < e['price']`, которую делают
        оба вызывающих, только до деления денег на цену.
        """
        # Числовой срез (отсев по цене и платёжеспособность каждой заявки)
        # вынесен в demand_boundary; строки и их кэш остаются хуками.
        return _demand.customer_demand_groups(self, e, orders)

    def _customer_demand_rows(self, tile_id, good, orders):
        """Дешёвая часть агрегата спроса: плоские строки на смену книги.

        Ключ повторяет `_customer_quote_index`: тот же список заявок, товар и
        площадка, плюс ревизия спроса. В записи только скаляры, снятые с живых
        заявок в момент построения, поэтому запись обязана умереть от любого
        изменения покупательской заявки.

        Ревизия здесь именно `_demand_revision`, а не версия книги: версию
        поднимают сделки и изменения предложения, а отмена покупательской
        заявки её не трогает — она меняет только `reserved` и `status` и растит
        `_demand_revision`. На версии книги отменённая заявка оставалась в
        записи со старым резервом, и спрос считался по ней ещё раз.
        """
        # Ключ кэша, его проверка и запись остаются в demand_boundary: там же
        # эталонная Python-ветка и fallback без собранного .pyd.
        return _demand._customer_demand_rows(self, tile_id, good, orders)

    def reachable_demand(self, e, sells):
        # Числовой срез (покрытие групп живой книгой) вынесен в demand_boundary:
        # она же держит эталонную Python-ветку и fallback без собранного .pyd.
        # Книга, offers и счета остаются Python-хуками.
        return _demand.reachable_demand(self, e, sells)

    def unit_cost(self, e, sells):
        # Компенсированные суммы raw/wear и кэш себестоимости — в
        # quote_boundary: reference_price остаётся хуком, порядок рецепта тот же.
        return _quote.unit_cost(self, e, sells)

    def volume_input_prices(self, e, quantity, sells):
        """Средние доставленные цены входов для объёма производственного плана."""
        # Порядок рецепта и выбор quoted_cost/reference_price — в
        # quote_boundary; склад и книга остаются хуками движка.
        return _quote.volume_input_prices(self, e, quantity, sells)

    def net_price(self, e):
        # Числовой цикл вынесен в net_price_boundary: она же держит эталонную
        # Python-ветку и fallback без собранного .pyd. Котировки покупателей
        # по-прежнему строит Python-хук границы, порядок пар и RNG не тронуты.
        return _net_price.net_price(self, e)

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
            for good, quantity in self.equipment_wear(recipe, 1).items():
                quotes = [quote[0] for firm, price in self._recovery_quotes[good]
                    if (quote := self.landed(firm['tile_id'], e['tile_id'], good, price))]
                cost += quantity*(min(quotes) if quotes else self.goods[good]['price'])
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
            for good, amount in self.equipment_requirements(recipe, target).items():
                missing = max(0, amount-self.free_stock(e['id'], good))
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
                # Защита кассы — денежное поле счёта, поэтому её двигает
                # владелец полей, а не прямая запись в dict.
                self.cash_store.add_protected_cash(e['id'], hold)

    def expected_stock_sales(self, e, sells, reachable=None):
        """Продажа старого склада до поступления планируемого нового выпуска.

        `reachable` передаётся вызывающим, если он уже посчитал достижимый
        спрос по той же цене: внутри прогноза выпуска это второй такой расчёт
        подряд, и он был 38% всех вызовов reachable_demand.
        """
        # Числовой срез (сумма своих офферов, средние продажи и min трёх
        # величин) вынесен в demand_boundary; кэш `_stock_sales_cache` остался
        # кэшем движка, порядок вызовов reachable_demand прежний.
        return _demand.expected_stock_sales(self, e, sells, reachable)

    def _outlook_quote_versions(self, e):
        """Версии книг только тех товаров, от которых зависит прогноз выпуска.

        Предприятие покупает входы и продаёт один товар, поэтому сделка по
        чужому товару не меняет ни себестоимость, ни достижимый спрос. Набор
        товаров задан рецептом и не меняется внутри игры, поэтому его можно
        запомнить: он не часть месячного состояния.
        """
        goods = self.__dict__.setdefault('_outlook_goods', {}).get(e['good'])
        if goods is None:
            recipe = self.goods[e['good']]
            goods = tuple(sorted(set(recipe['inputs'])
                                 | set(self.equipment_requirements(recipe, 1)) | {e['good']}))
            self._outlook_goods[e['good']] = goods
        versions = self.__dict__.get('_market_quote_versions')
        epoch = getattr(self, '_market_quotes_epoch', 0)
        # Полная пересборка книги поднимает epoch и сбрасывает точечные версии.
        return (epoch, tuple(versions.get(good, 0) for good in goods) if versions else ())

    def production_outlook(self, e, sells=None):
        # Числовая часть прогноза вынесена в production_outlook_boundary: она
        # же держит эталонную Python-ветку и fallback без собранного .pyd.
        # Книга заявок, unit_cost, net_price, reachable_demand,
        # expected_stock_sales, оброк, отчёт и кэш прогноза остаются
        # Python-хуками, порядок обхода и RNG не тронуты.
        return _production_outlook.production_outlook(self, e, sells)

    def plan(self, e, history=None, sells=None):
        _enterprise_state.apply_outlook(e, self.production_outlook(e, sells))
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
            if limits and last['sold'] > EPS and max(limits) > price+EPS:
                price = min(price*(1+self.rules['price_step']), max(limits))
            elif limits and max(limits) < price-EPS:
                price = max(price*(1-self.rules['price_step']), max(limits))
            else:
                handled = False
                if last['sold'] <= EPS:
                    # Обзор конкурентов нужен только этим двум ветвям. Раньше он
                    # строился до выбора ветви, то есть и там, где не читался:
                    # 1280 из 1926 обзоров за 6 месяцев были лишними.
                    competitors = self._price_competitors(e)
                    if competitors and min(competitors) < price-EPS:
                        price = max(price*(1-self.rules['price_step']), min(competitors))
                        handled = True
                    elif competitors and min(competitors) <= price+EPS:
                        # Ветвь выбрана, но цена меняется только при объёме рынка.
                        handled = True
                        a = self.accounts[e['id']]
                        signal = self.market_signal(self.meta.get('market_history', []), a['state_id'], a['province_id'], e['good'])
                        if signal['volume'] > EPS:
                            price *= 1-self.rules['price_step']
                if not handled:
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

    def _price_competitors(self, e):
        """Доставленные цены конкурентов с положительным запасом того же товара.

        Отдельный метод, чтобы вызов стоял внутри нужной ветви: цены
        изменяются последовательно, и поздняя фирма должна видеть уже
        обновлённую цену ранней, поэтому таблицу цен заранее не строим.
        """
        competitors = []
        for other in self.enterprises.values():
            if other['id'] == e['id'] or other['good'] != e['good'] or self.accounts[other['id']]['stock'].get(e['good'], 0) <= EPS:
                continue
            quote = self.landed(other['tile_id'], e['tile_id'], e['good'], other['price'])
            if quote:
                competitors.append(quote[0])
        return competitors

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
        # Числовой цикл допокупки вынесен в topup_boundary: отбор кандидатов,
        # обход товаров и книги офферов, расчёт missing/quantity. Перемешивание
        # (RNG), заявки, сделки, отчёт и dirty flags остаются Python-хуками
        # границы; порядок обхода и int/float-семантика прежние.
        _topup.top_up(self, households, sells, EPS)
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
        # Штатный расчёт закупок (cash_needed, 40 итераций бисекции, потребности
        # в сырье и оборудовании, резерв зарплат и production_plan) целиком в
        # plan_boundary: она же держит эталонную Python-ветку и fallback без
        # собранного .pyd. Заявки, деньги и склад двигают прежние stores через
        # хуки границы; котировки, прогноз выпуска и отчёт остаются Python.
        return _plan.input_requests(self, e, sells)

    def investment_demand(self, candidate, sells):
        """Сначала пригодные мощности и начатые проекты, затем новая стройка."""
        index = getattr(self, '_investment_index', None)
        demand_cache = getattr(self, '_investment_demand_cache', None)
        cache_key = None
        if index is not None and demand_cache is not None:
            own_orders = index['buyer_orders'].get((candidate['id'], candidate['good']), ())
            state = getattr(self, '_investment_state', None)
            revision = (state['supply_revision'][candidate['good']]
                        if state is not None else index['project_revision'])
            cache_key = (candidate['good'], candidate['tile_id'], candidate['price'], own_orders, revision)
            cached = demand_cache.get(cache_key)
            if cached is not None:
                self._investment_demand_cache_hits = getattr(self, '_investment_demand_cache_hits', 0)+1
                return cached
            self._investment_demand_cache_misses = getattr(self, '_investment_demand_cache_misses', 0)+1
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
                # Предел добычи по провинции не меняется за месяц: он считается
                # один раз при загрузке, а не перебором всех клеток для каждой
                # ресурсной фирмы.
                resource = self.resource_limits.get((firm['province_id'], recipe['resource']), 0)
                capacity = min(capacity, resource)
            suppliers.append([firm, price, capacity])
        if cached is not None:
            suppliers = [list(s) for s in cached]
        elif cache is not None:
            cache[candidate['good']] = [tuple(s) for s in suppliers]
        if index is not None:
            suppliers.extend([list(project) for project in index['projects'][candidate['good']]])
        else:
            for a in self.accounts.values():
                p = a.get('project')
                if p and p['good'] == candidate['good']:
                    suppliers.append([dict(p['location']), p['expected_price'], p['capacity']])
        residual = 0.0
        # Та же рабочая книга только для оцениваемого товара.
        cheap = {o['id']: o['remaining'] for o in sells.get(candidate['good'], [])}
        for participant, tile_id, order_price, limit, freight, volume in self.customer_demand_groups(candidate):
            if participant == candidate['id']:
                continue
            if limit+EPS < candidate['price']:
                continue
            buyer = self.accounts[participant]
            # Та же монотонная граница: книга отсортирована по доставленной
            # цене, поэтому остаток дороже заявки не покрывает ничего. Раньше
            # 78% просмотренных здесь предложений были за этой границей.
            for price, sell, _ in self.offers(buyer, candidate['good'], sells):
                if price > order_price+EPS:
                    break
                covered = min(volume, cheap[sell['id']])
                cheap[sell['id']] -= covered
                volume -= covered
            for supply in suppliers:
                # Спрос этой заявки уже покрыт дешёвым предложением: покрывать
                # нечего, covered = min(0, ...) = 0, и остатки мощностей не
                # меняются. Дальше проверять доставку бессмысленно — это 88%
                # вызовов landed в инвестиционной оценке.
                if volume <= EPS:
                    break
                if supply[2] <= EPS:
                    # Мощность исчерпана: covered = 0, доставку можно не считать.
                    continue
                quote = self.landed(supply[0]['tile_id'], tile_id, candidate['good'], supply[1])
                if quote and quote[0] <= order_price+EPS:
                    covered = min(volume, supply[2])
                    supply[2] -= covered
                    volume -= covered
            residual += volume
        if cache_key is not None:
            demand_cache[cache_key] = residual
        return residual

    def project_candidate(self, investor, sells, derived_demand=None, demand_region=None,
                          parent_project_id=None, diagnostics=None, allowed_goods=None):
        candidates = []
        history = self.meta.get('market_history', [])
        index = getattr(self, '_investment_index', None)
        if index is not None:
            markets = set(index['markets'].get(investor['state_id'], ()))
        else:
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
            if allowed_goods is not None and good not in allowed_goods:
                continue
            if expansion and (good != expansion['good'] or pid != expansion['province_id']):
                continue
            loc = {k: expansion[k] for k in ('state_id', 'province_id', 'city_id', 'tile_id')} if expansion else self.location(pid)
            if loc['state_id'] != investor['state_id']:
                continue
            recipe = self.goods[good]
            if recipe['resource']:
                tiles = (index['resource_tiles'].get((investor['state_id'], pid, recipe['resource']), [])
                         if index is not None else
                         [t for t in self.tiles.values() if t['province_id'] == pid and recipe['resource'] in t['resources']])
                if not tiles:
                    continue
                loc.update(tile_id=expansion['tile_id'] if expansion else self.shuffled(tiles)[0]['id'], city_id=None)
            quote_firm = dict(id=investor['id'], good=good, price=expansion['price'] if expansion else self.goods[good]['price'], **loc)
            customer_limit = self.investment_customer_limit(quote_firm)
            signal = self.market_signal(history, investor['state_id'], pid, good)
            price = customer_limit if customer_limit is not None else signal['price'] or self.goods[good]['price']
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
            state = getattr(self, '_investment_state', None)
            labor = (index['labor'].get((loc['state_id'], pid), 0.0)
                     if index is not None else
                     sum(self.population(a)*self.cfg['labor_share'] for a in self.accounts.values()
                         if a['kind'] == 'household' and a['state_id'] == loc['state_id']
                         and a['province_id'] == pid))
            if state is not None:
                labor -= state['labor_reserved'][loc['state_id'], pid]
            capacity = min(capacity, labor/recipe['labor'])
            if recipe['resource']:
                used = (index['resource_used'].get((pid, recipe['resource']), 0.0)
                        if index is not None else
                        sum(e['capacity'] for e in self.enterprises.values()
                            if e['status'] != 'closed' and e['province_id'] == pid
                            and self.goods[e['good']]['resource'] == recipe['resource']))
                if state is not None:
                    used += state['resource_reserved'][pid, recipe['resource']]
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
            equipment_capital = sum(ratio*self.quoted_cost(investor, equipment, ratio, sells)[1]
                for equipment, ratio in recipe.get('equipment', {}).items())
            available = self.free_cash(investor['id'])
            startup = 0 if expansion else self.cfg['startup_cash']
            capacity, working, investment, profit = project_finances(
                capacity, available, startup, material, delivery, unit,
                fixed_unit, equipment_capital, margin, bool(expansion))
            if capacity < self.cfg['minimum_capacity']:
                if diagnostics is not None:
                    diagnostics.append(dict(investor=investor['id'], good=good,
                        province_id=pid, stage='funding_capacity', capacity=capacity,
                        minimum=self.cfg['minimum_capacity']))
                continue
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
                parent_project_id=parent_project_id, diagnostics=diagnostics,
                allowed_goods={good})
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
            self._upstream_candidate_cache = self._investment_state['upstream_candidates']
        diagnostic_demands = []
        flow_reserved = {}
        flow_balances = None
        if due:
            state_ids = sorted({a['state_id'] for a in self.accounts.values() if a.get('state_id')})
            flow_balances = self.investment_flow_balances(state_ids)
        elif evaluate:
            flow_balances = self._investment_state['flow_balances']
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
                self.register_investment_project(project)
                self.report['investment_decisions'].append(dict(
                    kind='investment_decision', investor=investor['id'], outcome='resumed',
                    **copy.deepcopy(project)))
            else:
                waiting.append(item)
        queue[:] = waiting
        for a in self.shuffled(investors):
            project = (self.project_candidate(a, sells, allowed_goods=allowed_goods)
                if evaluate and 'project' not in a else None)
            if project:
                demand = dict(state_id=a['state_id'], good=project['good'],
                    quantity=project['capacity'], decision_id=project['project_id'],
                    project_id=project['project_id'], investor=a['id'],
                    province_id=project['location']['province_id'])
                diagnostic_demands.append(demand)
                self.report['investment_candidates'].append(copy.deepcopy(demand))
                flow_candidate = dict(state_id=a['state_id'], good=project['good'],
                    quantity=project['capacity'], project_id=project['project_id'])
                sustainable_capacity, flow_trial = self.sustainable_candidate_capacity(
                    flow_candidate, self._investment_state['flow_reserved'], flow_balances)
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
                available_inputs = {}
                for input_good, ratio in self.goods[project['good']]['inputs'].items() if project else ():
                    required = project['capacity']*ratio
                    available = sum(sell['remaining'] for _, sell, _ in self.offers(a, input_good, sells))
                    available_inputs[input_good] = available
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
                        queue.append(dict(created=self.meta['month'], investor=a['id'],
                                          project=copy.deepcopy(project)))
                    project = None
            if project:
                flow_reserved = flow_trial
                self._investment_state['flow_reserved'] = flow_reserved
                for input_good, ratio in self.goods[project['good']]['inputs'].items():
                    self._investment_input_reserved[input_good] += project['capacity']*ratio
                evidence = {}
                for input_good, ratio in self.goods[project['good']]['inputs'].items():
                    required = project['capacity']*ratio
                    available = available_inputs[input_good]
                    signal = self.market_signal(self.meta.get('market_history', []), a['state_id'],
                                                project['location']['province_id'], input_good)
                    evidence[input_good] = dict(required=round(required, 6),
                        available=round(available, 6), shortage=round(max(0, required-available), 6),
                        historical_unmet=round(signal['unmet'], 6), historical_volume=round(signal['volume'], 6))
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
            self.__dict__.pop('_investment_supply_cache', None)
            self.__dict__.pop('_investment_index', None)
            self.__dict__.pop('_investment_demand_cache', None)
            self.__dict__.pop('_upstream_candidate_cache', None)
            self.__dict__.pop('_investment_input_reserved', None)
            self.__dict__.pop('_investment_state', None)
            self.__dict__.pop('_investment_customer_limits', None)
        self.project_material_requests(sells, due, investors)

    def project_material_requests(self, sells, due, investors):
        """Последовательные закупки и отмены уже принятых проектов."""
        store = self.order_store
        for a in self.shuffled(investors):
            project = a.get('project')
            if not project:
                continue
            # Прогресс, порог простоя, причина и результат отмены — в нативном
            # срезе: ядро обновляет progress-state и решает, отменять ли.
            # Снятие проекта, protected и отчёт остаются хуком в границе.
            if _cancel.cancel_stalled_project(self, a, EPS):
                continue
            # Порог маржи/спроса, условие отмены и её причина — в нативном
            # срезе. Снятие проекта, protected и отчёт остаются тем же хуком.
            if _cancel.cancel_margin_demand_project(self, a, sells, due, EPS):
                continue
            loc = project['location']
            distance, _ = self.path(self.market_tile(a), loc['tile_id'])
            if distance is None:
                continue
            # Смета, доставка, защита денег и резерв материалов — в нативном срезе.
            # Строительные закупки на недостающий материал этот же срез берёт из
            # нативного construction_buy_kernel: граница подставляет свой хук при
            # импорте, поэтому тела закупки в Python больше нет.
            bill = _delivery.request_project_materials(self, a, sells, EPS, store, distance)
            # Финализация material request: затронутые товары и готовые journal-
            # строки считает ядро, отчёт пополняет хук. Повторных пометок
            # котировок и ревизий нет: их уже сделали release- и cancel-срезы.
            _material_finalize.apply_material_request(self, a, project, bill, EPS)

    def finish_projects(self):
        # Числовой срез завершения проектов (проверка сметы и кассы, asset,
        # запись capacity/asset_value при расширении) вынесен в
        # project_completion_boundary: она же держит Python-эталон и fallback
        # без собранного .pyd. Порядок счетов (list(values())), переводы через
        # прежний transfer, склад через существующую stock boundary, создание
        # предприятия и отчёты остаются за Python-хуками границы.
        _project_completion.finish_projects(self)

    def produce(self, existing, sells):
        # Числовой цикл production вынесен в production_boundary: она же держит
        # эталонную Python-ветку и fallback без собранного .pyd. Порядок
        # предприятий (shuffled), RNG, склад через существующую stock boundary и
        # все side effects остаются за Python-хуками границы.
        _production.produce(self, existing)

    def return_capital(self, e):
        self.cash_store.set_protected_cash(e['id'], 0)
        self.transfer(e['id'], e['owner_id'], self.free_cash(e['id']), 'Возврат капитала закрытого предприятия')

    def close_enterprise(self, e, unpaid=0):
        for o in self.report['orders']:
            if o['participant'] == e['id'] and o['side'] == 'buy':
                self.cancel_order(o, 'Предприятие закрыто')
        self.accounts[e['id']].pop('project', None)
        e.update(status='closed', closed=self.meta['month'], closure_reason='Двенадцать месяцев подряд убыточной работы', reason='Прекращение нежизнеспособной деятельности')
        self.return_capital(e)
        self.mark_investment_dirty(e['good'])
        self.report['lifecycle'].append(dict(enterprise=e['id'], good=e['good'], action='closed', reason=e['reason']))

    def settle(self, enterprises, households):
        _enterprise_settlement.settle_month(self, enterprises, households)

    def record_market(self):
        # Числовую агрегацию книги заявок и сделок (requested, limit_value,
        # unmet_solvent, reserved_unmet, volume, trade_gold, freight и отметка
        # o['unmet_solvent']) считает report_history_kernel: там же
        # Python-эталон и fallback без собранного .pyd. Ключи строк и их порядок
        # прежние; снимок заявки для public_orders остаётся Python-хуком.
        rows, public = _report_history.record_market_rows(
            self.report['orders'], self.report['trades'],
            _report_history.MARKET_FIELDS, EPS, self._planning_order_snapshot)
        self.meta['market_history'] = (self.meta.get('market_history', [])+[dict(month=self.meta['month'], markets=[dict(state_id=s, province_id=p, good=g, **r) for (s, p, g), r in sorted(rows.items(), key=lambda x: tuple(v or '' for v in x[0]))])])[-3:]
        # Обычная сделка не запускает инвестиции. Устойчивый платёжеспособный
        # дефицит помечает лишь товар и его потребителей для следующего месяца.
        history = self.meta['market_history']
        for (state_id, province_id, good), row in rows.items():
            if row['unmet_solvent'] <= EPS:
                continue
            repeated = sum(1 for month in history
                for market in month['markets']
                if market['state_id'] == state_id and market['province_id'] == province_id
                and market['good'] == good and market['unmet_solvent'] > EPS)
            if repeated >= 2:
                self.mark_investment_dirty(good)
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

    def plan_enterprises(self, existing, sells):
        # Штатный обход (порядок по убыванию глубины товара, условия
        # восстановления и планирования) выполняет plan_boundary. Перемешивание
        # списка остаётся здесь: это RNG месяца. Восстановление и закупки идут
        # прежними методами движка через хуки границы.
        _plan.plan_enterprises(self, existing, sells)

    def step(self, enterprise_planner=None, planning_mode='v3', investment_mode='v3', investment_workers=1,
             native_mode='off'):
        if native_mode not in ('off', 'cython'):
            raise ValueError('Неизвестный native-режим: '+str(native_mode))
        if native_mode == 'cython':
            # Ошибка сборки возникает до мутации пролога; fallback на V3 запрещён.
            # Проверяется и драйвер месяца: иначе собранный serial_step скрыл бы
            # отсутствие native-оркестрации и месяц молча ушёл бы в Python.
            from .native import load_serial_step
            from .native import monthly_boundary
            load_serial_step()
            monthly_boundary.require_native_monthly()
        if planning_mode not in ('v3', 'lagged_batch'):
            raise ValueError('Неизвестный режим планирования: '+str(planning_mode))
        if investment_mode not in ('v3', 'snapshot_batch', 'async_snapshot_batch'):
            raise ValueError('Неизвестный инвестиционный режим: '+str(investment_mode))
        if investment_mode in ('snapshot_batch', 'async_snapshot_batch') and planning_mode != 'v3':
            raise ValueError('snapshot_batch пока совместим только с планированием v3')
        if planning_mode == 'lagged_batch' and enterprise_planner is not None:
            raise ValueError('lagged_batch несовместим с enterprise_planner')
        # Оркестрация месяца живёт в Cython: единственный внешний вызов на
        # месяц вместо десятков переходов Python→Cython между фазами.
        # Фасад остаётся тонким: проверить режимы, собрать внешний контекст,
        # вызвать monthly_step и вернуть живой отчёт. Всё, что делает
        # monthly_step, — прежний порядок фаз с прежним RNG и хуками.
        from .native import monthly_boundary
        # Буфер событий месяца настраивается до фазы: детализация решает, какие
        # каналы открыты (events — только deep), батч и предел очереди заданы
        # здесь же. Это только числа и фильтры — ни строк, ни диска.
        recorder = self.event_recorder
        recorder.configure(self.report_detail(), batch_size=256,
                           capacity=4096, drop_diagnostic=False)
        # Окно владения cash открыто ровно на месяц: вход в него — импорт уже
        # лежащего в словаре состояния (checkpoint, откат, инструмент), выход —
        # сверка колонок с dict. Внутри месяца деньги пишет только CashStore,
        # поэтому прямой writer в dict даёт ошибку, а не скрытое восстановление.
        self.cash_store.begin_runtime()
        completed = False
        try:
            report = monthly_boundary.monthly_step(self, EPS, planning_mode,
                investment_mode, investment_workers, native_mode, enterprise_planner)
            completed = True
        finally:
            # Ошибка внутри месяца уже несёт причину; вторая ошибка сверки
            # замаскировала бы её, поэтому при исключении окно закрывается без
            # проверки. Успешный месяц обязан пройти сверку.
            self.cash_store.end_runtime(check=completed)
        # Готовые батчи уходят в фон без ожидания диска. Если writer не
        # подключён, батчи отпускаются, но счётчики буфера остаются: replay
        # по-прежнему живёт в report['events'] и ничего не теряется.
        _event_writer.drain_recorder(recorder, self.event_writer)
        return report

    def validate(self, before=None):
        super().validate(before)
        # Read-only цикл проверок населения и счетов вынесен в
        # settlement_boundary: она держит Python-эталон и fallback без
        # собранного .pyd. Ядро ничего не мутирует — только возвращает код
        # первой неудачи, а точный текст ValueError и отметки
        # report['invariants'] остаются за Python-хуками границы.
        _settlement.validate(self, EPS)


def initialize(service):
    """Создать экономическое состояние нового мира один раз, в памяти.

    Старых экономических записей в Ladybug больше нет: `EconomyMeta` удалена из
    схемы. Признак инициализации — живое состояние сервиса, а не строка в БД.
    """
    if not service.db.campaign():
        return False
    state = service.economy_state()
    if state.initialized:
        return False
    state.bootstrap()
    event = service.db.record_event(service.event('economy_initialized',
        dict(gold=state.engine.gold(), enterprises=len(state.engine.enterprises),
             rules_version=3), [], source='economy',
        summary='Начальное состояние экономики V3'))
    service.notify(event)
    return True


def monthly_phase(service, ctx):
    """Месяц экономики: тот же объект состояния, никакой загрузки и записи в БД.

    `state.step()` и есть весь ход: состояние остаётся в памяти, а отчёт
    месяца уходит в Parquet только по явному сохранению/autosave.
    """
    state = service.economy_state()
    if not state.initialized:
        state.bootstrap()
    report = state.step()
    active = sum(e['status'] == 'active' for e in state.engine.enterprises.values())
    ctx['changes'].append(f'Экономика V3: {active} предприятий, {len(report["trades"])} сделок')
    event = service.db.record_event(service.event('economy_month',
        dict(month=report['month'], invariants=report['invariants'], trades=len(report['trades']), rules_version=3), [],
        source='economy', action_id=ctx['command_id'], summary=ctx['changes'][-1]))
    ctx['events'].append(event)
    return len(state.engine.enterprises)
