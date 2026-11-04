"""Сборка компактных доказательств аудита из CSV прогонов audit/_runs (код движка не читается и не меняется).

Запуск: python audit/build_evidence.py
Пишет audit/evidence.json и audit/evidence.csv.
"""
import csv
import json
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE/'_runs'
SEEDS = (17, 2026)
SCENARIOS = ('baseline', 'route_cut', 'halt')
WINDOWS = {'before_1_6': (1, 6), 'shock_7_12': (7, 12), 'after_13_18': (13, 18), 'late_19_36': (19, 36)}
METRICS = ('prod_bread', 'cons_bread', 'deficit_bread', 'deficit_fish', 'welfare', 'active', 'trades')


def load(scenario, seed):
    path = RUNS/f'{scenario}_{seed}.csv'
    if not path.exists():
        return None
    return {int(r['month']): r for r in csv.DictReader(path.open(encoding='utf-8'))}


def mean(rows, months, key):
    return st.mean(float(rows[m][key]) for m in months if m in rows)


def autocorr(x, k):
    m = st.mean(x)
    return sum((x[i]-m)*(x[i+k]-m) for i in range(len(x)-k))/sum((v-m)**2 for v in x)


def main():
    evidence = dict(method='36 месяцев, детерминированный RNG (seed карты = seed RNG), шок месяцы 7-12 в провинции с наибольшим числом предприятий пищевой цепочки',
                    caveat='Один прогон на сценарий и карту: это иллюстрация механизма, не статистическое доказательство. Шоковый и контрольный прогоны расходятся из-за разного потребления RNG.',
                    runs={}, shocks={}, structure={})
    rows_csv = []
    for seed in SEEDS:
        base = load('baseline', seed)
        if base is None:
            continue
        months = sorted(base)
        P = [float(base[m]['prod_bread']) for m in months][5:]
        C = [float(base[m]['cons_bread']) for m in months]
        Pall = [float(base[m]['prod_bread']) for m in months]
        # потребление хлеба домохозяйствами месяца m против выпуска m-1
        a, b = C[3:], Pall[2:-1]
        ma, mb = st.mean(a), st.mean(b)
        lag_corr = sum((x-ma)*(y-mb) for x, y in zip(a, b))/(sum((x-ma)**2 for x in a)**.5*sum((y-mb)**2 for y in b)**.5)
        evidence['structure'][str(seed)] = dict(
            bread_prod_cv_m6_36=round(st.pstdev(P)/st.mean(P), 3), bread_prod_min=min(P), bread_prod_max=max(P),
            bread_prod_autocorr_lag1=round(autocorr(P, 1), 2), bread_prod_autocorr_lag2=round(autocorr(P, 2), 2),
            corr_cons_m_vs_prod_m_minus_1=round(lag_corr, 3),
            mean_bread_deficit_m6_36=round(st.mean(float(base[m]['deficit_bread']) for m in months if m >= 6), 1),
            money_household=[round(float(base[1]['gold_household'])), round(float(base[36]['gold_household']))],
            money_treasury=[round(float(base[1]['gold_treasury'])), round(float(base[36]['gold_treasury']))],
            money_owners=[round(float(base[1]['gold_owners'])), round(float(base[36]['gold_owners']))],
            money_enterprise=[round(float(base[1]['gold_enterprise'])), round(float(base[36]['gold_enterprise']))],
            gold_total_constant=len({base[m]['gold_total'] for m in months}) == 1,
            closed_events=sum(int(base[m]['closed_events']) for m in months),
            reopened_events=sum(int(base[m]['reopened_events']) for m in months),
            invariants_ok=all(base[m]['invariants_ok'] == 'True' for m in months),
            source=f'audit/_runs/baseline_{seed}.csv')
        for scenario in SCENARIOS:
            rows = load(scenario, seed)
            if rows is None:
                continue
            province = next((r['shock_province'] for r in rows.values() if r['shock_province']), '')
            evidence['runs'][f'{scenario}_{seed}'] = dict(source=f'audit/_runs/{scenario}_{seed}.csv', shock_province=province,
                invariants_ok=all(r['invariants_ok'] == 'True' for r in rows.values()),
                suspended_max=max(int(r['suspended']) for r in rows.values()))
            if scenario == 'baseline':
                continue
            for wname, (lo, hi) in WINDOWS.items():
                ms = range(lo, hi+1)
                for metric in METRICS:
                    s, b = mean(rows, ms, metric), mean(base, ms, metric)
                    rows_csv.append(dict(seed=seed, scenario=scenario, window=wname, metric=metric,
                                         shock_run=round(s, 2), baseline=round(b, 2),
                                         ratio=round(s/b, 3) if b else '', source=f'audit/_runs/{scenario}_{seed}.csv'))
    for r in rows_csv:
        if r['metric'] == 'prod_bread':
            evidence['shocks'].setdefault(f"{r['scenario']}_{r['seed']}", {})[r['window']] = dict(ratio_to_baseline=r['ratio'])
    (HERE/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding='utf-8')
    with (HERE/'evidence.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['seed', 'scenario', 'window', 'metric', 'shock_run', 'baseline', 'ratio', 'source'])
        w.writeheader()
        w.writerows(rows_csv)
    print('written', len(rows_csv), 'rows')


if __name__ == '__main__':
    main()
