"""Диагностика контура предприятия: выпуск, закупки, причины простоев."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp

sandbox_mp.enable_multiprocessing()

from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow.runner import prepare_market, run_shadow  # noqa: E402


def main(seed=73129, months=4):
    work = ROOT / 'tmp' / 'diag-loop'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(seed, work, report_detail='none')
    try:
        with ProcessPoolExecutor(max_workers=4) as pool:
            for month in range(months):
                engine = Economy.load(db)
                prepare_market(engine)
                parameters = (engine.cfg['transport_rate'], engine.cfg['wage'],
                              engine.cfg['consumption_tax'], engine.cfg['markup'],
                              engine.cfg.get('enterprise_overhead_rate', 0.0),
                              engine.cfg['labor_share'])
                run = run_shadow(engine, parameters, pool, batch=32)
                service.advance_turn(command_id='diag-%d' % month, source='audit')
        print('PARAMS: seed=%d months=%d detail=none batch=32 workers=4' % (seed, months))
        rows = run['production_rows']
        made = sum(row['output'] for row in rows)
        planned = sum(row['planned'] for row in rows)
        idle = [row for row in rows if row['output'] <= 1e-7]
        print('агентов с планом %d, выпуск %.2f из плана %.2f (%.1f%%)'
              % (len([r for r in rows if r['planned'] > 0]), made, planned,
                 100*made/planned if planned else 0))
        print('простой: %d агентов' % len(idle))
        reasons = {}
        for row in idle:
            reasons[row['reason']] = reasons.get(row['reason'], 0)+1
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
            print('   %-64s %d' % (reason, count))
        by_good = {}
        for row in rows:
            bucket = by_good.setdefault(row['good'], [0.0, 0.0])
            bucket[0] += row['planned']
            bucket[1] += row['output']
        print('выпуск по товарам (план -> факт):')
        for good, (plan_value, out) in sorted(by_good.items()):
            print('   %-10s %8.2f -> %8.2f' % (good, plan_value, out))
        print('произведено:', {k: round(v, 2) for k, v in sorted(run['produced'].items())})
        print('потреблено:', {k: round(v, 2) for k, v in sorted(run['consumed'].items())})
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
