"""Последовательное планирование фирм по прошлому месяцу и начальному состоянию.

Экспериментальный режим не использует текущие заявки при расчёте планов. Снимок
собирается после ``update_prices`` и до заявок; мир меняет только отдельное
применение через штатные ``Economy.order``, ``match`` и ``produce``.
"""
from collections import defaultdict
from dataclasses import dataclass

from modules.economy.support import EPS
from shadow import agent_planner, agent_snapshot


@dataclass(frozen=True, slots=True)
class PurchasePlan:
    good: str
    quantity: float
    price: float
    purpose: str


@dataclass(frozen=True, slots=True)
class FirmPlan:
    firm_id: str
    good: str
    sale_quantity: float
    sale_price: float
    output: float
    requested_output: float
    purchases: tuple[PurchasePlan, ...]
    sales_forecast: float
    expected_unit_cost: float
    expected_net_price: float
    expected_margin: float
    expected_fixed_cost: float
    expected_fixed_unit: float
    wage_unit: float
    free_cash: float
    production_limit: str
    input_prices: tuple[tuple[str, float], ...]
    input_ratios: tuple[tuple[str, float], ...]
    equipment_required: tuple[tuple[str, float], ...]
    equipment_wear: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class LaggedSnapshot:
    arrays: dict
    meta: dict
    parameters: tuple[float, ...]
    eligible: tuple[str, ...]
    firm_index: dict[str, int]
    minimum_price: float
    maximum_price: float


def build_snapshot(engine):
    """Зафиксировать входы фирм; текущая книга намеренно отсутствует."""
    eligible = tuple(sorted(
        firm['id'] for firm in engine.enterprises.values()
        if firm['status'] != 'closed' and not firm.get('suspended')
        and firm['available_from'] <= engine.meta['month']
    ))
    budget = {firm_id: engine.free_cash(firm_id) for firm_id in eligible}
    # Постоянные пустые списки нужны reference_price: исторический fallback не
    # должен зависеть от временных id() разных пустых списков.
    empty_sells = defaultdict(list)
    arrays, meta = agent_snapshot.build(engine, budget, sells=empty_sells)
    for array in arrays.values():
        array.setflags(write=False)
    parameters = tuple(engine.cfg[key] for key in (
        'transport_rate', 'wage', 'consumption_tax', 'markup',
        'enterprise_overhead_rate',
    ))
    return LaggedSnapshot(
        arrays=arrays,
        meta=meta,
        parameters=parameters,
        eligible=eligible,
        firm_index={firm_id: row for row, firm_id in enumerate(meta['firms'])},
        minimum_price=engine.rules['minimum_price'],
        maximum_price=engine.rules['maximum_price'],
    )


def plan(snapshot, firm_id):
    """Чистый расчёт FirmPlan без engine, RNG, заявок и текущего рынка."""
    if firm_id not in snapshot.eligible:
        raise ValueError('Фирма недоступна для lagged_batch: '+firm_id)
    book = snapshot.arrays
    agent = snapshot.firm_index[firm_id]
    _rate, wage, tax, markup, overhead_rate = snapshot.parameters
    good_row = int(book['agent_good'][agent])
    good = snapshot.meta['goods'][good_row]
    capacity = float(book['agent_capacity'][agent])
    price = float(book['agent_price'][agent])
    stock = book['agent_available_stock'][agent]
    budget = float(book['agent_budget'][agent])
    inputs = [(int(g), float(book['good_inputs'][good_row, g]))
              for g in range(snapshot.meta['n_goods'])
              if book['good_inputs'][good_row, g] > EPS]
    equipment = [(int(g), float(book['good_equip'][good_row, g]))
                 for g in range(snapshot.meta['n_goods'])
                 if book['good_equip'][good_row, g] > EPS]
    quotes = {g: float(book['agent_input_price'][agent, g])
              for g, _ratio in inputs+equipment}
    wage_unit = float(book['good_labor'][good_row])*wage
    costs = dict(inputs)
    for g, ratio in equipment:
        costs[g] = costs.get(g, 0.0)+ratio*float(book['good_wear'][g])
    unit_cost = wage_unit+sum(ratio*quotes[g] for g, ratio in costs.items())
    net = 1.0-tax*float(book['good_taxed'][good_row])
    sale_price = price if price*net+EPS >= unit_cost else unit_cost*(1+markup)/max(net, EPS)
    sale_price = min(snapshot.maximum_price, max(snapshot.minimum_price, sale_price))

    # Текущих покупателей нет: ожидание строится только по прошлым наблюдениям.
    forecast = agent_planner.expectations(book, agent, observed=0.0)
    own_stock = float(stock[good_row])
    expected_sales = min(own_stock, forecast)
    projected_stock = max(0.0, own_stock-expected_sales)
    desired = max(0.0, min(capacity, forecast-projected_stock))
    fixed_cost = capacity*wage_unit*overhead_rate
    fixed_unit = fixed_cost/desired if desired > EPS else 0.0
    expected_net = sale_price*net
    expected_margin = expected_net-unit_cost-fixed_unit
    reason = 'План'
    if desired <= EPS:
        reason = 'Готовые запасы' if forecast > EPS else 'Нет ожидаемого спроса'
    elif expected_margin < -EPS:
        desired, reason = 0.0, 'Отрицательная ожидаемая маржа'

    requirements = dict(inputs)
    for g, ratio in equipment:
        requirements[g] = requirements.get(g, 0.0)+ratio

    def purchases_for(output):
        rows = []
        for g, ratio in requirements.items():
            missing = max(0.0, output*ratio-float(stock[g]))
            if missing > EPS:
                rows.append((g, missing, quotes[g]))
        return rows

    def cash_needed(output):
        purchases = purchases_for(output)
        return fixed_cost+output*wage_unit+sum(q*p for _g, q, p in purchases)

    target = desired
    if target > EPS and cash_needed(target) > budget+EPS:
        low, high = 0.0, target
        for _ in range(40):
            middle = (low+high)/2
            if cash_needed(middle) <= budget:
                low = middle
            else:
                high = middle
        target, reason = low, 'Деньги на выпуск'

    purposes = {g: 'Сырьё' for g, _ratio in inputs}
    purposes.update({g: 'Оборудование' for g, _ratio in equipment})
    purchases = tuple(
        PurchasePlan(snapshot.meta['goods'][g], quantity, quote, purposes[g])
        for g, quantity, quote in purchases_for(target)
    )
    names = snapshot.meta['goods']
    return FirmPlan(
        firm_id=firm_id,
        good=good,
        sale_quantity=own_stock,
        sale_price=sale_price,
        output=target,
        requested_output=desired,
        purchases=purchases,
        sales_forecast=forecast,
        expected_unit_cost=unit_cost,
        expected_net_price=expected_net,
        expected_margin=expected_margin,
        expected_fixed_cost=fixed_cost,
        expected_fixed_unit=fixed_unit,
        wage_unit=wage_unit,
        free_cash=budget,
        production_limit=reason,
        input_prices=tuple(sorted((names[g], quote) for g, quote in quotes.items())),
        input_ratios=tuple((names[g], ratio) for g, ratio in inputs),
        equipment_required=tuple((names[g], target*ratio) for g, ratio in equipment),
        equipment_wear=tuple((names[g], target*ratio*float(book['good_wear'][g]))
                             for g, ratio in equipment),
    )


