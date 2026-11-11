"""Чистая рыночная оценка инвестиций и последовательный commit."""
import copy
import multiprocessing
import time
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, replace
from types import MappingProxyType

from modules.economy.support import EPS
from modules.economy.v3 import project_finances


@dataclass(frozen=True, slots=True)
class RecipeSnapshot:
    inputs: tuple
    equipment: tuple
    wear: tuple
    labor: float
    resource: str | None
    price: float
    weight: float


@dataclass(frozen=True, slots=True)
class InvestorSnapshot:
    id: str
    state_id: str
    province_id: str
    tile_id: str
    available_cash: float
    stock: tuple
    risk_tolerance: float


@dataclass(frozen=True, slots=True)
class OfferSnapshot:
    id: str
    participant: str
    tile_id: str
    price: float
    remaining: float


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    participant: str
    tile_id: str
    price: float
    remaining: float
    reserved: float
    final_consumption: bool


@dataclass(frozen=True, slots=True)
class SupplierSnapshot:
    id: str
    good: str
    tile_id: str
    state_id: str
    province_id: str
    price: float
    production_price: float
    capacity: float
    funds: float
    labor_available: float
    resource_limit: float | None


@dataclass(frozen=True, slots=True)
class ProjectSupply:
    tile_id: str
    price: float
    capacity: float

    def __iter__(self):
        return iter((self.tile_id, self.price, self.capacity))


@dataclass(frozen=True, slots=True)
class HistoryRow:
    state_id: str
    province_id: str
    good: str
    requested: float
    volume: float
    trade_gold: float
    freight: float
    limit_value: float
    unmet: float


@dataclass(frozen=True, slots=True)
class InvestmentCandidate:
    key: str
    investor_id: str
    good: str
    province_id: str
    location: tuple
    expansion: bool
    parent_project_id: str | None = None
    derived_demand: float | None = None


@dataclass(frozen=True, slots=True)
class DemandGroupSnapshot:
    participant: str
    tile_id: str
    order_price: float
    limit: float
    freight: float
    orders: tuple


@dataclass(frozen=True, slots=True)
class SupplierEvaluation:
    unit_cost: float
    price: float
    capacity: float
    available: bool


@dataclass(frozen=True, slots=True)
class RequiredMarketKeys:
    customer: tuple
    offers: tuple
    history: tuple


@dataclass(frozen=True, slots=True)
class EvaluationIndex:
    history: MappingProxyType
    customer_rows: MappingProxyType
    customer_leaders: MappingProxyType
    demand_groups: MappingProxyType
    offer_curves: MappingProxyType
    supplier_economics: MappingProxyType


@dataclass(frozen=True, slots=True)
class MarketInvestmentSnapshot:
    month: int
    minimum_capacity: float
    investment_payback_months: float
    project_stall_months: int
    startup_cash: float
    wage: float
    consumption_tax: float
    overhead_rate: float
    transport_rate: float
    construction: tuple
    consumer_goods: frozenset
    recipes: MappingProxyType
    investors: MappingProxyType
    offers: MappingProxyType
    orders: MappingProxyType
    suppliers: MappingProxyType
    projects: MappingProxyType
    history: tuple
    history_months: int
    labor: MappingProxyType
    resource_used: MappingProxyType
    resource_limits: MappingProxyType
    recovery_quotes: MappingProxyType
    distances: MappingProxyType
    candidates: tuple
    investor_ids: tuple
    evaluation_index: EvaluationIndex | None = None


InvestmentSnapshot = MarketInvestmentSnapshot


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: InvestmentCandidate
    accepted: bool
    reason: str
    score: float
    project: dict | None = None


def _location(candidate):
    return dict(candidate.location)


def _freight(snapshot, source, target, good):
    distance = snapshot.distances.get((source, target))
    if distance is None:
        return None
    return distance*snapshot.recipes[good].weight*snapshot.transport_rate


def _offers(snapshot, good, buyer_id, buyer_tile):
    index = snapshot.evaluation_index
    if index is not None:
        return [(price, offer) for price, offer in index.offer_curves[good, buyer_tile]
                if offer.participant != buyer_id]
    result = []
    for offer in snapshot.offers.get(good, ()):
        if offer.participant == buyer_id or offer.remaining <= EPS:
            continue
        freight = _freight(snapshot, offer.tile_id, buyer_tile, good)
        if freight is not None:
            result.append((offer.price+freight, offer))
    result.sort(key=lambda row: (row[0], row[1].id))
    return result


def _market_signal(snapshot, state_id, province_id, good):
    index = snapshot.evaluation_index
    if index is not None:
        values = index.history[state_id, province_id, good]
        volume, unmet, price, landed, limit = values
        return dict(volume=volume, unmet=unmet, price=price, landed_price=landed, limit_price=limit)
    rows = [row for row in snapshot.history if row.state_id == state_id
            and row.province_id == province_id and row.good == good]
    if not rows:
        return dict(volume=0.0, unmet=0.0, price=None, landed_price=None, limit_price=None)
    volume = sum(row.volume for row in rows)
    requested = sum(row.requested for row in rows)
    count = snapshot.history_months
    return dict(volume=volume/count,
        unmet=sum(row.unmet for row in rows)/count,
        price=sum(row.trade_gold for row in rows)/volume if volume else None,
        landed_price=sum(row.trade_gold+row.freight for row in rows)/volume if volume else None,
        limit_price=sum(row.limit_value for row in rows)/requested if requested else None)


def _reference_price(snapshot, good, buyer_id, buyer_tile, state_id, province_id):
    offers = _offers(snapshot, good, buyer_id, buyer_tile)
    if offers:
        return offers[0][0]
    signal = _market_signal(snapshot, state_id, province_id, good)
    return signal['landed_price'] or snapshot.recipes[good].price


