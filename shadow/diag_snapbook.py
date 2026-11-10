"""Видит ли планировщик живые офферы фирм: снимок против книги движка."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow import agent_snapshot  # noqa: E402
from shadow.runner import prepare_market  # noqa: E402


def main():
    work = ROOT / 'tmp' / 'snapbook'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(73129, work, report_detail='none')
    try:
        for month in range(8):
            service.advance_turn(command_id='sb-%d' % month, source='audit')
        engine = service.economy_state().engine
        sells = prepare_market(engine)
        budget = {f['id']: engine.free_cash(f['id']) for f in engine.enterprises.values()}
        arrays, meta = agent_snapshot.build(engine, budget, sells=sells)

        print('PARAMS: seed=73129 warmup=8 detail=none')
        for good in ('grain', 'flour', 'bread'):
            column = meta['good_index'][good]
            in_snapshot = int((arrays['offer_good'] == column).sum())
            in_engine = sum(1 for o in engine.report['orders']
                            if o['side'] == 'sell' and o['good'] == good
                            and o['status'] not in ('cancelled', 'expired'))
            from_firms = sum(1 for o in engine.report['orders']
                             if o['side'] == 'sell' and o['good'] == good
                             and o['participant'] in engine.enterprises
                             and o['status'] not in ('cancelled', 'expired'))
            print('%-6s офферов в книги движка %3d (из них фирм %3d), в снимке %3d'
                  % (good, in_engine, from_firms, in_snapshot))
            # Что видит планировщик как котировку входа.
            rows = [row for row, firm in enumerate(meta['firms'])
                    if firm == 'firm-00117']
            if rows and good == 'grain':
                print('       firm-00117 видит цену входа %.6f'
                      % arrays['agent_input_price'][rows[0], column])
                # Реальная лучшая landed для его площадки по книге движка.
                firm = engine.enterprises['firm-00117']
                account = engine.accounts['firm-00117']
                tile = engine.market_tile(account)
                best = None
                for order in engine.report['orders']:
                    if order['side'] != 'sell' or order['good'] != 'grain':
                        continue
                    quote = engine.landed(order['tile_id'], tile, 'grain', order['price'])
                    if quote and (best is None or quote[0] < best):
                        best = quote[0]
                print('       движок: лучшая landed по книге %.6f' % best)
        db.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
