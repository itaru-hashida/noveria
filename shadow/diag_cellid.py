"""Есть ли cell_id у счетов и совпадает ли площадка снимка с площадкой заявки."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

import scenario_runner as sr  # noqa: E402
from modules.economy import Economy  # noqa: E402


def main():
    work = ROOT / 'tmp' / 'cellid'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(73129, work, report_detail='none')
    try:
        for month in range(3):
            service.advance_turn(command_id='ci-%d' % month, source='audit')
        engine = service.economy_state().engine
        with_cell = [a for a in engine.accounts.values() if a.get('cell_id')]
        print('PARAMS: seed=73129 months=3 detail=none')
        print('счетов всего %d, с cell_id %d' % (len(engine.accounts), len(with_cell)))
        diff = [a for a in with_cell if a['cell_id'] != a['tile_id']]
        print('у которых cell_id != tile_id: %d' % len(diff))
        for account in diff[:5]:
            print('  %s cell_id=%s tile_id=%s' % (account['id'], account['cell_id'],
                                                  account['tile_id']))
        # Сравнение distance для пары через market_tile и через tile_id.
        sample = diff[0] if diff else None
        if sample:
            other = next(a for a in engine.accounts.values()
                         if a['id'] != sample['id'] and a.get('cell_id'))
            print()
            print('пара %s -> %s' % (sample['id'], other['id']))
            print('  по cell_id:  %s' % (engine.landed(sample['cell_id'], other['cell_id'],
                                                       'grain', 0),))
            print('  по tile_id:  %s' % (engine.landed(sample['tile_id'], other['tile_id'],
                                                       'grain', 0),))
            print('  mixed:       %s' % (engine.landed(sample['tile_id'], other['cell_id'],
                                                       'grain', 0),))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