def _quoted_cost(snapshot, good, quantity, buyer_id, buyer_tile, state_id, province_id):
    left, value, ceiling = quantity, 0.0, 0.0
    for price, offer in _offers(snapshot, good, buyer_id, buyer_tile):
        amount = min(left, offer.remaining)
        value += amount*price
        ceiling = max(ceiling, price)
        left -= amount
        if left <= EPS:
            break
    if left > EPS:
        reference = _reference_price(snapshot, good, buyer_id, buyer_tile, state_id, province_id)
        quotes = []
        for firm_id, tile_id, price in snapshot.recovery_quotes.get(good, ()):
            if firm_id == buyer_id:
                continue
            freight = _freight(snapshot, tile_id, buyer_tile, good)
            if freight is not None:
                quotes.append(price+freight)
        if quotes:
            reference = max(reference, min(quotes))
        value += left*reference
        ceiling = max(ceiling, reference)
    return value, max(ceiling, EPS), left <= EPS


def _unit_cost(snapshot, good, buyer_id, buyer_tile, state_id, province_id):
    recipe = snapshot.recipes[good]
    raw = sum(ratio*_reference_price(snapshot, input_good, buyer_id, buyer_tile, state_id, province_id)
              for input_good, ratio in recipe.inputs)
    wear = sum(ratio*_reference_price(snapshot, equipment, buyer_id, buyer_tile, state_id, province_id)
               for equipment, ratio in recipe.wear)
    return recipe.labor*snapshot.wage+raw+wear


def _customer_rows(snapshot, tile_id, good):
    index = snapshot.evaluation_index
    if index is not None:
        return index.customer_rows[good, tile_id]
    rows = []
    for order in snapshot.orders.get(good, ()):
        freight = _freight(snapshot, tile_id, order.tile_id, good)
        if freight is not None and order.remaining > EPS:
            rows.append((order, freight, order.price-freight))
    return rows


def _net_price(snapshot, good, price, tile_id):
    quantity, final_quantity = 0.0, 0.0
    for order, freight, limit in _customer_rows(snapshot, tile_id, good):
        if limit+EPS < price:
            continue
        volume = min(order.remaining, order.reserved/max(price+freight, EPS))
        quantity += volume
        if order.final_consumption:
            final_quantity += volume
    share = final_quantity/quantity if quantity > EPS else float(good in snapshot.consumer_goods)
    return price*(1-snapshot.consumption_tax*share)


def _customer_limit(snapshot, candidate, tile_id, good):
    index = snapshot.evaluation_index
    if index is not None:
        leaders = index.customer_leaders[good, tile_id]
    else:
        by_participant = {}
        for order, freight, limit in _customer_rows(snapshot, tile_id, good):
            # Предел задаёт конкретная заявка покупателя, а не цена кандидата.
            limit = order.price-freight
            by_participant[order.participant] = max(
                limit, by_participant.get(order.participant, float('-inf')))
        leaders = sorted(((limit, participant) for participant, limit in by_participant.items()),
                         reverse=True)[:2]
    return next((limit for limit, participant in leaders
                 if participant != candidate.investor_id and limit > 0), None)


def _demand_groups(snapshot, good, tile_id, price):
    index = snapshot.evaluation_index
    if index is not None:
        result = []
        for group in index.demand_groups[good, tile_id]:
            if group.limit+EPS < price:
                continue
            volume = sum(min(remaining, reserved/max(price+group.freight, EPS))
                         for remaining, reserved in group.orders)
            result.append((group.participant, group.tile_id, group.order_price,
                           group.limit, group.freight, volume))
        return tuple(result)
    grouped = {}
    for order, freight, limit in _customer_rows(snapshot, tile_id, good):
        if limit+EPS < price:
            continue
        key = (order.participant, order.tile_id, order.price)
        row = grouped.get(key)
        if row is None:
            row = grouped[key] = [order.participant, order.tile_id, order.price,
                                  limit, freight, 0.0]
        row[5] += min(order.remaining, order.reserved/max(price+freight, EPS))
    return tuple(tuple(row) for row in grouped.values())


def _investment_demand(snapshot, candidate, price):
    good = candidate.good
    tile_id = _location(candidate)['tile_id']
    suppliers = []
    for firm in snapshot.suppliers.get(good, ()):
        economics = snapshot.evaluation_index.supplier_economics[firm.id]
        if not economics.available:
            continue
        suppliers.append([firm.tile_id, economics.price, economics.capacity])
    suppliers.extend([list(project) for project in snapshot.projects.get(good, ())])
    cheap = {offer.id: offer.remaining for offer in snapshot.offers.get(good, ())}
    residual = 0.0
    for participant, order_tile, order_price, limit, freight, volume in _demand_groups(
            snapshot, good, tile_id, price):
        if participant == candidate.investor_id:
            continue
        for offer_price, offer in _offers(snapshot, good, participant, order_tile):
            if offer_price > order_price+EPS:
                break
            covered = min(volume, cheap[offer.id])
            cheap[offer.id] -= covered
            volume -= covered
        for supply in suppliers:
            if volume <= EPS:
                break
            if supply[2] <= EPS:
                continue
            freight_supply = _freight(snapshot, supply[0], order_tile, good)
            if freight_supply is not None and supply[1]+freight_supply <= order_price+EPS:
                covered = min(volume, supply[2])
                supply[2] -= covered
                volume -= covered
        residual += volume
    investor = snapshot.investors[candidate.investor_id]
    return max(0.0, residual-dict(investor.stock).get(good, 0.0))


def _chain_requirements(snapshot, good, quantity):
    result = {}
    stack = [(good, quantity)]
    while stack:
        product, amount = stack.pop()
        for input_good, ratio in snapshot.recipes[product].inputs:
            required = amount*ratio
            result[input_good] = result.get(input_good, 0.0)+required
            stack.append((input_good, required))
    return result


