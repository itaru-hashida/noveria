"""Компактный gzip-журнал питания из доверенного checkpoint, без полного лога рынка."""
import argparse
import gzip
import hashlib
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.economy import Economy
from core.lab import engine_version


def context(engine):
    return dict(type='context', schema=1, seed=engine.seed, config=engine.cfg,
        tiles=list(engine.tiles.values()),
        households=[dict(id=a['id'], state=a['state_id'], province=a['province_id'],
            tile=a['tile_id'], population=engine.population(a))
            for a in engine.accounts.values() if a['kind'] == 'household'])


def summarize(path):
    totals = {}; months = 0; contexts = set()
    with gzip.open(path, 'rt', encoding='utf-8') as source:
        for line in source:
            row = json.loads(line)
            if row['type'] == 'context':
                contexts.add(row['id'])
                continue
            if row['type'] != 'month' or row['context'] not in contexts:
                raise ValueError('Нет контекста диагностического месяца')
            months += 1
            for good, values in row['totals'].items():
                total = totals.setdefault(good, {k: 0. for k in values})
                for k, v in values.items():total[k] += v
    return dict(months=months, contexts=len(contexts), totals=totals, gzip_bytes=Path(path).stat().st_size)


def run(checkpoint, output, months):
    if not 1 <= months <= 10000:
        raise ValueError('Нужно 1..10000 месяцев')
    source = Path(checkpoint)
    engine = pickle.loads(source.read_bytes())
    engine.__class__ = Economy
    engine.FOOD_DIAGNOSTICS = True
    checkpoint_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    build = engine_version()
    build['diagnostics_sha256'] = hashlib.sha256(
        (Path(__file__).resolve().parents[1]/'modules/economy/food_diagnostics.py').read_bytes()).hexdigest()
    build['exporter_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    last_context = None
    with Path(output).open('xb') as raw:
        with gzip.open(raw, 'wt', encoding='utf-8', compresslevel=6) as target:
            for _ in range(months):
                report = engine.step()
                ctx = context(engine)
                ctx['engine'] = build
                encoded = json.dumps(ctx, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                identity = hashlib.sha256(encoded.encode()).hexdigest()
                if identity != last_context:
                    ctx.update(id=identity, checkpoint_sha256=checkpoint_hash)
                    target.write(json.dumps(ctx, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n')
                    last_context = identity
                row = dict(report['food_diagnostics'], type='month', context=identity)
                target.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n')
    return summarize(output)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--checkpoint', help='Только собственный доверенный pickle')
    source.add_argument('--inspect', help='Прочитать существующий .jsonl.gz без симуляции')
    p.add_argument('--out')
    p.add_argument('--months', type=int, default=1)
    args = p.parse_args()
    if args.checkpoint and not args.out:p.error('Нужен --out')
    result = summarize(args.inspect) if args.inspect else run(args.checkpoint, args.out, args.months)
    print(json.dumps(result, ensure_ascii=False, indent=2))
