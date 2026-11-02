"""Потоковый импорт известных CSV аудита без выдумывания отсутствующих полей."""
import csv
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

from core.lab import DEFAULT_RUNS, now
from core.lab_store import FILTERS, LabStore


FORMATS = [
    ('enterprises', {'scenario', 'month', 'enterprise_id', 'capacity_open', 'cash_end', 'output', 'profit'}),
    ('geography', {'scenario', 'month', 'province_id', 'population', 'deficits', 'consumption'}),
    ('production', {'scenario', 'month', 'good', 'sell_quantity', 'buy_unfilled', 'production'}),
    ('overview', {'scenario', 'month', 'active', 'total_gold', 'unmet_population', 'trade_volume'}),
]


def convert(value):
    if value == '':
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def import_csv(content, filename, runs=DEFAULT_RUNS):
    text = content.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text))
    fields = set(reader.fieldnames or [])
    dataset = next((d for d, required in FORMATS if required <= fields), None)
    if dataset is None:
        raise ValueError('Неизвестный CSV: поддерживаются scenario_monthly, industry_monthly, enterprise_monthly и scenario_provinces из экономического аудита')
    stores = {}
    directories = []
    try:
        for ordinal, original in enumerate(reader):
            row = {k: convert(v) for k, v in original.items() if k is not None}
            month = row.get('month')
            if not isinstance(month, int) or isinstance(month, bool) or month < 0 or month > 100000:
                raise ValueError('Некорректный месяц в CSV')
            scenario = str(row['scenario'])
            if scenario not in stores:
                identity = uuid4().hex
                directory = Path(runs)/identity
                directory.mkdir(parents=True, exist_ok=False)
                directories.append(directory)
                store = LabStore(directory/'analytics.sqlite', create=True)
                meta = dict(id=identity, name=f'{Path(filename).name}: {scenario}', created_at=now(),
                    status='importing', format_version=1, source=filename, scenario=scenario,
                    source_sha256=hashlib.sha256(content).hexdigest(), completed=0, months=0,
                    seed=None, detail='legacy', available_datasets=[dataset],
                    missing='Отсутствующие в CSV поля неизвестны; заявки и денежные потоки не восстанавливаются')
                store.save_metadata(meta)
                stores[scenario] = (store, meta)
            store, meta = stores[scenario]
            renames = {'production': 'output', 'unmet_population': 'unmet', 'trade_volume': 'sold',
                       'international_volume': 'international_volume', 'enterprise_id': 'entity_id',
                       'cash_end': 'balance', 'capacity_end': 'capacity'}
            if dataset == 'geography':
                renames = {}
                # В этом формате производство и дефицит — словари по товарам.
                row['unmet'] = sum((row.get('deficits') or {}).values())
                row['consumed'] = sum((row.get('consumption') or {}).values())
                row['production'] = sum((row.get('production') or {}).values())
            for old, new in renames.items():
                if old in row and new not in row:
                    row[new] = row[old]
            if dataset in ('overview', 'production') and 'closed' in row:
                # В старом аудите closed — число закрытий за месяц, а не запас закрытых фирм.
                row['closures'] = row.pop('closed')
            store.connection.execute('INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?)',
                (dataset, month, ordinal, *(row.get(k) for k in FILTERS), json.dumps(row, ensure_ascii=False)))
            meta['completed'] = max(meta['completed'], month)
            if ordinal % 1000 == 0:
                store.connection.commit()
        if not stores:
            raise ValueError('Пустой CSV')
        for store, meta in stores.values():
            store.connection.commit()
            meta.update(status='imported', months=meta['completed'])
            store.save_metadata(meta)
    except BaseException:
        for store, meta in stores.values():
            store.connection.rollback()
            meta.update(status='failed', error='Импорт CSV прерван; данные могут быть неполными')
            store.save_metadata(meta)
        raise
    finally:
        for store, _ in stores.values():
            store.__exit__()
    return directories