def evaluate(snapshot, candidate):
    """Полная чистая оценка кандидата по неизменяемым рыночным данным."""
    location = _location(candidate)
    tile_id = location['tile_id']
    investor = snapshot.investors[candidate.investor_id]
    recipe = snapshot.recipes[candidate.good]
    if candidate.expansion:
        firm = next(row for row in snapshot.suppliers[candidate.good]
                    if row.id == candidate.investor_id)
        initial_price = firm.price
    else:
        initial_price = recipe.price
    limit = _customer_limit(snapshot, candidate, tile_id, candidate.good)
    signal = _market_signal(snapshot, investor.state_id, candidate.province_id, candidate.good)
    price = limit if limit is not None else signal['price'] or recipe.price
    demand = _investment_demand(snapshot, candidate, price)
    if candidate.derived_demand is not None:
        demand = max(demand, candidate.derived_demand)
    if demand < snapshot.minimum_capacity:
        return CandidateEvaluation(candidate, False, 'Недостаточный спрос', 0.0)
    capacity = min(demand, candidate.derived_demand) \
        if candidate.derived_demand is not None else demand
    labor = snapshot.labor.get((location['state_id'], candidate.province_id), 0.0)
    capacity = min(capacity, labor/recipe.labor)
    if recipe.resource:
        key = (candidate.province_id, recipe.resource)
        available = snapshot.resource_limits.get(key, 0.0)-snapshot.resource_used.get(key, 0.0)
        capacity = min(capacity, max(0.0, available))
    if capacity < snapshot.minimum_capacity:
        return CandidateEvaluation(candidate, False, 'Недостаточны труд или ресурс', 0.0)
    unit = _unit_cost(snapshot, candidate.good, investor.id, tile_id,
                      investor.state_id, candidate.province_id)
    fixed_unit = recipe.labor*snapshot.wage*snapshot.overhead_rate
    margin = _net_price(snapshot, candidate.good, price, tile_id)-unit-fixed_unit
    if margin <= EPS:
        return CandidateEvaluation(candidate, False, 'Неположительная маржа', 0.0)
    distance = snapshot.distances.get((investor.tile_id, tile_id))
    if distance is None:
        return CandidateEvaluation(candidate, False, 'Нет маршрута', 0.0)
    material = sum(quantity*_quoted_cost(snapshot, good, quantity, investor.id,
        investor.tile_id, investor.state_id, investor.province_id)[0]/max(quantity, EPS)
        for good, quantity in snapshot.construction)
    delivery = (sum(quantity*snapshot.recipes[good].weight
                    for good, quantity in snapshot.construction)*distance*snapshot.transport_rate)
    equipment_capital = sum(ratio*_quoted_cost(snapshot, equipment, ratio, investor.id,
        investor.tile_id, investor.state_id, investor.province_id)[1]
        for equipment, ratio in recipe.equipment)
    startup = 0.0 if candidate.expansion else snapshot.startup_cash
    capacity, working, investment, profit = project_finances(
        capacity, investor.available_cash, startup, material, delivery, unit,
        fixed_unit, equipment_capital, margin, candidate.expansion)
    if capacity < snapshot.minimum_capacity:
        return CandidateEvaluation(candidate, False, 'Недостаточно денег для минимальной мощности', 0.0)
    horizon = snapshot.investment_payback_months*(0.5+investor.risk_tolerance)
    if investment > investor.available_cash+EPS or profit*horizon < investment:
        return CandidateEvaluation(candidate, False, 'Окупаемость не проходит', 0.0)
    project_id = f'{candidate.investor_id}:{candidate.good}:{candidate.province_id}:{snapshot.month}'
    project = dict(good=candidate.good, capacity=capacity, location=location,
        materials={}, working_cash=working, expected_price=price,
        expected_profit=profit, expected_investment=investment,
        expected_unit_cost=unit+fixed_unit, expansion=candidate.expansion,
        created=snapshot.month, chain_requirements=_chain_requirements(snapshot, candidate.good, capacity),
        project_id=project_id, parent_project_id=candidate.parent_project_id,
        derived_demand=candidate.derived_demand,
        dependency_expires=snapshot.month+snapshot.project_stall_months
            if candidate.parent_project_id else None)
    score = profit*snapshot.investment_payback_months-investment
    return CandidateEvaluation(candidate, True, 'Кандидат принят к применению', score, project)


_WORKER_SNAPSHOT = None


def _spawn_snapshot(snapshot):
    """Снять mappingproxy перед передачей неизменяемого снимка в spawn."""
    mappings = {name: dict(getattr(snapshot, name)) for name in (
        'recipes', 'investors', 'offers', 'orders', 'suppliers', 'projects', 'labor',
        'resource_used', 'resource_limits', 'recovery_quotes', 'distances')}
    index = snapshot.evaluation_index
    if index is not None:
        mappings['evaluation_index'] = replace(index, **{
            name: dict(getattr(index, name)) for name in (
                'history', 'customer_rows', 'customer_leaders', 'demand_groups', 'offer_curves',
                'supplier_economics')})
    return replace(snapshot, **mappings)


def _init_evaluation_worker(snapshot):
    global _WORKER_SNAPSHOT
    mappings = {name: MappingProxyType(getattr(snapshot, name)) for name in (
        'recipes', 'investors', 'offers', 'orders', 'suppliers', 'projects', 'labor',
        'resource_used', 'resource_limits', 'recovery_quotes', 'distances')}
    index = snapshot.evaluation_index
    if index is not None:
        mappings['evaluation_index'] = replace(index, **{
            name: MappingProxyType(getattr(index, name)) for name in (
                'history', 'customer_rows', 'customer_leaders', 'demand_groups', 'offer_curves',
                'supplier_economics')})
    _WORKER_SNAPSHOT = replace(snapshot, **mappings)


