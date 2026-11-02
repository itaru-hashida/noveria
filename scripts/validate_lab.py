"""Помесячная проверка двух архивов с ограниченной загрузкой торгового журнала."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import WorldDB
from core.lab import digest
from core.lab_store import LabStore


def validate(first, second):
    with LabStore(first/'analytics.sqlite') as a, LabStore(second/'analytics.sqlite') as b:
        left, right = a.metadata(), b.metadata()
        assert left['status'] == right['status'] == 'completed'
        assert left['completed'] == right['completed']
        assert left['initial_state_hash'] == right['initial_state_hash']
        months = left['completed']
        assert a.series('overview') == b.series('overview')
        assert a.series('production') == b.series('production')
        assert a.series('geography') == b.series('geography')
        hashes = []
        with WorldDB(first/'world.lbug', read_only=True) as one, WorldDB(second/'world.lbug', read_only=True) as two:
            assert one.economy_records('EconomyAccount') == two.economy_records('EconomyAccount')
            assert one.economy_records('Enterprise') == two.economy_records('Enterprise')
            for month in range(1, months+1):
                report = one.economy_month(month)
                actual_hash = digest(report)
                assert actual_hash == digest(two.economy_month(month))
                assert all(report['invariants'].values())
                observed = a.query('overview', start=month, end=month)[0]
                assert abs(observed['output']-sum(report['production'].values())) < 1e-7
                assert abs(observed['sold']-sum(t['quantity'] for t in report['trades'])) < 1e-7
                assert abs(observed['household_gold']-report['balances']['household']) < 1e-7
                hashes.append(actual_hash)
        counts = dict(a.connection.execute('SELECT dataset, COUNT(*) FROM rows GROUP BY dataset'))
        return dict(status='passed', months=months, initial_state_hash=left['initial_state_hash'],
            monthly_reports_identical=True, final_accounts_identical=True, final_enterprises_identical=True,
            monthly_aggregates_identical=True, all_engine_invariants=True,
            report_hashes=hashes,
            first=str(first), second=str(second), full_archive_rows=counts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('first', type=Path)
    parser.add_argument('second', type=Path)
    parser.add_argument('--output', type=Path, default=Path('archives/lab_validation.json'))
    args = parser.parse_args()
    result = validate(args.first, args.second)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PASSED: {result["months"]} months; reports, aggregates, accounts and enterprises match')
