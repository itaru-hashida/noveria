"""Диагностика сохранённого оборота; результаты не влияют на решения участников."""
import json


def assess_convergence(store, current=None):
    rows = store.query('overview', limit=10000)
    if current:
        rows += current['overview']
    rows = sorted((r for r in rows if r.get('month', 0) > 0), key=lambda r: r['month'])
    if len(rows) < 180:
        return dict(state='Недостаточно истории', months=len(rows), required=180)
    rows = rows[-180:]
    metrics = ('output', 'consumed', 'wages', 'turnover', 'household_gold', 'enterprise_gold',
               'treasury_gold', 'private_gold', 'merchant_gold', 'stock', 'pending_projects')
    metrics = [k for k in metrics if all(r.get(k) is not None for r in rows)]
    windows = [rows[i:i+60] for i in (0, 60, 120)]
    averages = [{k: sum(r[k] for r in w)/60 for k in metrics} for w in windows]
    changes = {k: max(abs(averages[i+1][k]-averages[i][k])/max(1, abs(averages[i][k])) for i in (0, 1)) for k in metrics}
    # Проверяется и направленный тренд внутри каждого окна, а не только его среднее.
    trends = {}
    for k in metrics:
        trends[k] = max(abs(sum((i-29.5)*r[k] for i, r in enumerate(w))/17995)*60/max(1, abs(avg[k]))
                        for w, avg in zip(windows, averages))
    activity = {k: [a.get(k, 0) for a in averages] for k in ('output', 'consumed', 'wages', 'turnover')}
    active = all(values[-1] > 1e-6 and values[-1] >= values[0]*0.95 for values in activity.values())
    stable = all(changes[k] <= .01 and trends[k] <= .01 for k in metrics)
    stable &= all((max(r[k] for r in w)-min(r[k] for r in w))/max(1, abs(avg[k])) <= .01
                  for w, avg in zip(windows, averages) for k in metrics)
    account_drift = 0.0
    endpoints = (rows[0]['month']-1, rows[59]['month'], rows[60]['month']-1, rows[119]['month'], rows[120]['month']-1, rows[-1]['month'])
    snapshots = {}
    for month in endpoints:
        source = current['accounts'] if current and current['overview'][0]['month'] == month else store.query('accounts', start=month, end=month, limit=10000)
        snapshots[month] = {r['id']: r['balance'] for r in source}
    mass = sum(snapshots[endpoints[-1]].values())
    for start, end in zip(endpoints[::2], endpoints[1::2]):
        for identity in snapshots[start].keys() | snapshots[end].keys():
            account_drift = max(account_drift, abs(snapshots[end].get(identity, 0)-snapshots[start].get(identity, 0))/max(1, mass))
    stable &= account_drift <= .001
    # Товарные ряды проверяются отдельно: рост хлеба не скрывает падение рыбы.
    goods = {}
    records = store.connection.execute('''SELECT month,good,
        SUM(json_extract(data,'$.output')),SUM(json_extract(data,'$.consumed')),
        SUM(json_extract(data,'$.revenue')),SUM(json_extract(data,'$.sold')),
        SUM(json_extract(data,'$.stock')) FROM rows WHERE dataset='production'
        AND month BETWEEN ? AND ? GROUP BY month,good''', (rows[0]['month'], rows[-1]['month']))
    for month, good, output, consumed, revenue, sold, stock in records:
        goods.setdefault(good, {})[month] = dict(output=output or 0, consumed=consumed or 0,
            revenue=revenue or 0, sold=sold or 0, stock=stock or 0)
    if current:
        for good in {r['good'] for r in current['production']}:
            values = [r for r in current['production'] if r['good'] == good]
            goods.setdefault(good, {})[rows[-1]['month']] = {k: sum(r.get(k) or 0 for r in values)
                for k in ('output', 'consumed', 'revenue', 'sold', 'stock')}
    goods_changes = {}
    for good, observations in goods.items():
        means = []
        for w in windows:
            values = [observations.get(r['month'], {}) for r in w]
            sold = sum(r.get('sold', 0) for r in values)
            means.append(dict(output=sum(r.get('output', 0) for r in values)/60,
                consumed=sum(r.get('consumed', 0) for r in values)/60,
                stock=sum(r.get('stock', 0) for r in values)/60,
                price=sum(r.get('revenue', 0) for r in values)/sold if sold > 1e-7 else None))
        goods_changes[good] = {k: max((abs(means[i+1][k]-means[i][k])/max(1e-7, abs(means[i][k]))
            for i in (0, 1) if means[i+1][k] is not None and means[i][k] is not None), default=0)
            for k in ('output', 'consumed', 'stock', 'price')}
    goods_stable = all(v <= .01 for changes_by_good in goods_changes.values() for v in changes_by_good.values())
    stable &= goods_stable
    period = None
    if active and not stable:
        for lag in range(2, 13):
            error = max(abs(rows[i][k]-rows[i-lag][k])/max(1, abs(rows[i][k]), abs(rows[i-lag][k]))
                        for i in range(lag, len(rows)) for k in metrics)
            good_error = max((abs(v[k]-observations[m-lag][k])/max(1e-7, abs(v[k]), abs(observations[m-lag][k]))
                for observations in goods.values() for m, v in observations.items() if m-lag in observations
                for k in ('output', 'consumed', 'revenue', 'sold', 'stock')), default=0)
            if error < 1e-5 and good_error < 1e-5:
                cycle_drift = 0.0
                for month in endpoints[1::2]:
                    previous = {r['id']: r['balance'] for r in store.query('accounts', start=month-lag, end=month-lag, limit=10000)}
                    cycle_drift = max(cycle_drift, max((abs(q-previous.get(i, 0))/max(1, mass) for i, q in snapshots[month].items()), default=0))
                if cycle_drift > 1e-5:
                    continue
                period = lag
                break
    state = 'Активное стационарное состояние' if active and stable else 'Устойчивый повторяющийся цикл' if period else 'Продолжающаяся адаптация' if active else 'Экономическая депрессия'
    latest = rows[-1]['month']
    firms = current['enterprises'] if current else store.query('enterprises', start=latest, end=latest, limit=10000)
    stuck = [e['id'] for e in firms if e.get('reachable_demand', 0) > 1e-6 and e.get('expected_margin', -1) > 0
             and e.get('output', 0) <= 1e-7 and e.get('production_limit') == 'План']
    if stuck:
        state = 'Возможный алгоритмический тупик'
    return dict(state=state, windows=[[w[0]['month'], w[-1]['month']] for w in windows],
        averages=averages, relative_changes=changes, relative_trends=trends, goods_relative_changes=goods_changes,
        max_account_drift_share=account_drift, period=period, stuck_candidates=stuck,
        note='Диагностические пороги: 1% показателей, 0,1% денежной массы на счёт; требуется проверка товаров и цен')