def _evaluate_chunk(candidates):
    return tuple(evaluate(_WORKER_SNAPSHOT, candidate) for candidate in candidates)


def _evaluate_snapshot_task(snapshot):
    """Полностью обработать один снимок в постоянном worker."""
    _init_evaluation_worker(snapshot)
    candidates = tuple(sorted(_WORKER_SNAPSHOT.candidates, key=lambda row: row.key))
    return _WORKER_SNAPSHOT.month, evaluate_all(_WORKER_SNAPSHOT, candidates)


def evaluate_all(snapshot, candidates=None, workers=1):
    rows = snapshot.candidates if candidates is None else tuple(candidates)
    if workers < 1:
        raise ValueError('workers должен быть положительным')
    if workers == 1 or not rows:
        return tuple(evaluate(snapshot, candidate) for candidate in rows)
    chunk_size = (len(rows)+workers-1)//workers
    chunks = tuple(rows[start:start+chunk_size] for start in range(0, len(rows), chunk_size))
    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=workers, mp_context=context,
            initializer=_init_evaluation_worker, initargs=(_spawn_snapshot(snapshot),)) as pool:
        results = {evaluation.candidate.key: evaluation
                   for chunk in pool.map(_evaluate_chunk, chunks) for evaluation in chunk}
    return tuple(results[candidate.key] for candidate in rows)


def _required_market_keys(snapshot):
    """Перечислить все рынки, к которым может обратиться чистая оценка."""
    customer = set()
    offers = set()
    history = set()

    def add_unit_markets(good, tile_id, state_id, province_id):
        recipe = snapshot.recipes[good]
        for input_good, _ratio in recipe.inputs+recipe.wear:
            offers.add((input_good, tile_id))
            history.add((state_id, province_id, input_good))

    for candidate in snapshot.candidates:
        tile_id = _location(candidate)['tile_id']
        investor = snapshot.investors[candidate.investor_id]
        customer.add((candidate.good, tile_id))
        history.add((investor.state_id, candidate.province_id, candidate.good))
        add_unit_markets(candidate.good, tile_id, investor.state_id, candidate.province_id)
        for good, _quantity in snapshot.construction:
            offers.add((good, investor.tile_id))
            history.add((investor.state_id, investor.province_id, good))
        for equipment, _ratio in snapshot.recipes[candidate.good].equipment:
            offers.add((equipment, investor.tile_id))
            history.add((investor.state_id, investor.province_id, equipment))
    for firms in snapshot.suppliers.values():
        for firm in firms:
            customer.add((firm.good, firm.tile_id))
            add_unit_markets(firm.good, firm.tile_id, firm.state_id, firm.province_id)
    for good, orders in snapshot.orders.items():
        offers.update((good, order.tile_id) for order in orders)
    history_keys = tuple(sorted(history, key=lambda key: tuple('' if value is None else value
                                                               for value in key)))
    return RequiredMarketKeys(tuple(sorted(customer)), tuple(sorted(offers)), history_keys)


def _build_evaluation_index(snapshot, required=None):
    """Построить только lookup-структуры, необходимые чистой оценке."""
    required = _required_market_keys(snapshot) if required is None else required
    history_rows = {}
    for row in snapshot.history:
        key = (row.state_id, row.province_id, row.good)
        history_rows.setdefault(key, []).append(row)
    history_values = {}
    for key in required.history:
        rows = history_rows.get(key, ())
        if not rows:
            history_values[key] = (0.0, 0.0, None, None, None)
            continue
        volume = sum(row.volume for row in rows)
        requested = sum(row.requested for row in rows)
        count = snapshot.history_months
        history_values[key] = (volume/count,
            sum(row.unmet for row in rows)/count,
            sum(row.trade_gold for row in rows)/volume if volume else None,
            sum(row.trade_gold+row.freight for row in rows)/volume if volume else None,
            sum(row.limit_value for row in rows)/requested if requested else None)
    customer_rows = {(good, tile): _customer_rows(snapshot, tile, good)
                     for good, tile in required.customer}
    customer_leaders = {}
    for key, rows in customer_rows.items():
        by_participant = {}
        for order, freight, limit in rows:
            limit = order.price-freight
            by_participant[order.participant] = max(
                limit, by_participant.get(order.participant, float('-inf')))
        customer_leaders[key] = tuple(sorted(
            ((limit, participant) for participant, limit in by_participant.items()), reverse=True)[:2])
    demand_groups = {}
    for key, rows in customer_rows.items():
        grouped = {}
        for order, freight, limit in rows:
            group_key = (order.participant, order.tile_id, order.price)
            if group_key not in grouped:
                grouped[group_key] = [order.participant, order.tile_id, order.price,
                                      limit, freight, []]
            grouped[group_key][5].append((order.remaining, order.reserved))
        demand_groups[key] = tuple(DemandGroupSnapshot(*row[:5], tuple(row[5]))
                                   for row in grouped.values())
    offer_curves = {(good, tile): _offers(snapshot, good, '', tile)
                    for good, tile in required.offers}
    partial = EvaluationIndex(MappingProxyType(history_values), MappingProxyType(customer_rows),
        MappingProxyType(customer_leaders), MappingProxyType(demand_groups),
        MappingProxyType(offer_curves), MappingProxyType({}))
    indexed = replace(snapshot, evaluation_index=partial)
    economics = {}
    for good, firms in indexed.suppliers.items():
        for firm in firms:
            cost = _unit_cost(indexed, good, firm.id, firm.tile_id, firm.state_id, firm.province_id)
            price = firm.price
            if _net_price(indexed, good, price, firm.tile_id)+EPS < cost:
                price = firm.production_price
            viable = _net_price(indexed, good, price, firm.tile_id)+EPS >= cost
            capacity = min(firm.capacity, firm.labor_available/indexed.recipes[good].labor)
            if firm.resource_limit is not None:
                capacity = min(capacity, firm.resource_limit)
            economics[firm.id] = SupplierEvaluation(cost, price, capacity,
                viable and firm.funds > EPS)
    return replace(partial, supplier_economics=MappingProxyType(economics))


