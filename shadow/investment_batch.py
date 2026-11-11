"""Независимая оценка инвестиций и последовательное применение решений.

Снимок и кандидаты материализуют все обращения к живому миру и RNG. ``evaluate``
работает только с этими неизменяемыми данными; проекты, резервы и отчёты создаёт
исключительно канонический последовательный ``commit``.
"""
import copy
import time
from dataclasses import dataclass
from types import MappingProxyType

from modules.economy.support import EPS
from modules.economy.v3 import project_finances


@dataclass(frozen=True, slots=True)
class InvestmentCandidate:
    key: str
    investor_id: str
    good: str
    province_id: str
    location: tuple[tuple[str, str], ...]
    expansion: bool
    expected_price: float
    demand: float
    unit_cost: float
    fixed_unit: float
    margin: float
    material: float
    delivery: float
    equipment_capital: float
    available_cash: float
    startup_cash: float
    risk_tolerance: float
    labor_available: float
    resource_available: float | None
    chain_ratios: tuple[tuple[str, float], ...]
    parent_project_id: str | None = None
    derived_demand: float | None = None


@dataclass(frozen=True, slots=True)
class InvestmentSnapshot:
    month: int
    minimum_capacity: float
    investment_payback_months: float
    project_stall_months: int
    labor_per_output: MappingProxyType
    candidates: tuple[InvestmentCandidate, ...]
    investor_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: InvestmentCandidate
    accepted: bool
    reason: str
    score: float
    project: dict | None = None


def _location(candidate):
    return dict(candidate.location)


def evaluate(snapshot, candidate):
    """Чистая оценка: без engine, RNG, заявок, резервов и изменений снимка."""
    demand = candidate.demand
    if candidate.derived_demand is not None:
        demand = max(demand, candidate.derived_demand)
    if demand < snapshot.minimum_capacity:
        return CandidateEvaluation(candidate, False, 'Недостаточный спрос', 0.0)
    capacity = min(demand, candidate.derived_demand) \
        if candidate.derived_demand is not None else demand
    labor_per_output = snapshot.labor_per_output[candidate.good]
    capacity = min(capacity, candidate.labor_available/labor_per_output)
    if candidate.resource_available is not None:
        capacity = min(capacity, max(0.0, candidate.resource_available))
    if capacity < snapshot.minimum_capacity:
        return CandidateEvaluation(candidate, False, 'Недостаточны труд или ресурс', 0.0)
    if candidate.margin <= EPS:
        return CandidateEvaluation(candidate, False, 'Неположительная маржа', 0.0)

    capacity, working, investment, profit = project_finances(
        capacity, candidate.available_cash, candidate.startup_cash,
        candidate.material, candidate.delivery, candidate.unit_cost,
        candidate.fixed_unit, candidate.equipment_capital, candidate.margin,
        candidate.expansion,
    )
    if capacity < snapshot.minimum_capacity:
        return CandidateEvaluation(candidate, False, 'Недостаточно денег для минимальной мощности', 0.0)
    horizon = snapshot.investment_payback_months*(0.5+candidate.risk_tolerance)
    if investment > candidate.available_cash+EPS or profit*horizon < investment:
        return CandidateEvaluation(candidate, False, 'Окупаемость не проходит', 0.0)

    project_id = f'{candidate.investor_id}:{candidate.good}:{candidate.province_id}:{snapshot.month}'
    project = dict(
        good=candidate.good, capacity=capacity, location=_location(candidate),
        materials={}, working_cash=working, expected_price=candidate.expected_price,
        expected_profit=profit, expected_investment=investment,
        expected_unit_cost=candidate.unit_cost+candidate.fixed_unit,
        expansion=candidate.expansion, created=snapshot.month,
        chain_requirements={good: capacity*ratio
                            for good, ratio in candidate.chain_ratios},
        project_id=project_id, parent_project_id=candidate.parent_project_id,
        derived_demand=candidate.derived_demand,
        dependency_expires=snapshot.month+snapshot.project_stall_months
            if candidate.parent_project_id else None,
    )
    score = profit*snapshot.investment_payback_months-investment
    return CandidateEvaluation(candidate, True, 'Кандидат принят к применению', score, project)


def evaluate_all(snapshot, candidates=None):
    """Оценить каждого кандидата независимо; порядок входа не влияет на результат."""
    rows = snapshot.candidates if candidates is None else tuple(candidates)
    return tuple(evaluate(snapshot, candidate) for candidate in rows)


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


