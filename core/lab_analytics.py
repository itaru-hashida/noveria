"""Наблюдения за уже рассчитанной экономикой, без вмешательства в расчёты."""
from collections import defaultdict

from core.lab_store import DATASETS


def capture(engine, report, detail='full'):
    data = {name: [] for name in DATASETS}
    month = report['month']
    accounts = engine.accounts
    firms = engine.enterprises
    trades = report.get('trades', [])
    total_gold = engine.gold()
    money_shares = {kind: sum(a['balance'] for a in accounts.values() if a['kind'] == kind)/total_gold
        if total_gold else 0 for kind in sorted({a['kind'] for a in accounts.values()})}
    for firm in firms.values():
        account = accounts[firm['id']]
        sales = [t for t in trades if t['seller'] == firm['id']]
        purchases = [t for t in trades if t['buyer'] == firm['id']]
        data['enterprises'].append(dict(firm, entity_id=firm['id'], balance=account['balance'],
            stocks=account['stock'], stock_values=account['values'],
            sold=sum(t['quantity'] for t in sales), bought=sum(t['quantity'] for t in purchases),
            purchase_gold=sum(t['gold']+t['freight'] for t in purchases),
            production_cost=firm.get('output', 0)*firm.get('unit_cost', 0)))
    for account in accounts.values():
        row = dict(account, entity_id=account['id'])
        flow = next((r for r in report.get('account_flows', []) if r['id'] == account['id']), {})
        row.update({k: flow.get(k) for k in ('incoming', 'outgoing', 'net')})
        data['accounts'].append(row)
        for good, quantity in account['stock'].items():
            data['stocks'].append(dict(entity_id=account['id'], state_id=account['state_id'],
                province_id=account['province_id'], good=good, quantity=quantity,
                value=account['values'].get(good), kind=account['kind']))

    scopes = sorted({(a['state_id'], a['province_id'] or '') for a in accounts.values()})
    for state, province in scopes:
        province = province or None
        local_accounts = [a for a in accounts.values() if a['state_id'] == state and a['province_id'] == province]
        local_firms = [e for e in data['enterprises'] if e['state_id'] == state and e['province_id'] == province]
        for good in engine.goods:
            producers = [e for e in local_firms if e['good'] == good]
            local_trades = [t for t in trades if t['good'] == good]
            sales = [t for t in local_trades if t['seller_state'] == state and t['seller_province'] == province]
            households = [a for a in local_accounts if a['kind'] == 'household']
            owner_orders = [o for o in report.get('orders', []) if o.get('purpose') == 'Личное потребление собственника'
                and o['good'] == good and o['state_id'] == state and o['province_id'] == province]
            owner_needed = sum(o['quantity'] for o in owner_orders)
            owner_consumed = sum(o.get('consumed_quantity', 0) for o in owner_orders)
            needed = sum(a.get('needs', {}).get(good, {}).get('needed', 0) for a in households)+owner_needed
            consumed = sum(a.get('needs', {}).get(good, {}).get('consumed', 0) for a in households)+owner_consumed
            actions = [a for a in report.get('lifecycle', []) if a['good'] == good and
                       a.get('state_id') == state and a.get('province_id') == province]
            capacity = sum(e['capacity'] for e in producers if e['status'] != 'closed')
            output = sum(e['output'] for e in producers)
            base = dict(state_id=state, province_id=province, good=good)
            data['production'].append(dict(base, producers=sum(e['status'] != 'closed' for e in producers),
                active=sum(e['status'] == 'active' for e in producers), closed=sum(e['status'] == 'closed' for e in producers),
                opened=sum(a['action'] == 'opened' for a in actions), closures=sum(a['action'] == 'closed' for a in actions),
                capacity=capacity, output=output, utilization=output/capacity if capacity else None,
                sold=sum(t['quantity'] for t in sales), revenue=sum(t['gold'] for t in sales),
                profit=sum(e['profit'] for e in producers), expenses=sum(e['expenses'] for e in producers),
                dividends=sum(e.get('dividends', 0) for e in producers),
                owner_needed=owner_needed, owner_consumed=owner_consumed,
                production_cost=sum(e['production_cost'] for e in producers),
                stock=sum(a['stock'].get(good, 0) for a in local_accounts),
                finished_stock=sum(accounts[e['id']]['stock'].get(good, 0) for e in producers),
                needed=needed if month else None, consumed=consumed if month else None,
                unmet=max(0, needed-consumed) if month else None))
            data['geography'].append(dict(base, population=sum(engine.population(a) for a in households),
                household_gold=sum(a['balance'] for a in households),
                enterprise_gold=sum(a['balance'] for a in local_accounts if a['kind'] == 'enterprise'),
                treasury_gold=sum(a['balance'] for a in local_accounts if a['kind'] == 'treasury'),
                investor_gold=sum(a['balance'] for a in local_accounts if a['kind'] in ('private', 'merchant', 'municipal')),
                production=output, needed=needed if month else None, consumed=consumed if month else None,
                unmet=max(0, needed-consumed) if month else None, coverage=consumed/needed if needed else None,
                imports=sum(t['quantity'] for t in local_trades if t['buyer_province'] == province and province is not None and t['seller_province'] != province),
                exports=sum(t['quantity'] for t in local_trades if t['seller_province'] == province and province is not None and t['buyer_province'] != province),
                enterprises=len(producers)))

    data['overview'] = [dict(active=sum(e['status'] == 'active' for e in firms.values()),
        closed=sum(e['status'] == 'closed' for e in firms.values()),
        opened=sum(a['action'] == 'opened' for a in report.get('lifecycle', [])),
        closures=sum(a['action'] == 'closed' for a in report.get('lifecycle', [])),
        output=sum(report.get('production', {}).values()), stock=sum(engine.total_stock().values()),
        sold=sum(t['quantity'] for t in trades), revenue=sum(t['gold'] for t in trades),
        international_volume=sum(t['quantity'] for t in trades if t['buyer_state'] != t['seller_state']),
        domestic_volume=sum(t['quantity'] for t in trades if t['buyer_state'] == t['seller_state']),
        profit=sum(e['profit'] for e in firms.values()),
        unmet=(sum(sum(p['deficits'].values()) for p in report.get('provinces', []))
            +sum(o['remaining'] for o in report.get('orders', [])
                if o.get('purpose') == 'Личное потребление собственника')) if month else None,
        consumed=sum(report.get('consumed', {}).values()) if month else None,
        wages=sum(e.get('wages', 0) for e in firms.values()),
        pending_projects=sum('project' in a for a in accounts.values()),
        unmet_solvent=sum(o.get('unmet_solvent', 0) for o in report.get('orders', []) if o['side'] == 'buy'),
        consumption_tax=sum(t.get('tax', 0) for t in trades),
        turnover=sum(t['gold']+t['freight'] for t in trades),
        dividends=sum(r['dividends'] for r in report.get('dividends', [])),
        working_reserve=sum(e.get('working_reserve', 0) for e in firms.values()),
        project_reserve=sum(a.get('project_reserve', 0) for a in accounts.values()),
        owner_consumption_budget=sum(r['budget'] for r in report.get('owner_consumption', [])),
        owner_consumption_spent=sum(r['spent'] for r in report.get('owner_consumption', [])),
        owner_consumption_unspent=sum(r.get('unused_budget', 0) for r in report.get('owner_consumption', [])),
        owner_consumption_tax=sum(r['tax'] for r in report.get('owner_consumption', [])),
        owner_consumption_freight=sum(r['freight'] for r in report.get('owner_consumption', [])),
        owner_unfilled_orders=sum(o['remaining'] > 1e-7 for o in report.get('orders', [])
            if o.get('purpose') == 'Личное потребление собственника'),
        owner_consumed=report.get('owner_consumed', {}), money_shares=money_shares,
        **{'money_share_'+kind: money_shares.get(kind, 0)
            for kind in ('household', 'enterprise', 'private', 'merchant', 'treasury', 'municipal', 'carrier')},
        **{'dividends_'+kind: sum(r['dividends'] for r in report.get('dividends', []) if r['owner_kind'] == kind)
            for kind in ('private', 'merchant', 'treasury', 'municipal')},
        **{kind+'_gold': sum(a['balance'] for a in accounts.values() if a['kind'] == kind)
           for kind in sorted({a['kind'] for a in accounts.values()})})]
    if detail == 'full':
        # Порядок заявок — порядок создания; порядок сделок — фактическое исполнение.
        data['orders'] = [dict(o, entity_id=o['participant'], creation_index=i,
            filled=o['quantity']-o['remaining']) for i, o in enumerate(report.get('orders', []))]
        data['trades'] = [dict(t, entity_id=t['buyer'], state_id=t['buyer_state'],
            province_id=t['buyer_province'], execution_index=i) for i, t in enumerate(trades)]
        data['flows'] = [dict(f, entity_id=f['sender'], state_id=accounts[f['sender']]['state_id'],
            province_id=accounts[f['sender']]['province_id'], execution_index=i)
            for i, f in enumerate(report.get('flows', []))]
    data['lifecycle'] = [dict(a, entity_id=a['enterprise']) for a in report.get('lifecycle', [])]
    data['diagnostics'] = anomalies(engine, data)
    data['diagnostics'].extend(report.get('diagnostics', []))
    data['diagnostics'].extend(dict(kind='sector_flow', **row) for row in report.get('sector_flows', []))
    return data


