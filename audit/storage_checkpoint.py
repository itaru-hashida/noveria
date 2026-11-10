"""Проверка хранения экономики: состояние в памяти, checkpoint в Parquet.

Скрипт отвечает на вопросы задания числами, а не словами:

1. два хода используют один объект `EconomyState`;
2. в обычном ходу нет записи динамической экономики в Ladybug;
3. восстановление из Parquet продолжает игру с тем же состоянием;
4. деньги, товары и резервы сходятся бит-в-бит;
5. replay после checkpoint совпадает с непрерывным прогоном;
6. Ladybug содержит только структурные данные и связи;
7. 12 и 36 месяцев проходят без полной загрузки/записи экономики через Ladybug.

Запуск:
    python audit/storage_checkpoint.py --seed 73129 --months 12
    python audit/storage_checkpoint.py --seed 73129 --months 36 --keep
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

import scenario_runner as sr  # noqa: E402
from core.db import WorldDB  # noqa: E402
from core.economy_state import EconomyState, checkpoint_size  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.world_service import WorldService  # noqa: E402

DYNAMIC_TABLES = ('EconomyAccount', 'Enterprise', 'EconomyMeta', 'EconomyMonth')
DYNAMIC_PREFIXES = ('firm-', 'treasury:', 'merchant:', 'private:', 'municipal:', 'carrier:')


def state_fingerprint(state):
    """Полный отпечаток экономики: деньги, товары, резервы, предприятия, RNG.

    Значения приводятся к каноническому JSON (`sort_keys`), а не к `repr`:
    порядок ключей словаря после загрузки из Parquet отличается от живого, и
    `repr` показал бы расхождение там, где данных разных нет. Числа пишутся
    через `repr(float)`, чтобы отпечаток ловил различие в последнем разряде.
    """
    def numbers(mapping):
        return {key: repr(value) for key, value in sorted(mapping.items())}

    def canonical(value):
        if isinstance(value, dict):
            return {str(key): canonical(item) for key, item in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        if isinstance(value, float):
            return repr(value)
        return value

    engine = state.engine
    payload = dict(
        month=engine.meta['month'],
        gold=repr(engine.gold()),
        accounts={a['id']: dict(kind=a['kind'], balance=repr(a['balance']),
                                state_id=a['state_id'], province_id=a['province_id'],
                                city_id=a['city_id'], tile_id=a['tile_id'],
                                group_id=a.get('group_id'),
                                reserved=repr(a.get('reserved', 0.0)),
                                protected=repr(a.get('protected', 0.0)),
                                stock=numbers(a['stock']),
                                values=numbers(a.get('values', {})),
                                stock_reserved=numbers(a.get('stock_reserved', {})),
                                project=canonical(a.get('project')),
                                public_projects=canonical(a.get('public_projects')))
                  for a in engine.accounts.values()},
        enterprises={e['id']: dict(good=e['good'], status=e['status'], owner=e['owner_id'],
                                   capacity=repr(e['capacity']), price=repr(e['price']),
                                   output=repr(e['output']), available_from=e.get('available_from'),
                                   sales_forecast=repr(e.get('sales_forecast')),
                                   sales_history=canonical(e.get('sales_history')),
                                   production_plan=canonical(e.get('production_plan')))
                     for e in engine.enterprises.values()},
        population={e['id']: e['properties']['population'] for e in engine.entities.values()
                    if e['kind'] == 'SocialGroup' and 'population' in e['properties']},
        market_history=canonical(engine.meta.get('market_history')),
        rng=repr(engine.rng.getstate()),
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def conservation(state):
    """Деньги и товары: сумма по счетам, резервы и их разность с обязательствами."""
    engine = state.engine
    reserved = sum(a.get('reserved', 0.0) for a in engine.accounts.values())
    protected = sum(a.get('protected', 0.0) for a in engine.accounts.values())
    stock_reserved = {}
    for account in engine.accounts.values():
        for good, quantity in account.get('stock_reserved', {}).items():
            stock_reserved[good] = stock_reserved.get(good, 0.0) + quantity
    return dict(gold=repr(engine.gold()), reserved=repr(reserved), protected=repr(protected),
                initial_gold=repr(engine.meta.get('initial_gold')),
                stock={g: repr(q) for g, q in sorted(engine.total_stock().items())},
                stock_reserved={g: repr(q) for g, q in sorted(stock_reserved.items())})


def dynamic_node_count(db):
    """Сколько узлов Ladybug описывают динамическую экономику, а не структуру."""
    counts = db.counts()
    offenders = {f'{table}': table for table in DYNAMIC_TABLES if table in counts}
    for entity in db.snapshot()['entities']:
        if entity['id'].startswith(DYNAMIC_PREFIXES):
            offenders[entity['id']] = entity['kind']
    return offenders, counts


def run_baseline(seed, months, work, detail):
    """Непрерывный прогон: N ходов на одном объекте состояния."""
    db, service = sr.build_world(seed, work, report_detail=detail)
    state = service.economy_state()
    times, marks, conservation_marks = [], [], []
    try:
        for step in range(1, months + 1):
            started = time.perf_counter()
            service.advance_turn(command_id=f'continuous-{step}', source='audit')
            times.append(time.perf_counter() - started)
            marks.append(state_fingerprint(state))
            conservation_marks.append(conservation(state))
        # Проверка «один объект на два хода»: тот же state и тот же движок.
        same_object = service.economy_state() is state
        checkpoint = work / 'economy'
        started = time.perf_counter()
        state.save_checkpoint(checkpoint)
        seconds = time.perf_counter() - started
        errors = []
        try:
            db.economy_records('EconomyAccount')
        except RuntimeError as error:
            errors.append(str(error))
        return dict(state=state, db=db, service=service, times=times, marks=marks,
                    conservation=conservation_marks, same_object=same_object,
                    checkpoint=checkpoint, checkpoint_seconds=seconds,
                    db_errors=errors)
    except BaseException:
        db.close()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    parser.add_argument('--split', type=int, default=None,
                        help='после скольких месяцев снять checkpoint (по умолчанию половина)')
    parser.add_argument('--detail', default='none')
    parser.add_argument('--keep', action='store_true', help='не удалять каталог прогона')
    args = parser.parse_args()
    months = args.months
    split = args.split if args.split is not None else max(1, months // 2)

    work = ROOT / 'tmp' / 'storage_checkpoint'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    print('ПАРАМЕТРЫ: seed=%d months=%d split=%d detail=%s' % (args.seed, months, split, args.detail))

    continuous_dir = work / 'continuous'
    continuous_dir.mkdir(parents=True, exist_ok=True)
    baseline = run_baseline(args.seed, months, continuous_dir, args.detail)
    db = baseline['db']
    try:
        print('\n== 1. Один объект EconomyState на все ходы ==')
        print('тот же объект после %d ходов: %s' % (months, baseline['same_object']))

        print('\n== 2. В обычном ходу нет записи динамики в Ladybug ==')
        for message in baseline['db_errors']:
            print('таблица недоступна:', message)
        offenders, counts = dynamic_node_count(db)
        print('узлы Ladybug по типам:', dict(sorted(counts.items())))
        print('динамических узлов экономики:', offenders or 'нет')

        print('\n== 6. Ladybug содержит только структурные данные ==')
        structural = {k: v for k, v in counts.items()
                      if k in ('State', 'Province', 'City', 'Character', 'SocialGroup',
                               'Army', 'Institution', 'Event')}
        print('структурные типы:', structural)

        print('\n== 5. Replay после checkpoint ==')
        checkpoint = baseline['checkpoint']
        size = checkpoint_size(checkpoint)
        print('checkpoint: %.2f МиБ в %d таблицах (%.0f мс записи)'
              % (size['total'] / 1024 / 1024, len(size['files']), baseline['checkpoint_seconds'] * 1000))
        for name, value in sorted(size['files'].items(), key=lambda kv: -kv[1]):
            print('   %-26s %8.1f KiB' % (name, value / 1024))

        # Непрерывный прогон уже ушёл дальше split: восстанавливаемся из
        # checkpoint-копии, снятой на момент split, и повторяем остаток.
        split_dir = work / 'split'
        split_dir.mkdir(parents=True, exist_ok=True)
        split_db, split_service = sr.build_world(args.seed, split_dir, report_detail=args.detail)
        split_state = split_service.economy_state()
        try:
            for step in range(1, split + 1):
                split_service.advance_turn(command_id=f'split-{step}', source='audit')
            split_marks = [state_fingerprint(split_state)]
            split_checkpoint = work / 'split_economy'
            split_state.save_checkpoint(split_checkpoint)
            split_seconds = split_state.checkpoint_seconds
            split_conservation = conservation(split_state)
        finally:
            split_db.close()

        with WorldDB(split_dir / 'world.lbug') as replay_db:
            replay_service = WorldService(replay_db, EventBus())
            replay_state = EconomyState.load_checkpoint(split_checkpoint,
                                                        replay_service.world_graph())
            replay_service.attach_economy(replay_state)
            loaded_mark = state_fingerprint(replay_state)
            loaded_conservation = conservation(replay_state)
            for step in range(1, months - split + 1):
                replay_service.advance_turn(command_id=f'split-{split + step}', source='audit')
                split_marks.append(state_fingerprint(replay_state))

        print('состояние на момент checkpoint совпало после загрузки: %s'
              % (loaded_mark == split_marks[0]))
        print('деньги/товары/резервы после загрузки совпали: %s'
              % (loaded_conservation == split_conservation))
        # split_marks[0] — состояние на момент checkpoint, а baseline['marks'][i]
        # — состояние после хода i+1. Поэтому сравниваются хвосты без первого.
        mismatches = [index + split + 1 for index, (left, right)
                      in enumerate(zip(split_marks[1:], baseline['marks'][split:]))
                      if left != right]
        print('replay: совпало %d из %d месяцев, расхождения: %s'
              % (months - split - len(mismatches), months - split, mismatches or 'нет'))

        print('\n== 4. Деньги, товары и резервы ==')
        print('непрерывный прогон, конец:', baseline['conservation'][-1])
        print('replay, конец:', conservation(replay_state))

        print('\n== 7. Ходы без загрузки/записи через Ladybug ==')
        print('checkpoint размера %.2f МиБ записан %.0f мс'
              % (size['total'] / 1024 / 1024, split_seconds * 1000))
        print('среднее время хода: %.3f s (непрерывный)'
              % (sum(baseline['times']) / len(baseline['times'])))
    finally:
        db.close()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print('\nкаталог прогона: %s' % work)


if __name__ == '__main__':
    main()