def plan_all(snapshot, firm_order=None):
    """Рассчитать независимые планы; порядок обхода не входит в их входы."""
    order = snapshot.eligible if firm_order is None else tuple(firm_order)
    if set(order) != set(snapshot.eligible) or len(order) != len(snapshot.eligible):
        raise ValueError('Обход должен содержать каждую доступную фирму ровно один раз')
    return {firm_id: plan(snapshot, firm_id) for firm_id in order}


def publish_sales(engine, plans, sells):
    """Опубликовать только стартовые запасы до заявок покупателей."""
    published = False
    for firm_id in sorted(plans):
        item = plans[firm_id]
        firm = engine.enterprises[firm_id]
        if firm['status'] == 'closed' or item.sale_quantity <= EPS:
            continue
        offer = engine.order(
            firm_id, item.good, item.sale_quantity, 'sell',
            item.sale_price, 'Продажа запасов',
        )
        if offer:
            sells[item.good].append(offer)
            published = True
    if published:
        # Публикация меняет состав книги, а не только цены её офферов.
        engine.invalidate_market_books()
    return sells


def apply_plans(engine, plans):
    """Применить производство и закупки без повторной публикации продаж."""
    for firm_id in sorted(plans):
        item = plans[firm_id]
        firm = engine.enterprises[firm_id]
        if firm['status'] == 'closed':
            continue
        account = engine.accounts[firm_id]
        account['protected'] = max(
            0.0, account.get('protected', 0.0)-account.pop('operating_protected', 0.0))
        payroll = min(item.output*item.wage_unit, engine.free_cash(firm_id))
        account['payroll_protected'] = payroll
        account['protected'] += payroll
        firm.update(
            agent_sales_forecast=item.sales_forecast,
            sales_forecast=item.sales_forecast,
            reachable_demand=0.0,
            planned_output=item.output,
            input_target_output=item.output,
            planned_price=item.sale_price,
            production_limit=item.production_limit,
            expected_unit_cost=item.expected_unit_cost,
            expected_price=item.expected_net_price,
            expected_margin=item.expected_margin,
            expected_fixed_cost=item.expected_fixed_cost,
            expected_fixed_unit=item.expected_fixed_unit,
        )
        production_plan = dict(
            month=engine.meta['month'],
            output=item.output,
            sale_price=item.sale_price,
            expected_net_price=item.expected_net_price,
            input_prices=dict(item.input_prices),
            input_ratios=dict(item.input_ratios),
            wage_unit=item.wage_unit,
            equipment_required=dict(item.equipment_required),
            equipment_wear=dict(item.equipment_wear),
            fixed_unit=item.expected_fixed_unit,
            requested_output=item.requested_output,
            free_cash=item.free_cash,
            reachable_demand=0.0,
            sales_forecast=item.sales_forecast,
            expected_margin=item.expected_margin,
            reason=item.production_limit,
            input_orders=[],
        )
        firm['production_plan'] = production_plan
        for purchase in sorted(item.purchases, key=lambda row: (row.good, row.purpose)):
            order = engine.order(
                firm_id, purchase.good, purchase.quantity, 'buy',
                purchase.price, purchase.purpose,
            )
            if order:
                production_plan['input_orders'].append(order['id'])