def _required_route_pairs(goods, offer_rows, order_rows, candidates, supplier_rows,
                          investor_rows, construction, recipes, recovery):
    """Построить необходимые маршруты через уникальные площадки каждого товара."""
    offer_tiles = {good: {offer.tile_id for offer in offer_rows[good]} for good in goods}
    order_tiles = {good: {order.tile_id for order in order_rows[good]} for good in goods}
    candidate_tiles = {good: set() for good in goods}
    supplier_tiles = {good: {firm.tile_id for firm in supplier_rows[good]} for good in goods}
    investor_tiles = set()
    equipment_investors = {good: set() for good in goods}
    route_pairs = set()
    for candidate in candidates:
        tile_id = _location(candidate)['tile_id']
        investor_tile = investor_rows[candidate.investor_id].tile_id
        candidate_tiles[candidate.good].add(tile_id)
        investor_tiles.add(investor_tile)
        route_pairs.add((investor_tile, tile_id))
        for equipment, _ratio in recipes[candidate.good].equipment:
            equipment_investors[equipment].add(investor_tile)
    for good in goods:
        route_pairs.update((source, target) for source in offer_tiles[good]
                           for target in order_tiles[good])
        route_pairs.update((source, target) for source in candidate_tiles[good]
                           for target in order_tiles[good])
        route_pairs.update((source, target) for source in offer_tiles[good]
                           for target in candidate_tiles[good])
        route_pairs.update((source, target) for source in supplier_tiles[good]
                           for target in order_tiles[good])
        route_pairs.update((source, target) for source in offer_tiles[good]
                           for target in supplier_tiles[good])
    for good, _quantity in construction:
        route_pairs.update((source, target) for source in offer_tiles[good]
                           for target in investor_tiles)
    for good, targets in equipment_investors.items():
        route_pairs.update((source, target) for source in offer_tiles[good] for target in targets)
    recovery_sources = {source for rows in recovery.values() for _firm, source, _price in rows}
    all_candidate_tiles = set().union(*candidate_tiles.values())
    route_pairs.update((source, target) for source in recovery_sources
                       for target in all_candidate_tiles)
    return route_pairs


