"""Наблюдение за V3 и изолированные исследовательские вмешательства, без изменения правил игры."""
import argparse
import copy
import hashlib
import itertools
import json
import math
import pickle
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.economy import Economy
from core.lab import engine_version

EPS = 1e-7


def ratio(numerator, denominator):
    return numerator/denominator if denominator > EPS else None


def paired_test(differences):
    """Двусторонняя точная перестановка знаков: единица анализа — мир, не месяц."""
    observed = abs(statistics.mean(differences))
    null = [abs(statistics.mean(d*s for d, s in zip(differences, signs)))
        for signs in itertools.product((-1, 1), repeat=len(differences))]
    return dict(n=len(differences), mean_difference=statistics.mean(differences),
        seed_range=[min(differences), max(differences)],
        p_two_sided=sum(v >= observed-1e-12 for v in null)/len(null))


def input_ceiling(engine, buy, record):
    """Предельная цена входа при неизменной ожидаемой чистой цене выпуска.

    Это условная ex ante оценка, а не обещание будущей прибыли.
    Остальные входы оцениваются не ниже уже принятых ценовых обязательств.
    """
    if not record or buy['good'] not in record['inputs'] or record['plan'] <= EPS:
        return None
    firm = engine.enterprises[buy['participant']]
    other_cost = record['wage_unit']
    for good, quantity in record['inputs'].items():
        if good == buy['good']:
            continue
        prices = [o['price'] for o in engine.report['orders']
            if o['id'] in record['input_orders'] and o['good'] == good]
        other_cost += quantity*max([record['prices'][good]]+prices)
    return max(0, (record['net_price']-other_cost)/record['inputs'][buy['good']])


def opportunity(engine, buy, sells, record):
    """Свидетель локально выполнимой упущенной покупки, без изменения состояния."""
    ceiling = input_ceiling(engine, buy, record)
    if ceiling is None or buy['remaining'] <= EPS:
        return None
    a = engine.accounts[buy['participant']]
    recipe = engine.goods[engine.enterprises[a['id']]['good']]
    output = record['plan']
    for good, quantity in record['inputs'].items():
        if good != buy['good']:
            output = min(output, engine.free_stock(a['id'], good)/quantity)
    labor = sum(engine.population(h)*engine.cfg['labor_share'] for h in engine.workforce(engine.enterprises[a['id']]))
    output = min(output, labor/recipe['labor'])
    if recipe['resource']:
        resource = sum(engine.cfg['extraction_per_tile'] for t in engine.tiles.values()
            if t['province_id'] == a['province_id'] and recipe['resource'] in t['resources'])
        output = min(output, resource)
    if record['wage_unit'] > EPS:
        output = min(output, a.get('payroll_protected', 0)/record['wage_unit'])
    amount = min(buy['remaining'], output*record['inputs'][buy['good']])
    budget = buy['reserved']+engine.free_cash(a['id'])
    for price, sell, _ in engine.offers(a, buy['good'], sells):
        if price <= buy['price']+EPS or price >= ceiling-EPS:
            continue
        quantity = min(amount, sell['remaining'], budget/price)
        if quantity > EPS:
            return dict(enterprise=a['id'], good=buy['good'], quantity=quantity,
                price=price, original_limit=buy['price'], conditional_surplus=quantity*(ceiling-price))
    return None


class Probe:
    def __init__(self, variant):
        self.variant = variant
        self.phase = None
        self.month = 0
        self.plans = {}
        self.prices = Counter()
        self.ages = []
        self.decisions = []
        self.consistency = []
        self.previous = {}
        self.idle = Counter()
        self.regret_streak = Counter()
        self.cash_return = 0.0

    def begin(self, month):
        self.month = month
        self.plans = {}
        self.prices = Counter()
        self.ages = []
        self.decisions = []
        self.consistency = []
        self.cash_return = 0.0