def build_snapshot(engine, sells, allowed_goods=None):
    """Материализовать рыночные входы, профили и выбор ресурсных клеток."""
    investors = sorted((a for a in engine.accounts.values()
        if a['kind'] in ('private', 'merchant', 'treasury', 'municipal')
        or (a['kind'] == 'enterprise'
            and engine.enterprises[a['id']]['status'] != 'closed'
            and engine.enterprises[a['id']]['available_from'] <= engine.meta['month'])),
        key=lambda a: a['id'])
    for investor in investors:
        engine.profile(investor)
    state = engine.build_investment_state()
    _set_build_caches(engine, state)
    candidates = []
    try:
        index = state['index']
        history = engine.meta.get('market_history', [])
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
                resource_available = None
                if recipe['resource']:
                    tiles = index['resource_tiles'].get(
                        (investor['state_id'], province_id, recipe['resource']), [])
                    if not tiles:
                        continue
                    location.update(
                        tile_id=expansion['tile_id'] if expansion else engine.shuffled(tiles)[0]['id'],
                        city_id=None)
                    used = index['resource_used'].get((province_id, recipe['resource']), 0.0)
                    resource_available = (len(tiles)*engine.cfg['extraction_per_tile']-used)
                quote_firm = dict(id=investor['id'], good=good,
                    price=expansion['price'] if expansion else recipe['price'], **location)
                customer_limit = engine.investment_customer_limit(quote_firm)
                signal = engine.market_signal(history, investor['state_id'], province_id, good)
                price = customer_limit if customer_limit is not None else signal['price'] or recipe['price']
                quote_firm['price'] = price
                demand = max(0.0, engine.investment_demand(quote_firm, sells)
                             - investor['stock'].get(good, 0.0))
                unit = engine.unit_cost(quote_firm, sells)
                fixed_unit = (recipe['labor']*engine.cfg['wage']
                              *engine.cfg.get('enterprise_overhead_rate', 0))
                margin = engine.net_price(quote_firm)-unit-fixed_unit
                distance, _route = engine.path(engine.market_tile(investor), location['tile_id'])
                if distance is None:
                    continue
                bill = engine.construction_bill(1)
                material = sum(quantity*engine.quoted_cost(investor, good_name, quantity, sells)[0]
                               / max(quantity, EPS) for good_name, quantity in bill.items())
                delivery = (sum(quantity*engine.goods[good_name]['weight']
                                for good_name, quantity in bill.items())
                            * distance*engine.cfg['transport_rate'])
                equipment_capital = sum(
                    ratio*engine.quoted_cost(investor, equipment, ratio, sells)[1]
                    for equipment, ratio in recipe.get('equipment', {}).items())
                labor = index['labor'].get((location['state_id'], province_id), 0.0)
                key = f'{investor["id"]}:{good}:{province_id}'
                candidates.append(InvestmentCandidate(
                    key=key, investor_id=investor['id'], good=good, province_id=province_id,
                    location=tuple(sorted(location.items())), expansion=bool(expansion),
                    expected_price=price, demand=demand, unit_cost=unit, fixed_unit=fixed_unit,
                    margin=margin, material=material, delivery=delivery,
                    equipment_capital=equipment_capital, available_cash=engine.free_cash(investor['id']),
                    startup_cash=0.0 if expansion else engine.cfg['startup_cash'],
                    risk_tolerance=investor['risk_tolerance'], labor_available=labor,
                    resource_available=resource_available,
                    chain_ratios=tuple(sorted(engine.recipe_requirements(good, 1.0).items())),
                ))
    finally:
        _clear_build_caches(engine)
    return InvestmentSnapshot(
        month=engine.meta['month'], minimum_capacity=engine.cfg['minimum_capacity'],
        investment_payback_months=engine.cfg['investment_payback_months'],
        project_stall_months=engine.cfg['project_stall_months'],
        labor_per_output=MappingProxyType({good: recipe['labor']
                                           for good, recipe in engine.goods.items()}),
        candidates=tuple(candidates), investor_ids=tuple(a['id'] for a in investors),
    )


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


def commit(engine, evaluations, sells, flow_balances=None):
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


def run_snapshot_batch(engine, sells):
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

    started = time.perf_counter()
    snapshot = build_snapshot(engine, sells, allowed_goods) if should_evaluate else None
    build_seconds = time.perf_counter()-started
    started = time.perf_counter()
    evaluations = evaluate_all(snapshot) if snapshot is not None else ()
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
        investment_evaluate_all=evaluate_seconds, investment_sequential_commit=commit_seconds)
