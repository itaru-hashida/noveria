"""Сводка наблюдений и парных исследовательских прогонов V3."""
import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.research_agents import paired_test, ratio


def avg(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def holm(pvalues):
    adjusted, previous = {}, 0.0
    for index, (name, value) in enumerate(sorted(pvalues.items(), key=lambda item: item[1])):
        previous = max(previous, min(1, (len(pvalues)-index)*value))
        adjusted[name] = previous
    return adjusted


def summarize(rows):
    window = rows[-30:]
    total = lambda key: sum(r['micro'][key] for r in window)
    prices = {key: sum(r['micro']['prices'].get(key, 0) for r in window) for key in ('live', 'history', 'prior')}
    forecasts = [f for r in window for f in r['forecasts'] if not f['censored']]
    model, naive = avg([f['model_error'] for f in forecasts]), avg([f['naive_error'] for f in forecasts])
    persistent, streak, input_firms = set(), {}, set()
    for row in rows:
        regrets = {d['participant'] for d in row['decisions'] if d['witness']}
        for identity in streak.keys() | regrets:
            streak[identity] = streak.get(identity, 0)+1 if identity in regrets else 0
        if row['relative_month'] > 30:
            persistent.update(i for i, count in streak.items() if count >= 3)
            input_firms.update(d['participant'] for d in row['decisions'] if d['purpose'] == 'Сырьё')
    elasticity = [v['elasticity'] for r in window for v in r['responses']]
    local_regrets = [d['witness']['conditional_surplus'] for r in window for d in r['decisions'] if d['witness']]
    signed = dict(signal_coverage=ratio(total('signal_seen'), total('signal_total')),
        signal_seen=total('signal_seen'), signal_total=total('signal_total'),
        live_price_share=ratio(prices['live'], sum(prices.values())), price_sources=prices,
        historical_age=ratio(sum((r['micro']['historical_age_mean'] or 0)*r['micro']['prices'].get('history', 0) for r in window), prices['history']),
        quote_accuracy=ratio(sum((r['micro']['quote_accuracy'] or 0)*r['micro']['trade_decisions'] for r in window), total('trade_decisions')),
        response_rate=ratio(total('correct_responses'), total('response_events')), response_events=total('response_events'),
        elasticity_median=statistics.median(elasticity) if elasticity else None,
        consistency=ratio(total('consistent'), total('consistency_checks')), consistency_checks=total('consistency_checks'),
        financial_feasibility=ratio(total('feasible'), total('plans')),
        individual_physical_feasibility=ratio(total('physical_feasible'), total('plans')), plans=total('plans'),
        local_regret_median=statistics.median(local_regrets) if local_regrets else None,
        local_regret_sum_not_joint=total('witnessed_regret'),
        dominated_rate=ratio(total('dominated'), total('input_decisions')), dominated=total('dominated'),
        input_decisions=total('input_decisions'), strict_missed_rate=ratio(total('strict_missed'), total('trade_decisions')),
        strict_missed=total('strict_missed'), trade_decisions=total('trade_decisions'),
        invariant_violation_rate=ratio(total('violated'), total('invariants')), invariant_checks=total('invariants'),
        forecast_model_mae=model, forecast_naive_mae=naive,
        forecast_improvement=1-model/naive if model is not None and naive is not None and naive > 1e-7 else None,
        forecast_uncensored=len(forecasts), forecast_total=total('forecast_rows'),
        persistent_regret_firms=len(persistent), input_firms=len(input_firms),
        persistent_regret_rate=ratio(len(persistent), len(input_firms)),
        max_regret_streak=max(r['micro']['max_regret_streak'] for r in window),
        policy_changes=total('policy_changes'))
    early, late = {}, {}
    for target, subset in ((early, rows[1:21]), (late, rows[40:60])):
        for row in subset:
            for f in row['forecasts']:
                if not f['censored']:
                    target.setdefault(f['id'], []).append(f['model_error'])
    cohort = {i for i in early.keys() & late.keys() if len(early[i]) >= 3 and len(late[i]) >= 3}
    first, last = avg([avg(early[i]) for i in cohort]), avg([avg(late[i]) for i in cohort])
    signed.update(forecast_cohort=len(cohort), forecast_early=first, forecast_late=last,
        temporal_forecast_improvement=1-last/first if first is not None and first > 1e-7 else None)
    macro = {k: avg([r['macro'][k] for r in window]) for k in ('basic_coverage', 'household_bread', 'velocity',
        'idle_money_share', 'profit', 'wages', 'pending', 'locked_stock_share', 'idle_input_share')}
    macro.update(enterprise_share_start=rows[0]['macro']['money_shares']['enterprise'],
        enterprise_share_end=rows[-1]['macro']['money_shares']['enterprise'],
        starts=sum(r['macro']['starts'] for r in rows), built=sum(r['macro']['built'] for r in rows),
        cancelled=sum(r['macro']['cancelled'] for r in rows), open_end=rows[-1]['macro']['open'],
        max_stall=max(r['macro']['oldest_stall'] for r in rows))
    macro['cash_return_total'] = sum(r['macro'].get('cash_return', 0) for r in rows)
    funding = [p for r in window for p in r['firms'] if p['margin'] > 0 and p['demand'] > 1e-7 and p['requested'] > 1e-7]
    macro['funding_cut_rate'] = ratio(sum(p['plan']+1e-7 < p['requested'] for p in funding), len(funding))
    macro['funding_eligible'] = len(funding)
    goods = {}
    for good in window[0]['goods']:
        amount = lambda key: sum(r['goods'][good][key] for r in window)
        goods[good] = dict(output=amount('output')/30, used=amount('used')/30,
            reproduction=ratio(amount('output'), amount('used')),
            coverage=ratio(amount('household_consumed'), amount('need')),
            execution_fidelity=ratio(amount('output'), amount('plan')),
            utilization=ratio(amount('output'), amount('capacity')),
            funded_unmet_share=ratio(amount('unmet_solvent'), amount('order_quantity')),
            stock_change=rows[-1]['goods'][good]['stock']-rows[-31]['goods'][good]['stock'],
            selling_loss_rate=ratio(amount('losses'), amount('revenue_firms')))
    return dict(micro=signed, macro=macro, goods=goods)


def recovery(control, shock, threshold=.9):
    first, confirmed, streak = None, None, 0
    for i in range(14, len(shock)):
        before = avg([r['macro']['household_bread'] for r in control[i-2:i+1]])
        after = avg([r['macro']['household_bread'] for r in shock[i-2:i+1]])
        if before > 1e-7 and after >= threshold*before:
            first = first if first is not None else i+1-12
            streak += 1
            if streak == 3:
                confirmed = i+1-12
                break
        else:
            streak = 0
    return dict(first_crossing_lag=first, confirmed_lag=confirmed, censored=confirmed is None,
        horizon=len(shock)-12, threshold=threshold,
        post_restoration_ratio=ratio(sum(r['macro']['household_bread'] for r in shock[12:]),
            sum(r['macro']['household_bread'] for r in control[12:])))


def main(directory, seeds):
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise ValueError('Нужны разные миры, а не повторное использование одного seed')
    result, all_rows = {}, {}
    checkpoints, fingerprints = {}, set()
    for variant in ('control', 'input_bid', 'shock', 'cash_return'):
        for seed in seeds:
            folder = directory/f'{variant}-{seed}'
            metadata = json.loads((folder/'metadata.json').read_text())
            if metadata['variant'] != variant:
                raise ValueError('Вариант не соответствует каталогу')
            fingerprints.add(metadata['engine']['fingerprint'])
            if variant == 'control':
                checkpoints[seed] = metadata['checkpoint_sha256']
            elif metadata['checkpoint_sha256'] != checkpoints[seed]:
                raise ValueError('Парные прогоны начаты из разных состояний')
            if metadata['status'] != 'completed' or metadata['completed'] != 60:
                raise ValueError('Незавершённый прогон: '+str(folder))
            if variant == 'control' and not metadata.get('observer_state_parity'):
                raise ValueError('Нет проверки невмешательства наблюдателя')
            rows = [json.loads(line) for line in (folder/'monthly.jsonl').read_text().splitlines()]
            if len(rows) != 60 or not all(all(r['invariants'].values()) for r in rows):
                raise ValueError('Ошибочные или неполные результаты')
            result[f'{variant}-{seed}'] = summarize(rows)
            all_rows[variant, seed] = rows
    if len(set(checkpoints.values())) != len(seeds) or len(fingerprints) != 1:
        raise ValueError('Повторён один мир или смешаны разные базовые движки')
    paired = []
    for seed in seeds:
        before, after = result[f'control-{seed}'], result[f'input_bid-{seed}']
        paired.append(dict(seed=seed, baseline=before['macro']['basic_coverage'],
            treatment=after['macro']['basic_coverage'],
            difference=after['macro']['basic_coverage']-before['macro']['basic_coverage'],
            bread_before=before['macro']['household_bread'], bread_after=after['macro']['household_bread']))
        result[f'shock-{seed}']['adaptation'] = recovery(all_rows['control', seed], all_rows['shock', seed])
    output = dict(seeds=seeds, window='relative 31..60 / economy 211..240', series=result,
        paired=paired, primary_test=paired_test([p['difference'] for p in paired]))
    output['secondary_cash_test'] = paired_test([result[f'cash_return-{seed}']['macro']['basic_coverage']
        -result[f'control-{seed}']['macro']['basic_coverage'] for seed in seeds])
    output['holm_adjusted_p'] = holm(dict(input_bid=output['primary_test']['p_two_sided'],
        cash_return=output['secondary_cash_test']['p_two_sided']))
    (directory/'summary.json').write_text(json.dumps(output, ensure_ascii=False, indent=2))
    with (directory/'paired.csv').open('w') as target:
        writer = csv.DictWriter(target, fieldnames=paired[0].keys(), lineterminator='\n')
        writer.writeheader()
        writer.writerows(paired)
    with (directory/'metrics.csv').open('w') as target:
        writer = csv.writer(target, lineterminator='\n')
        writer.writerow(('series', 'block', 'metric', 'value'))
        for series, values in result.items():
            for block in ('micro', 'macro'):
                for key, value in values[block].items():
                    if not isinstance(value, dict):
                        writer.writerow((series, block, key, value))
            for good, metrics in values['goods'].items():
                for key, value in metrics.items():
                    writer.writerow((series, good, key, value))
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=len(seeds), cols=1, subplot_titles=[f'Seed {seed}' for seed in seeds])
    colors = dict(control='#1565c0', input_bid='#2e7d32', shock='#ef6c00', cash_return='#7b1fa2')
    for index, seed in enumerate(seeds, 1):
        for variant in ('control', 'input_bid', 'shock', 'cash_return'):
            rows = all_rows[variant, seed]
            fig.add_trace(go.Scatter(x=[r['relative_month'] for r in rows],
                y=[r['macro']['basic_coverage'] for r in rows], name=variant,
                legendgroup=variant, showlegend=index == 1, line=dict(color=colors[variant])), row=index, col=1)
        fig.add_vrect(x0=7, x1=12, fillcolor='orange', opacity=.12, line_width=0, row=index, col=1)
        fig.update_yaxes(title_text='Базовая корзина, доля', range=[0, 1], row=index, col=1)
    fig.update_layout(height=300*len(seeds), template='plotly_white', title='Наблюдение, изменение лимитов сырья и временный шок спроса')
    fig.write_html(directory/'research.html', include_plotlyjs=True)
    print(json.dumps(output['primary_test']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--seeds', nargs='+', type=int, default=[73129, 17, 2026])
    args = parser.parse_args()
    main(args.directory, args.seeds)