class ObservedEconomy(Economy):
    def pay_dividends(self, sells):
        super().pay_dividends(sells)
        if self.probe.variant != 'cash_return':
            return
        for firm in self.enterprises.values():
            if firm['status'] == 'closed':
                continue
            working = self.working_cash_reserve(firm, sells)
            project = self.project_cash_reserve(firm['id'], sells)
            available = self.cash_after_commitments(firm['id'], project, working)
            self.transfer(firm['id'], firm['owner_id'], available,
                'Исследование: возврат избыточного денежного капитала')
            self.probe.cash_return += available

    def reference_price(self, buyer, good, sells):
        offers = self.offers(buyer, good, sells)
        p = self.probe
        if offers:
            if p.phase == 'procurement':
                p.prices['live'] += 1
            return offers[0][0]
        history = self.meta.get('market_history', [])
        signal = self.market_signal(history, buyer['state_id'], self.market_province(buyer), good)
        if p.phase == 'procurement':
            if signal['landed_price']:
                p.prices['history'] += 1
                observations = [(self.meta['month']-h['month'], r['volume']) for h in history for r in h['markets']
                    if r['state_id'] == buyer['state_id'] and r['province_id'] == self.market_province(buyer)
                    and r['good'] == good and r['volume'] > EPS]
                mass = sum(q for _, q in observations)
                if mass > EPS:
                    p.ages.append(sum(age*q for age, q in observations)/mass)
            else:
                p.prices['prior'] += 1
        return signal['landed_price'] or self.goods[good]['price']

    def input_requests(self, firm, sells):
        p = self.probe
        a = self.accounts[firm['id']]
        inputs = self.goods[firm['good']]['inputs']
        prices = {g: Economy.reference_price(self, a, g, sells) for g in inputs}
        held = {g: self.free_stock(a['id'], g) for g in inputs}
        stock = a['stock'].get(firm['good'], 0)
        # Повторяются только чистые оценки, на ограниченной детерминированной выборке.
        if p.month % 10 == 0 and len(p.consistency) < 10:
            state = self.rng.getstate()
            first = self.production_outlook(firm, sells)
            second = self.production_outlook(firm, sells)
            p.consistency.append(first == second and state == self.rng.getstate())
        p.phase = 'procurement'
        super().input_requests(firm, sells)
        p.phase = None
        plan = firm['production_plan']
        orders = [o for o in self.report['orders'] if o['id'] in plan['input_orders']]
        wage = self.goods[firm['good']]['labor']*self.cfg['wage']
        required = plan['output']*wage+sum(o['quantity']*o['price'] for o in orders)
        labor = sum(self.population(h)*self.cfg['labor_share'] for h in self.workforce(firm))
        resource = self.goods[firm['good']]['resource']
        physical = min(firm['capacity'], labor/self.goods[firm['good']]['labor'])
        if resource:
            physical = min(physical, sum(self.cfg['extraction_per_tile'] for t in self.tiles.values()
                if t['province_id'] == firm['province_id'] and resource in t['resources']))
        ids = [o['id'] for o in self.report['orders'][:plan['known_order_count']]
            if o['side'] == 'buy' and o['good'] == firm['good'] and o['participant'] != firm['id']
            and self.path(firm['tile_id'], o['tile_id'])[0] is not None]
        p.plans[firm['id']] = dict(id=firm['id'], good=firm['good'], tile=firm['tile_id'],
            plan=plan['output'], requested=plan['requested_output'], forecast=firm['sales_forecast'],
            demand=firm['reachable_demand'], stock=stock, capacity=firm['capacity'],
            margin=firm['expected_margin'], net_price=firm['expected_price'],
            free_cash=plan['free_cash'], financial_feasible=required <= plan['free_cash']+EPS,
            physical_feasible=plan['output'] <= physical+EPS, known_ids=ids,
            input_orders=plan['input_orders'], inputs=dict(inputs), prices=prices,
            held=held, wage_unit=wage)

    def match(self, buy, sells, local_only=False):
        if buy is None or buy['status'] in ('cancelled', 'expired', 'filled'):
            return super().match(buy, sells, local_only=local_only)
        p = self.probe
        a = self.accounts[buy['participant']]
        original = buy['price']
        marginal = self.quoted_cost(a, buy['good'], buy['remaining'], sells)[1]
        record = p.plans.get(buy['participant']) if buy['purpose'] == 'Сырьё' else None
        changed = False
        if p.variant == 'input_bid' and record and buy['remaining'] > EPS:
            limit = input_ceiling(self, buy, record)
            budget = buy['reserved']+self.free_cash(a['id'])
            price = min(marginal, limit, budget/buy['remaining'])
            if price > buy['price']+EPS:
                extra = buy['remaining']*(price-buy['price'])
                a['reserved'] += extra
                buy['reserved'] += extra
                buy.update(price=price, backing=buy['reserved'])
                changed = True
        super().match(buy, sells, local_only=local_only)
        changed = changed or (buy['purpose'] == 'Сырьё' and buy.get('adaptive_input_budget', False)
            and buy['price'] > original+EPS)
        remaining = buy['remaining']
        executable = 0.0
        if remaining > EPS:
            for price, sell, _ in self.offers(a, buy['good'], sells):
                if price <= buy['price']+EPS:
                    executable = max(executable, min(remaining, sell['remaining'], buy['reserved']/price))
        witness = opportunity(self, buy, sells, record) if record else None
        p.decisions.append(dict(id=buy['id'], participant=a['id'], good=buy['good'], purpose=buy['purpose'],
            quote_accuracy=1-abs(original-marginal)/max(original, marginal, EPS),
            original_limit=original, execution_limit=buy['price'], marginal_price=marginal,
            reason=buy.get('reason', ''), quantity=buy['quantity'], remaining=remaining,
            strict_missed=executable > EPS, witness=witness, changed=changed))