def anomalies(engine, data):
    result = []

    def add(kind, row, **evidence):
        result.append(dict(kind=kind, entity_id=row.get('entity_id'), good=row.get('good'),
            state_id=row.get('state_id'), province_id=row.get('province_id'),
            evidence=evidence, interpretation='Наблюдение; причинная связь не установлена'))

    groups = defaultdict(list)
    for e in data['enterprises']:
        groups[e['good']].append(e)
        if e['loss_months'] >= 3 and e['profit'] < 0:
            add('Отрицательная прибыль', e, profit=e['profit'], loss_months=e['loss_months'])
        stock = e['stocks'].get(e['good'], 0)
        if stock > 3*max(e['sold'], 1):
            add('Запасы превышают продажи', e, stock=stock, sold=e['sold'])
    for good, firms in groups.items():
        if all(e['status'] == 'closed' for e in firms):
            add('Закрыта отрасль', firms[0], enterprises=[e['id'] for e in firms])
        positive = [e['sold'] for e in firms if e['sold'] > 0]
        if positive and max(positive) > 10*max(min(e['sold'] for e in firms), 1):
            add('Различия продаж в отрасли', firms[0], min_sales=min(e['sold'] for e in firms), max_sales=max(positive))
    for row in data['production']:
        if (row['unmet'] or 0) <= 0:
            continue
        add('Неудовлетворённый спрос', row, unmet=row['unmet'], consumed=row['consumed'])
        if row['consumed'] == 0:
            add('Нет фактического потребления', row, unmet=row['unmet'])
        recipe = engine.goods[row['good']]['inputs']
        local = [a for a in engine.accounts.values() if a['province_id'] == row['province_id']]
        stocks = {g: sum(a['stock'].get(g, 0) for a in local) for g in recipe}
        if recipe and all(stocks[g] > row['unmet']*ratio for g, ratio in recipe.items()):
            add('Дефицит при запасах сырья', row, unmet=row['unmet'], input_stock=stocks)
    return result


