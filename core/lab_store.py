"""Индексированное аналитическое хранилище; одна атомарная запись на месяц."""
import csv
import io
import json
import sqlite3
from pathlib import Path


DATASETS = ('overview', 'production', 'enterprises', 'accounts', 'stocks', 'orders',
            'trades', 'flows', 'geography', 'diagnostics', 'lifecycle')
FILTERS = ('state_id', 'province_id', 'good', 'entity_id', 'status')
METRICS = {
    'overview': ('active', 'closed', 'opened', 'closures', 'output', 'stock', 'sold', 'revenue',
                 'international_volume', 'domestic_volume', 'profit', 'unmet', 'consumed',
                 'household_gold', 'enterprise_gold', 'treasury_gold', 'private_gold', 'merchant_gold',
                 'wages', 'turnover', 'pending_projects', 'unmet_solvent', 'consumption_tax',
                 'dividends', 'working_reserve', 'project_reserve', 'owner_consumption_budget',
                 'owner_consumption_spent', 'owner_consumption_unspent', 'owner_unfilled_orders',
                 'owner_consumption_tax', 'owner_consumption_freight', 'dividends_private',
                 'dividends_merchant', 'dividends_treasury', 'dividends_municipal',
                 'money_share_household', 'money_share_enterprise', 'money_share_private',
                 'money_share_merchant', 'money_share_treasury', 'money_share_municipal', 'money_share_carrier'),
    'production': ('active', 'closed', 'opened', 'closures', 'output', 'stock', 'sold', 'profit', 'unmet', 'consumed', 'needed', 'capacity', 'production_cost'),
    'geography': ('production', 'consumed', 'unmet', 'imports', 'exports'),
    'enterprises': ('output', 'sold', 'revenue', 'expenses', 'profit', 'balance', 'capacity',
                    'dividends', 'working_reserve', 'project_reserve'),
}


class LabStore:
    def __init__(self, path, create=False):
        self.path = Path(path)
        if create:
            self.connection = sqlite3.connect(self.path, timeout=30)
            self.connection.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rows (
                    dataset TEXT, month INTEGER, ordinal INTEGER,
                    state_id TEXT, province_id TEXT, good TEXT, entity_id TEXT, status TEXT,
                    data TEXT NOT NULL, PRIMARY KEY(dataset,month,ordinal));
                CREATE INDEX IF NOT EXISTS by_location ON rows(dataset,state_id,province_id,month);
                CREATE INDEX IF NOT EXISTS by_good ON rows(dataset,good,month);
                CREATE INDEX IF NOT EXISTS by_entity ON rows(dataset,entity_id,month);
            ''')
        else:
            self.connection = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def metadata(self):
        row = self.connection.execute('SELECT data FROM metadata WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {}

    def save_metadata(self, metadata):
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO metadata VALUES (1,?)', (json.dumps(metadata, ensure_ascii=False),))

    def append_month(self, month, datasets, metadata):
        with self.connection:
            for dataset, rows in datasets.items():
                if dataset not in DATASETS:
                    raise ValueError('Неизвестный набор данных')
                self.connection.executemany('INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?)', [
                    (dataset, month, i, *(row.get(k) for k in FILTERS),
                     json.dumps(dict(row, month=month), ensure_ascii=False, allow_nan=False))
                    for i, row in enumerate(rows)])
            self.connection.execute('INSERT OR REPLACE INTO metadata VALUES (1,?)', (json.dumps(metadata, ensure_ascii=False),))

    def query(self, dataset, start=0, end=2**31-1, limit=500, offset=0, **filters):
        if dataset not in DATASETS or not 1 <= limit <= 10000 or offset < 0:
            raise ValueError('Некорректный запрос аналитики')
        clauses = ['dataset=?', 'month BETWEEN ? AND ?']
        params = [dataset, start, end]
        for key, value in filters.items():
            if key not in FILTERS:
                raise ValueError('Неизвестный фильтр')
            if value is not None:
                clauses.append(key+'=?')
                params.append(value)
        cursor = self.connection.execute('SELECT data FROM rows WHERE '+' AND '.join(clauses)+
                                         ' ORDER BY month,ordinal LIMIT ? OFFSET ?', (*params, limit, offset))
        return [json.loads(row[0]) for row in cursor]

    def choices(self, dataset, key):
        if dataset not in DATASETS or key not in FILTERS:
            raise ValueError('Некорректный справочник')
        return [r[0] for r in self.connection.execute(
            f'SELECT DISTINCT {key} FROM rows WHERE dataset=? AND {key} IS NOT NULL ORDER BY {key}', (dataset,))]

    def series(self, dataset, start=0, end=2**31-1, **filters):
        metrics = METRICS.get(dataset)
        if not metrics:
            raise ValueError('Нет агрегатов для набора данных')
        clauses = ['dataset=?', 'month BETWEEN ? AND ?']
        params = [dataset, start, end]
        for key, value in filters.items():
            if key not in FILTERS:
                raise ValueError('Неизвестный фильтр')
            if value is not None:
                clauses.append(key+'=?')
                params.append(value)
        fields = ','.join(f"SUM(json_extract(data,'$.{key}'))" for key in metrics)
        rows = self.connection.execute('SELECT month,'+fields+' FROM rows WHERE '+
            ' AND '.join(clauses)+' GROUP BY month ORDER BY month', params)
        return [dict(zip(('month', *metrics), row)) for row in rows]

    def order_trades(self, month, identity, limit=200, offset=0):
        rows = self.connection.execute('''SELECT data FROM rows WHERE dataset='trades' AND month=?
            AND (json_extract(data,'$.buy_order')=? OR json_extract(data,'$.sell_order')=?)
            ORDER BY ordinal LIMIT ? OFFSET ?''', (month, identity, identity, limit, offset))
        return [json.loads(row[0]) for row in rows]

    def export_csv(self, destination, dataset, **filters):
        # Постраничная выгрузка не держит торговую историю целиком в памяти.
        with Path(destination).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['month', 'data'])
            offset = 0
            while True:
                rows = self.query(dataset, limit=1000, offset=offset, **filters)
                if not rows:
                    break
                writer.writerows((r['month'], json.dumps(r, ensure_ascii=False)) for r in rows)
                offset += len(rows)


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    fields = list(dict.fromkeys(k for row in rows for k in row))
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    return stream.getvalue().encode('utf-8-sig')