def capture(engine):
    p, r = engine.probe, engine.report
    firms = engine.enterprises
    accounts = engine.accounts
    trades = r['trades']
    sales = defaultdict(float)
    offered = defaultdict(float)
    outgoing = defaultdict(float)
    for t in trades:
        if t['seller'] in firms and t['good'] == firms[t['seller']]['good']:
            sales[t['seller']] += t['quantity']
    for o in r['orders']:
        if o['side'] == 'sell' and o['participant'] in firms and o['good'] == firms[o['participant']]['good']:
            offered[o['participant']] += o['quantity']
    for flow in r['flows']:
        outgoing[flow['sender']] += flow['gold']
    for a in accounts.values():
        p.idle[a['id']] = p.idle[a['id']]+1 if outgoing[a['id']] <= EPS else 0
    values = engine.goods
    ref = lambda good: values[good]['price']
    by_good = {}
    for good in values:
        relevant = [e for e in firms.values() if e['good'] == good and e['status'] != 'closed']
        hs = [a for a in accounts.values() if a['kind'] == 'household']
        need = sum(a.get('needs', {}).get(good, {}).get('needed', 0) for a in hs)
        consumption = sum(a.get('needs', {}).get(good, {}).get('consumed', 0) for a in hs)
        orders = [o for o in r['orders'] if o['side'] == 'buy' and o['good'] == good]
        planned = sum(e.get('planned_output', 0) for e in relevant)
        output = r['production'].get(good, 0)
        used = r['consumed'].get(good, 0)+r['inputs_used'].get(good, 0)+r['construction_used'].get(good, 0)
        by_good[good] = dict(need=need, household_consumed=consumption, coverage=ratio(consumption, need),
            output=output, used=used, reproduction=ratio(output, used), stock=r['stocks'][good],
            plan=planned, execution_fidelity=ratio(output, planned), capacity=sum(e['capacity'] for e in relevant),
            order_quantity=sum(o['quantity'] for o in orders),
            unmet_solvent=sum(o.get('unmet_solvent', 0) for o in orders),
            unsold=sum(o['remaining'] for o in r['orders'] if o['side'] == 'sell' and o['good'] == good),
            losses=sum(e['profit'] < -EPS for e in relevant if e['revenue'] > EPS),
            revenue_firms=sum(e['revenue'] > EPS for e in relevant))
    plan_rows = []
    forecast_rows = []
    responses = []
    for identity, record in p.plans.items():
        good = record['good']
        eligible = {o['id'] for o in r['orders'] if o['side'] == 'buy' and o['good'] == good
            and o['participant'] != identity and engine.path(record['tile'], o['tile_id'])[0] is not None}
        record['signal_total'] = len(eligible)
        record['signal_seen'] = len(eligible & set(record['known_ids']))
        record['output'] = firms[identity]['output']
        record['limit'] = firms[identity].get('production_limit')
        previous = p.previous.get(identity)
        if previous:
            censored = offered[identity] <= sales[identity]+EPS or offered[identity]+EPS < max(previous['forecast'], previous['sold'])
            forecast_rows.append(dict(id=identity, good=good, censored=censored,
                model_error=abs(previous['forecast']-sales[identity])/max(1, record['capacity']),
                naive_error=abs(previous['sold']-sales[identity])/max(1, record['capacity'])))
            delta = record['demand']-previous['demand']
            comparable = (record['margin'] >= 0 and previous['margin'] >= 0
                and abs(record['capacity']-previous['capacity']) <= EPS
                and abs(record['stock']-previous['stock']) <= .1*record['capacity']
                and abs(delta) > max(EPS, .1*max(1, previous['demand']))
                and EPS < previous['plan'] < previous['capacity']-EPS
                and record['free_cash'] >= record['plan']*max(record['net_price']-record['margin'], EPS))
            if comparable:
                change = record['plan']-previous['plan']
                elasticity = ((math.log1p(record['plan'])-math.log1p(previous['plan']))/
                    (math.log1p(record['demand'])-math.log1p(previous['demand'])))
                responses.append(dict(id=identity, response=delta*change > EPS, elasticity=elasticity))
        p.previous[identity] = dict(record, sold=sales[identity])
        plan_rows.append({k: v for k, v in record.items() if k not in ('known_ids',)})
    witnesses = [d['witness'] for d in p.decisions if d['witness']]
    regretting = {w['enterprise'] for w in witnesses}
    for identity in firms:
        p.regret_streak[identity] = p.regret_streak[identity]+1 if identity in regretting else 0
    prices = dict(p.prices)
    stock_value = sum(q*ref(g) for g, q in r['stocks'].items())
    locked = sum(sum(q*ref(g) for g, q in a.get('project', {}).get('materials', {}).items()) for a in accounts.values())
    idle_inputs = sum(sum(q*ref(g) for g, q in accounts[e['id']]['stock'].items()
        if g in values[e['good']]['inputs']) for e in firms.values() if e['status'] == 'idle')
    basic = engine.cfg['basic_needs']
    needed = sum(by_good[g]['need']*ref(g) for g in basic)
    consumed = sum(by_good[g]['household_consumed']*ref(g) for g in basic)
    turnover = sum(t['gold']+t['freight'] for t in trades)
    gold = engine.gold()
    projects = [a['project'] for a in accounts.values() if 'project' in a]
    macro = dict(basic_coverage=ratio(consumed, needed), household_bread=by_good['bread']['household_consumed'],
        turnover=turnover, velocity=ratio(turnover, gold), money=r['balances'], money_shares=r['money_shares'],
        idle_money_share=ratio(sum(a['balance'] for a in accounts.values() if p.idle[a['id']] >= 6), gold)
            if p.month >= 6 else None,
        profit=sum(e['profit'] for e in firms.values()), wages=sum(e['wages'] for e in firms.values()),
        pending=len(projects), locked_stock_share=ratio(locked, stock_value),
        idle_input_share=ratio(idle_inputs, stock_value),
        oldest_stall=max((engine.meta['month']-proj.get('last_progress', proj['created']) for proj in projects), default=0),
        starts=sum(d['outcome'] == 'started' for d in r['investment_decisions']),
        built=sum(d['outcome'] in ('built', 'expanded') for d in r['investment_decisions']),
        cancelled=sum(d['outcome'] == 'cancelled' for d in r['investment_decisions']),
        open=sum(e['status'] != 'closed' for e in firms.values()))
    macro['cash_return'] = p.cash_return
    micro = dict(signal_seen=sum(x['signal_seen'] for x in plan_rows), signal_total=sum(x['signal_total'] for x in plan_rows),
        prices=prices, historical_age_mean=statistics.mean(p.ages) if p.ages else None,
        quote_accuracy=statistics.mean(d['quote_accuracy'] for d in p.decisions) if p.decisions else None,
        response_events=len(responses), correct_responses=sum(d['response'] for d in responses),
        elasticity_median=statistics.median(d['elasticity'] for d in responses) if responses else None,
        consistency_checks=len(p.consistency), consistent=sum(p.consistency),
        feasible=sum(d['financial_feasible'] for d in plan_rows), physical_feasible=sum(d['physical_feasible'] for d in plan_rows),
        plans=len(plan_rows), witnessed_regret=sum(w['conditional_surplus'] for w in witnesses),
        dominated=len(witnesses), input_decisions=sum(d['purpose'] == 'Сырьё' for d in p.decisions),
        strict_missed=sum(d['strict_missed'] for d in p.decisions), trade_decisions=len(p.decisions),
        violated=sum(not v for v in r['invariants'].values()), invariants=len(r['invariants']),
        persistent_regret_firms=sum(v >= 3 for v in p.regret_streak.values()), max_regret_streak=max(p.regret_streak.values(), default=0),
        forecast_rows=len(forecast_rows), forecast_uncensored=sum(not x['censored'] for x in forecast_rows),
        policy_changes=sum(d['changed'] for d in p.decisions))
    return dict(month=engine.meta['month'], relative_month=p.month, macro=macro, goods=by_good,
        food_diagnostics=r.get('food_diagnostics'),
        micro=micro, firms=plan_rows, decisions=p.decisions, forecasts=forecast_rows, responses=responses,
        invariants=r['invariants'])