def build_snapshot(engine, sells, allowed_goods=None, timings=None):
    """Собрать общие данные; кандидатная экономика выполняется только в evaluate."""
    capture_started = time.perf_counter()
    investors = sorted((a for a in engine.accounts.values()
        if a['kind'] in ('private', 'merchant', 'treasury', 'municipal')
        or (a['kind'] == 'enterprise'
            and engine.enterprises[a['id']]['status'] != 'closed'
            and engine.enterprises[a['id']]['available_from'] <= engine.meta['month'])),
        key=lambda a: a['id'])
    for investor in investors:
        engine.profile(investor)
    index = engine.build_investment_state()['index']
    candidates = []
    for investor in investors:
        markets = set(index['markets'].get(investor['state_id'], ()))
        expansion = engine.enterprises.get(investor['id'])
        for province_id, good in sorted(markets):
            if allowed_goods is not None and good not in allowed_goods:
                continue
            if expansion and (good != expansion['good'] or province_id != expansion['province_id']):
                continue
            location = ({key: expansion[key]
                         for key in ('state_id', 'province_id', 'city_id', 'tile_id')}
                        if expansion else engine.location(province_id))
            if location['state_id'] != investor['state_id']:
                continue
            recipe = engine.goods[good]
            if recipe['resource']:
                tiles = index['resource_tiles'].get(
                    (investor['state_id'], province_id, recipe['resource']), [])
                if not tiles:
                    continue
                location.update(tile_id=expansion['tile_id'] if expansion
                                else engine.shuffled(tiles)[0]['id'], city_id=None)
            key = f'{investor["id"]}:{good}:{province_id}'
            candidates.append(InvestmentCandidate(key, investor['id'], good, province_id,
                tuple(sorted(location.items())), bool(expansion)))
    recipes = {good: RecipeSnapshot(tuple(sorted(recipe['inputs'].items())),
        tuple(sorted(recipe.get('equipment', {}).items())),
        tuple(sorted((equipment, ratio*engine.goods[equipment].get('durable', {}).get('wear_rate', 0))
                     for equipment, ratio in recipe.get('equipment', {}).items())),
        recipe['labor'], recipe['resource'], recipe['price'], recipe['weight'])
        for good, recipe in engine.goods.items()}
    investor_rows = {a['id']: InvestorSnapshot(a['id'], a['state_id'], a['province_id'],
        engine.market_tile(a), engine.free_cash(a['id']), tuple(sorted(a['stock'].items())),
        a['risk_tolerance']) for a in investors}
    offer_rows = {good: tuple(OfferSnapshot(o['id'], o['participant'], o['tile_id'],
        o['price'], o['remaining']) for o in sells.get(good, ())
        if o['remaining'] > EPS and o['status'] not in ('cancelled', 'expired'))
        for good in engine.goods}
    order_rows = {good: tuple(OrderSnapshot(o['participant'], o['tile_id'], o['price'],
        o['remaining'], o['reserved'], o.get('final_consumption', False))
        for o in engine.report['orders'] if o['side'] == 'buy' and o['good'] == good
        and o['remaining'] > EPS and o['status'] not in ('cancelled', 'expired'))
        for good in engine.goods}
    supplier_rows = {good: [] for good in engine.goods}
    for firm in engine.enterprises.values():
        if firm['status'] == 'closed' or firm.get('suspended'):
            continue
        recipe = engine.goods[firm['good']]
        owner = engine.accounts[firm['owner_id']]
        funds = engine.free_cash(firm['id'])+engine.cash_after_commitments(
            owner['id'], engine.project_cash_reserve(owner['id'], sells))
        labor = sum(engine.population(a)*engine.cfg['labor_share'] for a in engine.workforce(firm))
        resource_limit = (engine.resource_limits.get((firm['province_id'], recipe['resource']), 0.0)
                          if recipe['resource'] else None)
        supplier_rows[firm['good']].append(SupplierSnapshot(firm['id'], firm['good'],
            firm['tile_id'], firm['state_id'], firm['province_id'], firm['price'],
            firm.get('production_price', firm['price']), firm['capacity'], funds,
            labor, resource_limit))
    supplier_rows = {good: tuple(rows) for good, rows in supplier_rows.items()}
    project_rows = {good: tuple(ProjectSupply(location['tile_id'], price, capacity)
                    for location, price, capacity in index['projects'].get(good, ()))
                    for good in engine.goods}
    history = tuple(HistoryRow(row['state_id'], row['province_id'], row['good'],
        row['requested'], row['volume'], row['trade_gold'], row['freight'],
        row['limit_value'], row['unmet_solvent'])
        for month in engine.meta.get('market_history', []) for row in month['markets'])
    recovery = {good: tuple((firm['id'], firm['tile_id'], price)
        for firm, price in getattr(engine, '_recovery_quotes', {}).get(good, ()))
        for good in engine.goods}

    route_pairs = _required_route_pairs(engine.goods, offer_rows, order_rows, candidates,
        supplier_rows, investor_rows, engine.cfg['construction'].items(), recipes, recovery)
    distances = {}
    for source, target in sorted(route_pairs):
        distance, _path = engine.path(source, target)
        if distance is not None:
            distances[source, target] = distance
    snapshot = MarketInvestmentSnapshot(
        month=engine.meta['month'], minimum_capacity=engine.cfg['minimum_capacity'],
        investment_payback_months=engine.cfg['investment_payback_months'],
        project_stall_months=engine.cfg['project_stall_months'],
        startup_cash=engine.cfg['startup_cash'], wage=engine.cfg['wage'],
        consumption_tax=engine.cfg['consumption_tax'],
        overhead_rate=engine.cfg.get('enterprise_overhead_rate', 0),
        transport_rate=engine.cfg['transport_rate'],
        construction=tuple(sorted(engine.cfg['construction'].items())),
        consumer_goods=frozenset(engine.cfg['basic_needs'] | engine.cfg['extra_needs']),
        recipes=MappingProxyType(recipes), investors=MappingProxyType(investor_rows),
        offers=MappingProxyType(offer_rows), orders=MappingProxyType(order_rows),
        suppliers=MappingProxyType(supplier_rows), projects=MappingProxyType(project_rows),
        history=history, history_months=len(engine.meta.get('market_history', [])),
        labor=MappingProxyType(dict(index['labor'])),
        resource_used=MappingProxyType(dict(index['resource_used'])),
        resource_limits=MappingProxyType(dict(engine.resource_limits)),
        recovery_quotes=MappingProxyType(recovery), distances=MappingProxyType(distances),
        candidates=tuple(candidates), investor_ids=tuple(a['id'] for a in investors))
    if timings is not None:
        timings['snapshot_capture'] = time.perf_counter()-capture_started
    started = time.perf_counter()
    snapshot = replace(snapshot, evaluation_index=_build_evaluation_index(snapshot))
    if timings is not None:
        timings['evaluation_index'] = time.perf_counter()-started
    return snapshot


def _set_build_caches(engine, state):
    engine._investment_state = state
    engine._investment_supply_cache = state['supply_cache']
    engine._investment_index = state['index']
    engine._investment_demand_cache = state['demand_cache']
    engine._investment_input_reserved = state['input_reserved']
    engine._upstream_candidate_cache = state['upstream_candidates']


def _clear_build_caches(engine):
    for name in ('_investment_supply_cache', '_investment_index', '_investment_demand_cache',
                 '_upstream_candidate_cache', '_investment_input_reserved', '_investment_state',
                 '_investment_customer_limits'):
        engine.__dict__.pop(name, None)


def _reject(engine, evaluation, reason):
    project = evaluation.project or {}
    engine.report['investment_decisions'].append(dict(
        kind='investment_decision', investor=evaluation.candidate.investor_id,
        outcome='rejected', good=evaluation.candidate.good,
        capacity=project.get('capacity', 0.0), reason=reason,
        project_id=project.get('project_id')))


def _resume_queue(engine, sells):
    queue = engine.meta.setdefault('investment_queue', [])
    waiting = []
    for item in queue:
        investor = engine.accounts.get(item.get('investor'))
        project = item.get('project')
        if not investor or investor.get('project') or not project:
            continue
        ready = all(project['capacity']*ratio <=
            sum(sell['remaining'] for _price, sell, _quote in engine.offers(investor, good, sells))+EPS
            for good, ratio in engine.goods[project['good']]['inputs'].items())
        if ready:
            investor['project'] = project
            engine.register_investment_project(project)
            engine.report['investment_decisions'].append(dict(
                kind='investment_decision', investor=investor['id'], outcome='resumed',
                **copy.deepcopy(project)))
        else:
            waiting.append(item)
    queue[:] = waiting


