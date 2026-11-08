"""Прогоны для аудита саморегуляции экономики (код движка не меняется).

Шоки подмешиваются обёрткой над Economy.sync_locations на время прогона:
  route_cut  — из графа убираются рёбра, связывающие провинцию с остальным миром;
  halt       — предприятия провинции помечаются suspended (производство и продажа стоят).
Население, деньги, ресурсы и запасы остаются; после снятия шока ничего не чинится вручную.

Запуск: python audit/scenario_runner.py --seed 17 --months 36 --scenario baseline --out audit/_runs
"""
import argparse
import csv
import json
from concurrent.futures import ProcessPoolExecutor
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db import WorldDB  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.world_service import WorldService  # noqa: E402
from loader import load_world  # noqa: E402
from modules.economy import Economy  # noqa: E402

SHOCK = dict(kind=None, province=None, start=0, end=0)


def install_shock():
    original = Economy.sync_locations

    def patched(self):
        original(self)
        m = self.meta['month']
        if SHOCK['kind'] and SHOCK['start'] <= m <= SHOCK['end']:
            pid = SHOCK['province']
            if SHOCK['kind'] == 'halt':
                for e in self.enterprises.values():
                    if e['province_id'] == pid:
                        e['suspended'] = True
            elif SHOCK['kind'] == 'route_cut' and m == SHOCK['start']:
                inside = {t['id'] for t in self.tiles.values() if t['province_id'] == pid}
                cut = [(a, b) for a, b in list(self.graph.edges) if (a in inside) != (b in inside)]
                self.graph.remove_edges_from(cut)
                self.invalidate_routes()
    Economy.sync_locations = patched


def build_world(seed, directory, report_detail='deep'):
    content = json.loads((ROOT/'content/initial_world.json').read_text(encoding='utf-8'))
    content['generation']['seed'] = seed
    scenario = Path(directory)/'scenario.json'
    scenario.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
    db = WorldDB(Path(directory)/'world.lbug')
    load_world(db, scenario)
    service = WorldService(db, EventBus())
    service.initialize_economy()
    engine = Economy.load(db)
    engine.meta['report_detail'] = report_detail
    engine.save(db, include_month=False)
    return db, service


def row_for(engine, report, scenario, seed):
    kinds = report['balances']
    prov = report['provinces']
    pop = sum(p['population'] for p in prov)
    need_b = {g: sum(p['deficits'].get(g, 0) for p in prov) for g in engine.cfg['basic_needs']}
    firms = engine.enterprises.values()
    projects = [a for a in engine.accounts.values() if a.get('project')]
    wage_flow = sum(f['gold'] for f in report['flows'] if f['reason'] == 'Заработная плата')
    trade_gold = sum(t['gold'] for t in report['trades'])
    freight = sum(t['freight'] for t in report['trades'])
    firm_cash = sum(a['balance'] for a in engine.accounts.values() if a['kind'] == 'enterprise')
    return dict(
        scenario=scenario, seed=seed, month=report['month'],
        population=round(pop), births=round(sum(p.get('births', 0) for p in prov), 2),
        deaths=round(sum(p.get('deaths', 0) for p in prov), 2),
        hunger_deaths=round(sum(p.get('hunger_deaths', 0) for p in prov), 2),
        population_change=round(sum(p.get('population_change', 0) for p in prov)),
        migration_in=round(sum(p.get('migration_in', 0) for p in prov)),
        migration_out=round(sum(p.get('migration_out', 0) for p in prov)),
        food_stress=round(sum(p.get('food_stress', 0)*p['population'] for p in prov)/pop, 6) if pop else 0,
        gold_total=round(report['gold_after'], 4),
        gold_household=round(kinds.get('household', 0), 2), gold_enterprise=round(kinds.get('enterprise', 0), 2),
        gold_owners=round(kinds.get('private', 0)+kinds.get('merchant', 0), 2),
        gold_treasury=round(kinds.get('treasury', 0), 2), gold_municipal=round(kinds.get('municipal', 0), 2),
        gold_carrier=round(kinds.get('carrier', 0), 2),
        active=report['active_enterprises'], open=report['open_enterprises'], total_firms=len(engine.enterprises),
        suspended=sum(bool(e.get('suspended')) for e in firms),
        closed_events=sum(l['action'] == 'closed' for l in report['lifecycle']),
        reopened_events=sum(l['action'] == 'reopened' for l in report['lifecycle']),
        opened_events=sum(l['action'] == 'opened' for l in report['lifecycle']),
        trades=len(report['trades']), trade_gold=round(trade_gold, 2), freight=round(freight, 2), wages=round(wage_flow, 2),
        prod_bread=round(report['production'].get('bread', 0), 2), prod_fish=round(report['production'].get('fish', 0), 2),
        prod_grain=round(report['production'].get('grain', 0), 2), prod_flour=round(report['production'].get('flour', 0), 2),
        prod_cloth=round(report['production'].get('cloth', 0), 2), prod_planks=round(report['production'].get('planks', 0), 2),
        prod_tools=round(report['production'].get('tools', 0), 2),
        cons_bread=round(report['consumed'].get('bread', 0), 2), cons_fish=round(report['consumed'].get('fish', 0), 2),
        deficit_bread=round(need_b.get('bread', 0), 2), deficit_fish=round(need_b.get('fish', 0), 2),
        welfare=round(sum(p['welfare']*p['population'] for p in prov)/pop, 2) if pop else 0,
        projects_open=len(projects), investments_started=sum(d.get('outcome') == 'started' for d in report['investment_decisions']),
        investments_built=sum(d.get('outcome') in ('built', 'expanded') for d in report['investment_decisions']),
        investments_cancelled=sum(d.get('outcome') == 'cancelled' for d in report['investment_decisions']),
        dividends=round(sum(d['dividends'] for d in report['dividends']), 2),
        firm_idle_cash=round(firm_cash, 2),
        goods_stock_bread=round(report['stocks'].get('bread', 0), 2), goods_stock_flour=round(report['stocks'].get('flour', 0), 2),
        goods_stock_grain=round(report['stocks'].get('grain', 0), 2),
        invariants_ok=all(report['invariants'].values()),
    )


