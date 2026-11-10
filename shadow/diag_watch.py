"""Перехват match: что видит заявка на зерно в момент исполнения.

Оборачивает Economy.match и для заявок на зерно записывает: цену заявки,
первые landed-цены книги, остатки офферов и итоговый код причины. Показывает,
отклонено по цене или из-за исчерпания книги.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit'))

from shadow import sandbox_mp  # noqa: E402

sandbox_mp.enable_multiprocessing()

from concurrent.futures import ProcessPoolExecutor  # noqa: E402

from modules.economy import Economy  # noqa: E402
import scenario_runner as sr  # noqa: E402
from shadow.turn import AgentTurn  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--warmup', type=int, default=8)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--good', default='grain')
    args = parser.parse_args()

    work = ROOT / 'tmp' / ('watch-%d' % args.seed)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    db, service = sr.build_world(args.seed, work, report_detail='none')
    try:
        for month in range(args.warmup):
            service.advance_turn(command_id='wa-warm-%d' % month, source='audit')

        records = []
        original = Economy.match

        def watching(self, buy, sells, local_only=False):
            snapshot = None
            if buy is not None and buy.get('good') == args.good and buy['side'] == 'buy':
                try:
                    options = list(self.offers(self.accounts[buy['participant']],
                                               buy['good'], sells))
                except Exception:
                    options = []
                snapshot = dict(participant=buy['participant'], price=buy['price'],
                                quantity=buy['quantity'], remaining=buy['remaining'],
                                reserved=buy['reserved'],
                                purpose=buy['purpose'],
                                options=[(round(price, 6), round(sell['price'], 6),
                                          round(sell['remaining'], 4), sell['participant'])
                                         for price, sell, _ in options[:6]],
                                options_total=len(options))
            result = original(self, buy, sells, local_only)
            if snapshot is not None:
                snapshot.update(after_remaining=buy['remaining'],
                                reason=buy.get('reason', ''), status=buy.get('status', ''))
                records.append(snapshot)
            return result

        Economy.match = watching
        try:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                engine = Economy.load(db)
                turn = AgentTurn(pool=pool, batch=32)
                engine.step(enterprise_planner=turn)
        finally:
            Economy.match = original

        print('PARAMS: seed=%d warmup=%d workers=%d good=%s, месяц %d'
              % (args.seed, args.warmup, args.workers, args.good, engine.meta['month']))
        print('перехвачено заявок на %s: %d' % (args.good, len(records)))
        print()
        failed = [r for r in records if r['after_remaining'] > 1e-6]
        print('не исполнено полностью: %d' % len(failed))
        for row in failed[:5]:
            print()
            print('  %s: q=%.2f price=%.6f remaining=%.4f reserved=%.2f -> reason=%s'
                  % (row['participant'], row['quantity'], row['price'],
                     row['after_remaining'], row['reserved'], row['reason']))
            print('    книга (landed, цена продавца, остаток, продавец):')
            for option in row['options']:
                print('      landed=%.6f  price=%.6f  remaining=%.2f  %s' % option)
            print('    всего офферов %d' % row['options_total'])
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