def commit(engine, evaluations, sells, flow_balances=None, stale=False):
    """Канонически применить оценки с повторной проверкой живых ограничений."""
    state = engine.build_investment_state()
    _set_build_caches(engine, state)
    input_reserved = state['input_reserved']
    flow_reserved = state['flow_reserved']
    try:
        _resume_queue(engine, sells)
        for evaluation in sorted(evaluations, key=lambda row: (
                row.candidate.investor_id, row.candidate.key)):
            if not evaluation.accepted:
                continue
            candidate = evaluation.candidate
            if stale:
                _reject(engine, evaluation, 'Устаревший инвестиционный снимок')
                continue
            account = engine.accounts[candidate.investor_id]
            if account.get('project'):
                _reject(engine, evaluation, 'Владелец уже имеет проект после раннего commit')
                continue
            project = copy.deepcopy(evaluation.project)
            available_cash = engine.free_cash(account['id'])
            if project['expected_investment'] > available_cash+EPS:
                _reject(engine, evaluation, 'Свободные деньги уже заняты ранним commit')
                continue
            recipe = engine.goods[project['good']]
            location = project['location']
            labor_key = (location['state_id'], location['province_id'])
            labor = sum(engine.population(a)*engine.cfg['labor_share']
                        for a in engine.accounts.values() if a['kind'] == 'household'
                        and a['state_id'] == labor_key[0] and a['province_id'] == labor_key[1])
            labor -= state['labor_reserved'][labor_key]
            if project['capacity']*recipe['labor'] > labor+EPS:
                _reject(engine, evaluation, 'Труд уже зарезервирован ранним commit')
                continue
            if recipe['resource']:
                resource_key = (location['province_id'], recipe['resource'])
                used = sum(firm['capacity'] for firm in engine.enterprises.values()
                    if firm['status'] != 'closed' and firm['province_id'] == resource_key[0]
                    and engine.goods[firm['good']]['resource'] == recipe['resource'])
                used += state['resource_reserved'][resource_key]
                available_resource = engine.resource_limits.get(resource_key, 0.0)-used
                if project['capacity'] > available_resource+EPS:
                    _reject(engine, evaluation, 'Ресурс уже зарезервирован ранним commit')
                    continue
            flow_candidate = dict(state_id=account['state_id'], good=project['good'],
                quantity=project['capacity'], project_id=project['project_id'])
            sustainable, flow_trial = engine.sustainable_candidate_capacity(
                flow_candidate, flow_reserved, flow_balances)
            if sustainable+EPS < project['capacity']:
                if sustainable < engine.cfg['minimum_capacity']:
                    _reject(engine, evaluation, 'Устойчивый поток уже занят ранним commit')
                    continue
                engine.limit_project_capacity(project, sustainable)
            available_inputs = {}
            feasible = True
            for input_good, ratio in recipe['inputs'].items():
                required = project['capacity']*ratio
                available = sum(sell['remaining'] for _price, sell, _quote
                                in engine.offers(account, input_good, sells))
                available_inputs[input_good] = available
                if required > available-input_reserved[input_good]+EPS:
                    _reject(engine, evaluation, 'Сырьё уже зарезервировано ранним commit')
                    feasible = False
                    break
            if not feasible:
                continue
            evidence = {}
            for input_good, ratio in recipe['inputs'].items():
                required = project['capacity']*ratio
                available = available_inputs[input_good]
                signal = engine.market_signal(engine.meta.get('market_history', []),
                    account['state_id'], location['province_id'], input_good)
                evidence[input_good] = dict(required=round(required, 6),
                    available=round(available, 6), shortage=round(max(0, required-available), 6),
                    historical_unmet=round(signal['unmet'], 6),
                    historical_volume=round(signal['volume'], 6))
            project['input_evidence'] = evidence
            account['project'] = project
            state['flow_reserved'] = flow_reserved = flow_trial
            for input_good, ratio in recipe['inputs'].items():
                input_reserved[input_good] += project['capacity']*ratio
            engine.register_investment_project(project)
            engine.report['investment_decisions'].append(dict(
                kind='investment_decision', investor=account['id'], outcome='started',
                **copy.deepcopy(project)))
    finally:
        _clear_build_caches(engine)


class AsyncInvestmentPlanner:
    """Один active future и не более одного свежего pending snapshot."""

    def __init__(self, executor=None):
        self.executor = executor or ProcessPoolExecutor(max_workers=1,
            mp_context=multiprocessing.get_context('spawn'))
        self._future = None
        self._pending = None

    def _start_pending(self):
        if self._future is not None or self._pending is None:
            return
        snapshot, self._pending = self._pending, None
        try:
            self._future = self.executor.submit(_evaluate_snapshot_task, _spawn_snapshot(snapshot))
        except Exception as error:
            self._future = Future()
            self._future.set_exception(error)

    def submit(self, snapshot):
        self._pending = snapshot
        self._start_pending()

    def poll(self, revision):
        """Забрать только готовый результат; незавершённый worker не ждать."""
        self._start_pending()
        if self._future is None:
            return ('pending' if self._pending is not None else 'skipped'), None, None
        if not self._future.done():
            return 'pending', None, None
        future, self._future = self._future, None
        try:
            result_revision, evaluations = future.result()
        except Exception as error:
            self._start_pending()
            return 'failed', None, f'{type(error).__name__}: {error}'
        self._start_pending()
        status = 'accepted' if result_revision == revision else 'stale'
        return status, evaluations, None

    def close(self):
        self.executor.shutdown(wait=True)


def _async_planner(engine):
    planner = getattr(engine, '_async_investment_planner', None)
    if planner is None:
        planner = engine._async_investment_planner = AsyncInvestmentPlanner()
    return planner