def row_for_none(engine, scenario, seed):
    """Минимальная строка для скоростного прогона без EconomyMonth в БД."""
    population = sum(engine.population(a) for a in engine.accounts.values()
                     if a['kind'] == 'household')
    balances = defaultdict(float)
    for account in engine.accounts.values():
        balances[account['kind']] += account['balance']
    stocks = engine.total_stock()
    return dict(scenario=scenario, seed=seed, month=engine.meta['month'],
        population=round(population), gold_total=round(engine.gold(), 4),
        gold_household=round(balances['household'], 2), gold_enterprise=round(balances['enterprise'], 2),
        active=sum(e['status'] == 'active' for e in engine.enterprises.values()),
        open=sum(e['status'] != 'closed' for e in engine.enterprises.values()),
        total_firms=len(engine.enterprises), goods_stock_bread=round(stocks.get('bread', 0), 2),
        goods_stock_flour=round(stocks.get('flour', 0), 2),
        goods_stock_grain=round(stocks.get('grain', 0), 2), report_detail='none')


def run(seed, months, scenario, shock, out, events_path=None, report_detail='standard'):
    SHOCK.update(kind=shock[0] if shock else None, province=None, start=shock[2] if shock else 0,
                 end=shock[3] if shock else 0)
    rows = []
    events = []
    if events_path:
        events_path = Path(events_path)
        events_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db, service = build_world(seed, tmp, report_detail=report_detail)
        try:
            engine = Economy.load(db)
            if shock:
                SHOCK['province'] = shock[1] if shock[1] != 'auto' else choose_province(engine)
            for step in range(1, months+1):
                service.advance_turn(command_id=f'audit-{scenario}-{seed}-{step}', source='audit')
                engine = Economy.load(db)
                if report_detail == 'none':
                    r = row_for_none(engine, scenario, seed)
                else:
                    report = db.economy_month(engine.meta['month'])
                    if events_path:
                        events.extend(report.get('events', []))
                    r = row_for(engine, report, scenario, seed)
                r['shock_province'] = SHOCK['province'] if SHOCK['kind'] and SHOCK['start'] <= r['month'] <= SHOCK['end'] else ''
                rows.append(r)
                if step in (1, months):
                    print(scenario, seed, 'month', r['month'], 'gold', r['gold_total'], 'active', r['active'], flush=True)
        finally:
            db.close()
    if events_path:
        standard = ('event_id', 'month', 'event_type', 'entity_id', 'region_id', 'good',
                    'cause', 'source', 'parent_event_id', 'decision_id', 'project_id')
        fields = list(standard)+sorted({key for event in events for key in event if key not in standard})
        with events_path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(events)
    return rows


def run_worker(task):
    """Отдельный процесс = независимый мир и независимый RNG."""
    seed, months, scenario, shock, out, report_detail = task
    install_shock()
    return seed, run(seed, months, scenario, shock, out, report_detail=report_detail)


def choose_province(engine):
    """Провинция с наибольшим числом предприятий пищевой цепочки (зерно/мука/хлеб/рыба)."""
    count = defaultdict(int)
    for e in engine.enterprises.values():
        if e['good'] in ('grain', 'flour', 'bread', 'fish'):
            count[e['province_id']] += 1
    return max(sorted(count), key=lambda p: count[p])


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--seed', type=int, nargs='+', required=True)
    p.add_argument('--months', type=int, default=36)
    p.add_argument('--scenario', choices=['baseline', 'route_cut', 'halt'], default='baseline')
    p.add_argument('--shock-start', type=int, default=7)
    p.add_argument('--shock-end', type=int, default=12)
    p.add_argument('--province', default='auto')
    p.add_argument('--out', type=Path, default=ROOT/'audit/_runs')
    p.add_argument('--events', type=Path, help='CSV-файл причинных событий экономики')
    p.add_argument('--workers', type=int, default=1,
        help='Число процессов для независимых seed; один мир остаётся последовательным')
    p.add_argument('--report-detail', choices=['none', 'standard', 'deep'], default='standard',
        help='none: не писать EconomyMonth; standard: компактный отчёт; deep: полная реконструкция')
    a = p.parse_args()
    install_shock()
    shock = None if a.scenario == 'baseline' else (a.scenario, a.province, a.shock_start, a.shock_end)
    a.out.mkdir(parents=True, exist_ok=True)
    if a.events and len(a.seed) != 1:
        p.error('--events поддерживается только для одного seed')
    tasks = [(seed, a.months, a.scenario, shock, a.out, a.report_detail) for seed in a.seed]
    if a.workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=min(a.workers, len(tasks))) as pool:
            results = list(pool.map(run_worker, tasks))
    else:
        results = [(seed, run(seed, a.months, a.scenario, shock, a.out,
                              a.events, a.report_detail)) for seed in a.seed]
    for seed, rows in sorted(results):
        target = a.out/f'{a.scenario}_{seed}.csv'
        with target.open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print('written', target)
