"""Баланс товара на чистом V3: проверка модели учёта до правки агентной модели."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr
from modules.economy import Economy
from modules.economy.v3 import monthly_phase
from shadow import goods_balance

seed, months = 73129, 6
work = ROOT / 'tmp' / 'balance-v3'
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True, exist_ok=True)
db, service = sr.build_world(seed, work, report_detail='none')

print('PARAMS: seed=%d months=%d detail=none' % (seed, months))
try:
    bad_months = 0
    for month in range(months):
        engine = Economy.load(db)
        before = goods_balance.totals(engine)
        # Фазу считаем в памяти, чтобы получить отчёт: в БД он не сохраняется.
        engine.meta['month'] += 1
        report = engine.step()
        rows = goods_balance.check(engine, before, report)
        if rows:
            bad_months += 1
            print('месяц %d: расхождений %d -> %s'
                  % (engine.meta['month'], len(rows), goods_balance.describe(engine, rows)))
            for row in rows[:3]:
                print('   %-10s delta=%+.6f (start %.2f produced %.2f inputs %.2f '
                      'consumed %.2f construction %.2f wear %.2f end %.2f)'
                      % (row['good'], row['delta'], row['start'], row['produced'],
                         row['inputs_used'], row['consumed'], row['construction'],
                         row['wear'], row['end']))
        else:
            print('месяц %d: баланс сошёлся' % engine.meta['month'])
        engine.save(db, include_month=True)
    print('ИТОГ: месяцев с расхождением %d из %d' % (bad_months, months))
finally:
    db.close()
    shutil.rmtree(work, ignore_errors=True)