def run_async_snapshot_batch(engine, sells):
    """Применить готовый обзор прошлого месяца без ожидания worker."""
    planner = _async_planner(engine)
    status, evaluations, error = planner.poll(engine.meta['month']-1)
    engine.report['investment_async'] = status
    engine.report['investment_review'] = dict(mode='async', revision=engine.meta['month']-1)
    if error is not None:
        engine.report['diagnostics'].append(dict(kind='investment_async_error', error=error))
    evaluations = evaluations or ()
    best = {}
    for evaluation in evaluations:
        current = best.get(evaluation.candidate.investor_id)
        if evaluation.accepted and (current is None or evaluation.score > current.score):
            best[evaluation.candidate.investor_id] = evaluation
    selected = tuple(best[key] for key in sorted(best))
    for evaluation in selected:
        project = evaluation.project
        engine.report['investment_candidates'].append(dict(
            state_id=engine.accounts[evaluation.candidate.investor_id]['state_id'],
            good=project['good'], quantity=project['capacity'],
            decision_id=project['project_id'], project_id=project['project_id'],
            investor=evaluation.candidate.investor_id, province_id=project['location']['province_id']))
    due = engine.meta['month'] % engine.cfg['investment_interval'] == 0
    flow_balances = None
    if due:
        state_ids = sorted({a['state_id'] for a in engine.accounts.values() if a.get('state_id')})
        flow_balances = engine.investment_flow_balances(state_ids)
    commit(engine, selected, sells, flow_balances, stale=status == 'stale')
    investors = [a for a in engine.accounts.values()
        if a['kind'] in ('private', 'merchant', 'treasury', 'municipal') or a['kind'] == 'enterprise']
    if evaluations:
        engine.report['expansion_intents'] = engine.investment_expansion_intents(
            engine.report['investment_candidates'], flow_balances)
        engine.report['expansion_intent_diagnostics'] = engine.investment_expansion_diagnostics(
            engine.report['expansion_intents'])
        engine.report['investment_candidate_statuses'] = engine.investment_candidate_statuses(
            engine.report['investment_candidates'], engine.report['expansion_intents'],
            engine.report['expansion_intent_diagnostics'])
        engine.report['investment_shadow_gate'] = engine.investment_shadow_gate(
            engine.report['investment_candidate_statuses'], engine.report['investment_decisions'])
        engine.report['investment_candidate_capacity_limits'] =             engine.investment_candidate_capacity_limits(
                engine.report['investment_candidates'], flow_balances)
    engine.project_material_requests(sells, due, investors)
    return build_snapshot(engine, sells)


def schedule_async_investment(engine, snapshot):
    """После завершения месяца передать его независимый снимок в worker."""
    _async_planner(engine).submit(snapshot)


def run_snapshot_batch(engine, sells, workers=1):
    """Новый инвестиционный обзор; материальные закупки остаются общим кодом V3."""
    due = engine.meta['month'] % engine.cfg['investment_interval'] == 0
    dirty_goods = set(getattr(engine, '_investment_dirty_goods',
        engine.meta.get('investment_dirty', [])))
    should_evaluate = due or bool(dirty_goods)
    allowed_goods = None if due else dirty_goods
    if should_evaluate:
        engine._investment_dirty_goods = set()
        if due:
            engine.meta['investment_full_reconcile_month'] = engine.meta['month']
    engine.report['investment_review'] = dict(
        mode='full' if due else 'targeted' if should_evaluate else 'skipped',
        dirty_goods=sorted(dirty_goods), month=engine.meta['month'])

    index_timings = {}
    started = time.perf_counter()
    snapshot = build_snapshot(engine, sells, allowed_goods, index_timings) if should_evaluate else None
    build_seconds = time.perf_counter()-started
    started = time.perf_counter()
    evaluations = evaluate_all(snapshot, workers=workers) if snapshot is not None else ()
    best = {}
    for evaluation in evaluations:
        current = best.get(evaluation.candidate.investor_id)
        if evaluation.accepted and (current is None or evaluation.score > current.score):
            best[evaluation.candidate.investor_id] = evaluation
    selected = tuple(best[key] for key in sorted(best))
    evaluate_seconds = time.perf_counter()-started
    for evaluation in selected:
        project = evaluation.project
        engine.report['investment_candidates'].append(dict(
            state_id=engine.accounts[evaluation.candidate.investor_id]['state_id'],
            good=project['good'], quantity=project['capacity'],
            decision_id=project['project_id'], project_id=project['project_id'],
            investor=evaluation.candidate.investor_id, province_id=project['location']['province_id']))
    flow_balances = None
    if due:
        state_ids = sorted({a['state_id'] for a in engine.accounts.values() if a.get('state_id')})
        flow_balances = engine.investment_flow_balances(state_ids)
    started = time.perf_counter()
    commit(engine, selected, sells, flow_balances)
    commit_seconds = time.perf_counter()-started
    investors = [engine.accounts[investor_id] for investor_id in snapshot.investor_ids] \
        if snapshot is not None else [a for a in engine.accounts.values()
            if a['kind'] in ('private', 'merchant', 'treasury', 'municipal')
            or a['kind'] == 'enterprise']
    if should_evaluate:
        engine.report['expansion_intents'] = engine.investment_expansion_intents(
            engine.report['investment_candidates'], flow_balances)
        engine.report['expansion_intent_diagnostics'] = engine.investment_expansion_diagnostics(
            engine.report['expansion_intents'])
        engine.report['investment_candidate_statuses'] = engine.investment_candidate_statuses(
            engine.report['investment_candidates'], engine.report['expansion_intents'],
            engine.report['expansion_intent_diagnostics'])
        engine.report['investment_shadow_gate'] = engine.investment_shadow_gate(
            engine.report['investment_candidate_statuses'], engine.report['investment_decisions'])
        engine.report['investment_candidate_capacity_limits'] = \
            engine.investment_candidate_capacity_limits(
                engine.report['investment_candidates'], flow_balances)
    engine.project_material_requests(sells, due, investors)
    return dict(investment_snapshot_build=build_seconds,
        investment_snapshot_capture=index_timings.get('snapshot_capture', 0.0),
        investment_evaluation_index=index_timings.get('evaluation_index', 0.0),
        investment_evaluate_all=evaluate_seconds, investment_sequential_commit=commit_seconds)