def run(args):
    if not 1 <= args.months <= 10000 or not 0 <= args.warmup <= 10000:
        raise ValueError('Некорректная продолжительность опыта')
    if args.variant == 'input_bid' and getattr(Economy, 'INPUT_BID_REVISION', 0):
        raise ValueError('Политика input_bid уже включена в игровой движок. '
            'Для исторического сравнения используйте ревизию a5b7ff9; '
            'control теперь измеряет перенесённую политику.')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    if args.checkpoint:
        checkpoint = Path(args.checkpoint)
        # Только собственные доверенные контрольные состояния исследования.
        with checkpoint.open('rb') as f:
            engine = pickle.load(f)
    else:
        from core.db import WorldDB
        from core.economy_state import EconomyState
        from core.events import EventBus
        from core.world_service import WorldService
        from loader import load_world
        content = json.loads((ROOT/'content/initial_world.json').read_text(encoding='utf-8'))
        content['generation']['seed'] = args.seed
        scenario = out/'scenario.json'
        scenario.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
        with WorldDB(out/'initial.lbug') as db:
            load_world(db, scenario)
            service = WorldService(db, EventBus())
            # Экономика создаётся в памяти один раз: в Ladybug её больше нет.
            state = EconomyState.create(service.world_graph())
            state.bootstrap()
            engine = state.engine
        # Исследовательский observer читает полный отчёт; игровой none для
        # этого намеренно недостаточен.
        engine.meta['report_detail'] = 'deep'
        for _ in range(args.warmup):
            engine.step()
        checkpoint = out/'checkpoint.pickle'
        with checkpoint.open('wb') as target:
            pickle.dump(engine, target)
    reference = copy.deepcopy(engine) if args.variant == 'control' and not args.verify_state else None
    if reference is not None:
        reference.__class__ = Economy
    engine.__class__ = ObservedEconomy
    engine.probe = Probe(args.variant)
    config = dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        observer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        engine=engine_version(), checkpoint=str(checkpoint), checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        seed=engine.seed, initial_month=engine.meta['month'],
        input_bid_revision=getattr(Economy, 'INPUT_BID_REVISION', 0),
        stock_planning_revision=getattr(Economy, 'STOCK_PLANNING_REVISION', 0),
        profit_distribution_revision=getattr(Economy, 'PROFIT_DISTRIBUTION_REVISION', 0),
        household_top_up_revision=getattr(Economy, 'HOUSEHOLD_TOP_UP_REVISION', 0),
        basic_budget_revision=getattr(Economy, 'BASIC_BUDGET_REVISION', 0),
        months=args.months, variant=args.variant, status='running', completed=0,
        shock='zero household needs and owner purchases in relative months 7..12; restoration in 13' if args.variant == 'shock' else None)
    (out/'metadata.json').write_text(json.dumps(config, indent=2))
    initial = engine.meta['month']
    saved = {k: copy.deepcopy(engine.cfg[k]) for k in ('basic_needs', 'extra_needs', 'owner_consumption_rate')}
    try:
        with (out/'monthly.jsonl').open('w') as target:
            for step in range(1, args.months+1):
                if args.variant == 'shock' and step == 7:
                    for key in ('basic_needs', 'extra_needs'):
                        engine.cfg[key] = {good: 0 for good in saved[key]}
                    engine.cfg['owner_consumption_rate'] = 0
                if args.variant == 'shock' and step == 13:
                    engine.cfg.update(copy.deepcopy(saved))
                engine.probe.begin(step)
                engine.step()
                if reference is not None:
                    reference.step()
                row = capture(engine)
                target.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
                target.flush()
                config['completed'] = step
                if not all(row['invariants'].values()):
                    raise ValueError('Нарушен инвариант')
                if step % 12 == 0:
                    print(args.variant, initial+step, 'coverage', row['macro']['basic_coverage'], flush=True)
        if args.verify_state or reference is not None:
            if args.verify_state:
                with Path(args.verify_state).open('rb') as source:
                    expected = pickle.load(source)
            else:
                expected = reference
            for name in ('accounts', 'enterprises', 'meta'):
                serialize = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)
                if serialize(getattr(engine, name)) != serialize(getattr(expected, name)):
                    raise AssertionError('Наблюдение изменило состояние: '+name)
            config['observer_state_parity'] = True
        config['status'] = 'completed'
    except BaseException as exc:
        config.update(status='failed', error=str(exc))
        raise
    finally:
        (out/'metadata.json').write_text(json.dumps(config, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--checkpoint', help='Только доверенный локальный pickle из собственного опыта')
    source.add_argument('--seed', type=int, help='Создать новый мир из content/initial_world.json')
    parser.add_argument('--warmup', type=int, default=180, help='Ходы до измерения для нового мира')
    parser.add_argument('--out', required=True, help='Новый каталог результатов; существующий не перезаписывается')
    parser.add_argument('--months', type=int, default=60)
    parser.add_argument('--variant', choices=('control', 'input_bid', 'shock', 'cash_return'), default='control')
    parser.add_argument('--verify-state')
    run(parser.parse_args())
