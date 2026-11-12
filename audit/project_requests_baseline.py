"""Записать точную базовую границу V3 project_requests.

Скрипт не меняет экономику. Он нужен перед переносом investment-фазы в Cython:
фиксирует вход/выход project_requests, порядок решений и стоимость кандидатов.

    python audit/project_requests_baseline.py --seed 73129 --months 12
    python audit/project_requests_baseline.py --seed 17 --months 12 --out tmp/project_trace_17.json
"""
import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'audit'))

import scenario_runner as sr


def canonical(value):
    """JSON-форма нужного состояния с сохранением порядка экономических списков."""
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, tuple):
        return [canonical(item) for item in value]
    if isinstance(value, set):
        return [canonical(item) for item in sorted(value, key=repr)]
    if isinstance(value, float):
        return value.hex()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return repr(value)


def digest(value):
    encoded = json.dumps(canonical(value), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def phase_state(engine):
    """Все поля, которые должны совпасть на Cython-границе investment-фазы."""
    accounts = {}
    for account_id, account in engine.accounts.items():
        accounts[account_id] = {
            'balance': account['balance'],
            'reserved': account.get('reserved', 0),
            'protected': account.get('protected', 0),
            'operating_protected': account.get('operating_protected', 0),
            'payroll_protected': account.get('payroll_protected', 0),
            'stock': account.get('stock', {}),
            'stock_reserved': account.get('stock_reserved', {}),
            'project': account.get('project'),
        }
    enterprises = {
        enterprise_id: {
            key: enterprise.get(key) for key in (
                'status', 'closed', 'suspended', 'available_from', 'capacity', 'price',
                'production_price', 'planned_output', 'production_plan', 'unviable_months',
            )
        }
        for enterprise_id, enterprise in engine.enterprises.items()
    }
    report = engine.report
    return {
        'accounts': accounts,
        'enterprises': enterprises,
        'meta': {
            'month': engine.meta['month'],
            'investment_queue': engine.meta.get('investment_queue', []),
            'investment_dirty': engine.meta.get('investment_dirty', []),
            'investment_full_reconcile_month': engine.meta.get('investment_full_reconcile_month'),
        },
        'orders': report['orders'],
        'flows': report['flows'],
        'lifecycle': report['lifecycle'],
        'investment_review': report.get('investment_review'),
        'investment_candidates': report.get('investment_candidates', []),
        'investment_decisions': report.get('investment_decisions', []),
        'expansion_intents': report.get('expansion_intents', []),
        'expansion_intent_diagnostics': report.get('expansion_intent_diagnostics', []),
        'investment_candidate_statuses': report.get('investment_candidate_statuses', []),
        'investment_shadow_gate': report.get('investment_shadow_gate'),
        'investment_candidate_capacity_limits': report.get('investment_candidate_capacity_limits', []),
        'rng': repr(engine.rng.getstate()),
    }


def install_probe(engine, rows):
    original_project_requests = engine.project_requests
    original_candidate = engine.project_candidate
    candidate_seconds = []
    candidate_count = [0]

    def timed_candidate(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_candidate(*args, **kwargs)
        finally:
            candidate_count[0] += 1
            candidate_seconds.append(time.perf_counter()-started)

    def timed_project_requests(sells):
        candidate_count[0] = 0
        candidate_seconds.clear()
        before = phase_state(engine)
        started = time.perf_counter()
        result = original_project_requests(sells)
        elapsed = time.perf_counter()-started
        after = phase_state(engine)
        rows.append({
            'month': engine.meta['month'],
            'project_requests_seconds': elapsed,
            'candidate_count': candidate_count[0],
            'candidate_seconds': sum(candidate_seconds),
            'before_sha256': digest(before),
            'after_sha256': digest(after),
            'orders_added': len(after['orders'])-len(before['orders']),
            'projects_after': sum(account['project'] is not None for account in after['accounts'].values()),
            'decisions': len(after['investment_decisions']),
        })
        return result

    engine.project_candidate = timed_candidate
    engine.project_requests = timed_project_requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--out', type=Path, default=ROOT/'tmp'/'project_requests_baseline.json')
    args = parser.parse_args()
    if args.months < 1 or args.warmup < 0:
        raise SystemExit('months должен быть положительным, warmup — неотрицательным')
    work = ROOT/'tmp'/'project_requests_baseline'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        engine = service.economy_state().engine
        for _ in range(args.warmup):
            engine.step()
        rows = []
        install_probe(engine, rows)
        for _ in range(args.months):
            engine.step()
            engine.validate()
        result = {
            'seed': args.seed,
            'warmup': args.warmup,
            'months': args.months,
            'rows': rows,
            'mean_project_requests_ms': sum(row['project_requests_seconds'] for row in rows)*1000/len(rows),
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