def temporal_anomalies(store, month, data):
    if month >= 180 and month % 60 == 0:
        from core.economy_convergence import assess_convergence
        observation = dict(data, overview=[dict(r, month=month) for r in data['overview']])
        data['diagnostics'].append(dict(kind='convergence', evidence=assess_convergence(store, observation)))
    previous = {(r['state_id'], r['province_id'], r['good']): r
                for r in store.query('production', start=month-1, end=month-1, limit=10000)}
    history = defaultdict(list)
    for r in store.query('production', start=max(1, month-2), end=month-1, limit=10000):
        history[r['state_id'], r['province_id'], r['good']].append(r)
    for row in data['production']:
        key = row['state_id'], row['province_id'], row['good']
        prior = previous.get(key)
        findings = []
        if prior and prior['output'] > 0 and row['output'] < prior['output']*.5:
            findings.append(('Падение выпуска более 50%', dict(previous_output=prior['output'], output=row['output'])))
        if len(history[key]) == 2 and (row['unmet'] or 0) > 0 and all((r['unmet'] or 0) > 0 for r in history[key]):
            findings.append(('Дефицит три месяца подряд', dict(months=[month-2, month-1, month], unmet=row['unmet'])))
        for kind, evidence in findings:
            data['diagnostics'].append(dict(kind=kind, state_id=key[0], province_id=key[1], good=key[2],
                evidence=evidence, interpretation='Наблюдение; причинная связь не установлена'))
