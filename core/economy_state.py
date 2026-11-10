"""Экономическое состояние в памяти и его checkpoint в Parquet.

Почему так: экономика — единственная механика, которая меняется каждый месяц.
Пока её состояние лежало в Ladybug, каждый ход платил за чтение и запись всех
счетов, предприятий и отчёта (замер: 0,072 s чтение + 0,075 s запись при ходе
0,65 s), а история сохранений превращалась в набор несовместимых таблиц. Здесь
состояние живёт в процессе: мир загружается один раз, `EconomyState` создаётся
один раз, а ход только двигает его.

Parquet — единственный формат долговременного хранения экономики. Ladybug
остаётся графом мира: государства, провинции, города, клетки, дороги,
транспортный граф, структурные сущности и связи принадлежности. Динамических
экономических записей там больше нет, поэтому схема БД не совместима со
старыми сохранениями — это разрешённая миграция с удалением старого хранения.

Таблицы checkpoint:

    meta.parquet            параметры месяца, правила, конфиг
    accounts.parquet        счета: деньги, расположение, касса в резерве
    stocks.parquet          склады: товар и его учётная стоимость по счёту
    reserves.parquet        товарные резервы под заявки и стройки
    enterprises.parquet     предприятия
    forecasts.parquet       прогнозы продаж и история продаж
    plans.parquet           производственные планы, стройки, наблюдения
    population.parquet      демография: численность и благосостояние страт
    market_history.parquet  последние месяцы рынка (цена, объём, дефицит)
    trades.parquet          сделки текущего месяца
    reports.parquet         месячные отчёты (когда они запрошены)
    rng.parquet             состояние генератора
    schema.json             версия схемы, отпечаток мира, счётчики и размеры

Точность: деньги и товары возвращаются бит-в-бит. Числа пишутся в Parquet как
IEEE-754 double, остальные поля — JSON, который в Python сохраняет кратчайшее
обратное представление float. Поэтому сверка «до/после» идёт без допуска.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = 4

# Колонки, которые вынесены в отдельные таблицы: в payload счёта их быть не должно,
# иначе при восстановлении одно и то же поле пришло бы из двух источников.
ACCOUNT_SPLIT = ('stock', 'values', 'stock_reserved', 'stock_sales_history',
                 'project', 'public_projects')
ENTERPRISE_SPLIT = ('production_plan', 'production_observation', 'sales_history',
                    'sales_forecast', 'initial_sales_estimate')
META_SPLIT = ('market_history', 'rng_state')

TABLES = ('meta', 'accounts', 'stocks', 'reserves', 'enterprises', 'forecasts',
          'plans', 'population', 'market_history', 'trades', 'reports', 'rng')

SCHEMAS = {
    'meta': [('key', pa.string()), ('data', pa.string())],
    'accounts': [
        ('id', pa.string()), ('kind', pa.string()), ('balance', pa.float64()),
        ('state_id', pa.string()), ('province_id', pa.string()), ('city_id', pa.string()),
        ('tile_id', pa.string()), ('group_id', pa.string()),
        ('reserved', pa.float64()), ('protected', pa.float64()), ('payload', pa.string())],
    'stocks': [('account_id', pa.string()), ('good', pa.string()),
               ('quantity', pa.float64()), ('value', pa.float64()), ('data', pa.string())],
    'reserves': [('account_id', pa.string()), ('good', pa.string()),
                 ('quantity', pa.float64()), ('data', pa.string())],
    'enterprises': [
        ('id', pa.string()), ('good', pa.string()), ('status', pa.string()),
        ('owner_id', pa.string()), ('state_id', pa.string()), ('province_id', pa.string()),
        ('capacity', pa.float64()), ('price', pa.float64()), ('output', pa.float64()),
        ('payload', pa.string())],
    'forecasts': [
        ('owner', pa.string()), ('owner_kind', pa.string()), ('good', pa.string()),
        ('forecast', pa.float64()), ('initial_estimate', pa.float64()),
        ('history', pa.string())],
    'plans': [('owner', pa.string()), ('owner_kind', pa.string()),
              ('kind', pa.string()), ('data', pa.string())],
    'population': [('group_id', pa.string()), ('population', pa.float64()),
                   ('welfare', pa.float64()), ('data', pa.string())],
    'market_history': [
        ('month', pa.int64()), ('state_id', pa.string()), ('province_id', pa.string()),
        ('good', pa.string()), ('volume', pa.float64()), ('trade_gold', pa.float64()),
        ('freight', pa.float64()), ('requested', pa.float64()),
        ('unmet_solvent', pa.float64()), ('limit_value', pa.float64()),
        ('reserved_unmet', pa.float64()), ('data', pa.string())],
    'trades': [
        ('trade_id', pa.string()), ('month', pa.int64()), ('good', pa.string()),
        ('quantity', pa.float64()), ('price', pa.float64()), ('gold', pa.float64()),
        ('freight', pa.float64()), ('buyer', pa.string()), ('seller', pa.string()),
        ('buyer_state', pa.string()), ('seller_state', pa.string()),
        ('buyer_province', pa.string()), ('seller_province', pa.string()),
        ('purpose', pa.string()), ('data', pa.string())],
    'reports': [('month', pa.int64()), ('rules_version', pa.int64()), ('data', pa.string())],
    'rng': [('version', pa.int64()), ('state', pa.string()), ('gauss', pa.float64())],
}


def _dump(value):
    """JSON без потери float и без перестановки ключей.

    `sort_keys` здесь запрещён: порядок ключей — часть состояния. `config.goods`
    перебирается движком в порядке вставки, и сортировка при сохранении меняла
    бы порядок товаров, а с ним очередь торгов и причинных событий. Python
    сохраняет кратчайшее обратное представление float, поэтому числа
    возвращаются бит-в-бит.
    """
    return json.dumps(value, ensure_ascii=False)


def _load(text):
    return json.loads(text)


def _float(value):
    """Значение для типизированной колонки: None остаётся None, не число — 0.0."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None if value is None else 0.0
    return float(value)


def _write(path, rows, columns):
    """Писать таблицу построчно; пустая таблица сохраняет схему."""
    arrays = []
    for name, kind in SCHEMAS[columns]:
        arrays.append(pa.array([row.get(name) for row in rows], type=kind))
    pq.write_table(pa.table(dict(zip([name for name, _ in SCHEMAS[columns]], arrays))),
                   str(path), compression='zstd')


def _read(path, columns):
    if not Path(path).is_file():
        return []
    return pq.read_table(str(path)).to_pylist()


def world_fingerprint(world_graph):
    """Отпечаток структурного мира: набор сущностей и клеток.

    Экономику нельзя наложить на другой мир: счета ссылаются на города, клетки
    и страты. Дороги и стоимость клеток в отпечаток не входят отдельным хешем —
    они проверяются в `_check_world` и дают предупреждение, а не отказ.
    """
    entities = sorted((e['id'], e['kind']) for e in world_graph['snapshot']['entities'])
    tiles = sorted(t['id'] for t in world_graph['tiles'])
    digest = hashlib.sha256()
    for identity, kind in entities:
        digest.update(f'{identity}\t{kind}\n'.encode('utf-8'))
    for tile in tiles:
        digest.update(f'{tile}\n'.encode('utf-8'))
    return digest.hexdigest()


def _tiles_signature(tiles):
    return hashlib.sha256(_dump(sorted(
        (t['id'], bool(t.get('road')), bool(t.get('passable', True)), t.get('cost'))
        for t in tiles)).encode('utf-8')).hexdigest()


class EconomyState:
    """Живое экономическое состояние: один объект на мир, живёт между ходами."""

    def __init__(self, engine, world_graph, keep_reports=None):
        self.engine = engine
        self.world_graph = world_graph
        self.month = engine.meta.get('month', 0)
        self.last_report = None
        self.reports = []
        self.checkpoint_path = None
        self.checkpoint_seconds = None
        self._undo = None
        # Отчёты хранятся только для лаборатории и аудита: в игре detail=none,
        # и держать 2,8 МБ отчёта на месяц незачем — он никому не читается.
        self.keep_reports = (engine.report_detail() != 'none' if keep_reports is None
                             else keep_reports)

    # --- создание -----------------------------------------------------------

    @classmethod
    def create(cls, world_graph, config=None, accounts=None, enterprises=None, meta=None,
               keep_reports=None):
        """Новый мир: пустой движок, который при необходимости бутстрапится."""
        from modules.economy import Economy
        engine = Economy(world_graph['snapshot'], world_graph['tiles'],
                         accounts=accounts, enterprises=enterprises, meta=meta,
                         copy_state=False)
        if config is not None:
            engine.meta['config'] = config
            engine.cfg = config
            engine.goods = config['goods']
        return cls(engine, world_graph, keep_reports)

    @classmethod
    def load_checkpoint(cls, path, world_graph, strict=True):
        """Восстановить состояние из Parquet и продолжить игру с того же места."""
        path = Path(path)
        schema_path = path / 'schema.json'
        if not schema_path.is_file():
            raise ValueError(f'Нет checkpoint экономики: {schema_path}')
        schema = _load(schema_path.read_text(encoding='utf-8'))
        if schema.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('Checkpoint собран другой версией схемы: %s вместо %s'
                             % (schema.get('schema_version'), SCHEMA_VERSION))
        if strict and schema.get('world') != world_fingerprint(world_graph):
            raise ValueError('Checkpoint собран на другом мире: набор сущностей и клеток '
                             'не совпадает с переданным графом мира')

        meta = {row['key']: _load(row['data']) for row in _read(path / 'meta.parquet', 'meta')}
        if not meta:
            raise ValueError('В checkpoint нет параметров экономики')
        meta.setdefault('id', 'game')

        accounts = {}
        for row in _read(path / 'accounts.parquet', 'accounts'):
            payload = _load(row['payload']) if row['payload'] else {}
            # Колонки id/kind/... повторяются поверх payload только там, где они
            # есть в исходной записи: иначе восстановление добавило бы
            # `group_id: null` счёту, у которого этого поля не было.
            identity = dict(id=row['id'], kind=row['kind'], balance=row['balance'],
                            state_id=row['state_id'], province_id=row['province_id'],
                            city_id=row['city_id'], tile_id=row['tile_id'])
            if row['group_id'] is not None or payload.get('group_id') is not None:
                identity['group_id'] = row['group_id']
            for key, value in identity.items():
                if key not in payload:
                    payload[key] = value
            for field in ('reserved', 'protected'):
                if field not in payload:
                    payload[field] = row[field] or 0.0
            if 'stock_reserved' not in payload:
                payload['stock_reserved'] = {}
            payload.update(stock={}, values={})
            accounts[row['id']] = payload
        for row in _read(path / 'stocks.parquet', 'stocks'):
            account = accounts.setdefault(row['account_id'], dict(
                id=row['account_id'], stock={}, values={}))
            quantity, value = (_load(row['data']) if row['data']
                               else (row['quantity'], row['value'] or 0.0))
            account['stock'][row['good']] = quantity
            account['values'][row['good']] = value
        for row in _read(path / 'reserves.parquet', 'reserves'):
            account = accounts[row['account_id']]
            account.setdefault('stock_reserved', {})[row['good']] = (
                _load(row['data']) if row['data'] else row['quantity'])

        enterprises = {}
        for row in _read(path / 'enterprises.parquet', 'enterprises'):
            payload = _load(row['payload']) if row['payload'] else {}
            identity = dict(id=row['id'], good=row['good'], status=row['status'],
                            owner_id=row['owner_id'], state_id=row['state_id'],
                            province_id=row['province_id'], capacity=row['capacity'],
                            price=row['price'])
            for key, value in identity.items():
                if key not in payload:
                    payload[key] = value
            if 'output' not in payload:
                payload['output'] = row['output']
            enterprises[row['id']] = payload
        for row in _read(path / 'forecasts.parquet', 'forecasts'):
            history = _load(row['history']) if row['history'] else []
            if row['owner_kind'] == 'enterprise':
                enterprise = enterprises[row['owner']]
                enterprise['sales_forecast'] = row['forecast']
                enterprise['initial_sales_estimate'] = row['initial_estimate']
                enterprise['sales_history'] = history
            else:
                account = accounts[row['owner']]
                account.setdefault('stock_sales_history', {})[row['good']] = history
        for row in _read(path / 'plans.parquet', 'plans'):
            data = _load(row['data']) if row['data'] else None
            target = enterprises.get(row['owner']) if row['owner_kind'] == 'enterprise' else accounts.get(row['owner'])
            if target is not None:
                target[row['kind']] = data

        population = {row['group_id']: _load(row['data']) if row['data'] else dict(
                          population=row['population'], welfare=row['welfare'])
                      for row in _read(path / 'population.parquet', 'population')}
        engine = cls.create(world_graph, accounts=list(accounts.values()),
                            enterprises=list(enterprises.values()), meta=meta,
                            keep_reports=False).engine
        for group_id, properties in population.items():
            entity = engine.entities.get(group_id)
            if entity is None:
                continue
            entity['properties'].update(properties)

        engine.meta['market_history'] = []
        months = {}
        for row in _read(path / 'market_history.parquet', 'market_history'):
            # Строка — один рынок; месяц собирается в общий список, иначе
            # восстановление нарезало бы историю на месяцы по одной записи.
            month = months.get(row['month'])
            if month is None:
                month = months[row['month']] = dict(month=row['month'], markets=[])
                engine.meta['market_history'].append(month)
            month['markets'].append(_load(row['data']) if row['data'] else dict(
                state_id=row['state_id'], province_id=row['province_id'], good=row['good'],
                volume=row['volume'], trade_gold=row['trade_gold'], freight=row['freight'],
                requested=row['requested'], unmet_solvent=row['unmet_solvent'],
                limit_value=row['limit_value'], reserved_unmet=row['reserved_unmet']))

        rng_rows = _read(path / 'rng.parquet', 'rng')
        if rng_rows and rng_rows[0]['state']:
            row = rng_rows[0]
            engine.meta['rng_state'] = [row['version'],
                                        [int(value) for value in row['state'].split()],
                                        row['gauss']]
            engine.rng.setstate((row['version'],
                                 tuple(int(value) for value in row['state'].split()),
                                 row['gauss']))

        state = cls(engine, world_graph)
        state.checkpoint_path = path
        state.reports = [_load(row['data']) for row in _read(path / 'reports.parquet', 'reports')]
        state.last_report = state.reports[-1] if state.reports else None
        tiles_changed = schema.get('tiles') not in (None, _tiles_signature(world_graph['tiles']))
        if tiles_changed:
            # Дорогу могли построить после checkpoint. Экономика об этом не знает,
            # поэтому кэш маршрутов сбрасывается, а расхождение видно в отчёте.
            engine.invalidate_routes()
        return state

    # --- ход ----------------------------------------------------------------

    def begin_turn(self):
        """Копия изменяемой части перед ходом: откат хода должен вернуть экономику."""
        self._undo = self.engine.export_state()

    def rollback(self):
        if self._undo is not None:
            self.engine.restore_state(self._undo)
            self._undo = None
            self.month = self.engine.meta.get('month', 0)
            self.last_report = None
            if self.reports and self.reports[-1].get('month', 0) > self.month:
                self.reports.pop()

    def commit_turn(self):
        self._undo = None

    def step(self):
        report = self.engine.step()
        self.month = self.engine.meta.get('month', 0)
        self.last_report = report
        if self.keep_reports:
            self.reports.append(report)
        return report

    def bootstrap(self):
        self.engine.bootstrap()
        self.month = self.engine.meta.get('month', 0)
        return self

    @property
    def initialized(self):
        return bool(self.engine.accounts)

    def flush_structural(self, db):
        """Записать в Ladybug только то, что там по-прежнему живёт.

        Две вещи остаются структурными и потому пишутся в транзакции хода:

        * завершённые стройки — `Institution` и её связи с государством и городом;
        * численность и благосостояние страт — свойства `SocialGroup`, узла графа
          мира, а не динамическая экономическая запись. Без этой записи проверка
          целостности, интерфейс и лаборатория читали бы устаревшее население.

        Ни счетов, ни предприятий, ни отчётов здесь нет: они лежат в checkpoint.
        """
        pending = getattr(self.engine, 'pending_institutions', [])
        for institution in pending:
            if _exists(db, institution['id']):
                continue
            db.create_entity(institution['id'], 'Institution', institution['name'],
                             institution['properties'])
            db.put_link(institution['id'], institution['properties']['state_id'], 'institution_of')
            db.put_link(institution['id'], institution['properties']['city_id'], 'located_in')
        self.engine.pending_institutions = []
        # Пишутся только поля, которые меняет экономика. Лояльность и влияние
        # принадлежат миру: их выставляет игрок, и экономика о них не знает.
        db.update_social_groups({a['group_id']: self.engine.entities[a['group_id']]['properties']
            for a in self.engine.accounts.values()
            if a['kind'] == 'household' and a['group_id'] in self.engine.entities},
            fields=('population', 'welfare'))
        return len(pending)

    # --- checkpoint ---------------------------------------------------------

    def _meta_rows(self):
        return [dict(key=key, data=_dump(value))
                for key, value in sorted(self.engine.meta.items()) if key not in META_SPLIT]

    def _account_rows(self):
        """Счёт: типизированные колонки для чтения, payload — точный источник.

        Колонки нужны, чтобы checkpoint можно было смотреть и сверять запросом.
        Восстанавливается значение из payload: только JSON сохраняет различие
        между `0` и `0.0`, а движок ставит `protected = min(balance, 0)` целым
        нулём, и подмена на `0.0` сделала бы состояние не бит-в-бит тем же.
        """
        rows, stocks, reserves = [], [], []
        for account in self.engine.accounts.values():
            payload = {key: value for key, value in account.items() if key not in ACCOUNT_SPLIT}
            rows.append(dict(id=account['id'], kind=account.get('kind'),
                             balance=_float(account.get('balance')),
                             state_id=account.get('state_id'), province_id=account.get('province_id'),
                             city_id=account.get('city_id'), tile_id=account.get('tile_id'),
                             group_id=account.get('group_id'),
                             reserved=_float(account.get('reserved', 0.0)),
                             protected=_float(account.get('protected', 0.0)),
                             payload=_dump(payload)))
            for good, quantity in sorted(account.get('stock', {}).items()):
                # Склад пишется целиком, включая точный тип значения: `852` и
                # `852.0` движок различает, и подмена сломала бы сверку.
                stocks.append(dict(account_id=account['id'], good=good,
                                   quantity=_float(quantity),
                                   value=_float(account.get('values', {}).get(good, 0.0)),
                                   data=_dump([quantity,
                                               account.get('values', {}).get(good)])))
            for good, quantity in sorted(account.get('stock_reserved', {}).items()):
                # Нулевые резервы пишутся тоже: движок заводит ключ на каждый
                # товар, а «ключа нет» и «резерв ноль» — разные состояния.
                reserves.append(dict(account_id=account['id'], good=good,
                                     quantity=_float(quantity),
                                     # Точное значение резерва: `0` и `0.0` в
                                     # движке различаются типом, а JSON их хранит.
                                     data=_dump(quantity)))
        return rows, stocks, reserves

    def _enterprise_rows(self):
        rows = []
        for enterprise in self.engine.enterprises.values():
            payload = {key: value for key, value in enterprise.items() if key not in ENTERPRISE_SPLIT}
            rows.append(dict(id=enterprise['id'], good=enterprise.get('good'),
                             status=enterprise.get('status'), owner_id=enterprise.get('owner_id'),
                             state_id=enterprise.get('state_id'),
                             province_id=enterprise.get('province_id'),
                             capacity=_float(enterprise.get('capacity')),
                             price=_float(enterprise.get('price')),
                             output=_float(enterprise.get('output', 0.0)),
                             payload=_dump(payload)))
        return rows

    def _forecast_rows(self):
        rows = []
        for enterprise in self.engine.enterprises.values():
            rows.append(dict(owner=enterprise['id'], owner_kind='enterprise',
                             good=enterprise.get('good'),
                             forecast=enterprise.get('sales_forecast'),
                             initial_estimate=enterprise.get('initial_sales_estimate'),
                             history=_dump(enterprise.get('sales_history', []))))
        for account in self.engine.accounts.values():
            for good, history in sorted(account.get('stock_sales_history', {}).items()):
                rows.append(dict(owner=account['id'], owner_kind='account', good=good,
                                 forecast=None, initial_estimate=None, history=_dump(history)))
        return rows

    def _plan_rows(self):
        rows = []
        for enterprise in self.engine.enterprises.values():
            for kind in ('production_plan', 'production_observation'):
                if kind in enterprise:
                    rows.append(dict(owner=enterprise['id'], owner_kind='enterprise',
                                     kind=kind, data=_dump(enterprise[kind])))
        for account in self.engine.accounts.values():
            for kind in ('project', 'public_projects'):
                if kind in account:
                    rows.append(dict(owner=account['id'], owner_kind='account',
                                     kind=kind, data=_dump(account[kind])))
        return rows

    def _population_rows(self):
        rows = []
        for entity in self.engine.entities.values():
            if entity['kind'] != 'SocialGroup' or 'population' not in entity['properties']:
                continue
            properties = entity['properties']
            rows.append(dict(group_id=entity['id'],
                             population=_float(properties.get('population')),
                             welfare=_float(properties.get('welfare')),
                             data=_dump(properties)))
        return rows

    def _market_rows(self):
        rows = []
        for month in self.engine.meta.get('market_history', []):
            for market in month.get('markets', []):
                rows.append(dict(month=month['month'], state_id=market['state_id'],
                                 province_id=market['province_id'], good=market['good'],
                                 volume=_float(market['volume']),
                                 trade_gold=_float(market['trade_gold']),
                                 freight=_float(market['freight']),
                                 requested=_float(market['requested']),
                                 unmet_solvent=_float(market['unmet_solvent']),
                                 limit_value=_float(market['limit_value']),
                                 reserved_unmet=_float(market['reserved_unmet']),
                                 data=_dump(market)))
        return rows

    def _trade_rows(self):
        rows = []
        for report in (self.reports[-1:] or ([self.last_report] if self.last_report else [])):
            for trade in report.get('trades', []):
                rows.append(dict(trade_id=trade.get('trade_id'), month=report.get('month'),
                                 good=trade.get('good'), quantity=trade.get('quantity'),
                                 price=trade.get('price'), gold=trade.get('gold'),
                                 freight=trade.get('freight'), buyer=trade.get('buyer'),
                                 seller=trade.get('seller'), buyer_state=trade.get('buyer_state'),
                                 seller_state=trade.get('seller_state'),
                                 buyer_province=trade.get('buyer_province'),
                                 seller_province=trade.get('seller_province'),
                                 purpose=trade.get('purpose'), data=_dump(trade)))
        return rows

    def _report_rows(self):
        # В игре полная история отчётов не нужна, но последний месяц нужен
        # всегда: по нему Live World показывает текущее состояние, и без него
        # checkpoint «свежего» мира читался бы как пустой.
        reports = self.reports or ([self.last_report] if self.last_report else [])
        return [dict(month=report.get('month'), rules_version=report.get('rules_version'),
                     data=_dump(report)) for report in reports]

    def _rng_rows(self):
        version, internal, gauss = self.engine.rng.getstate()
        return [dict(version=int(version), state=' '.join(str(value) for value in internal),
                     gauss=gauss)]

    def save_checkpoint(self, path):
        """Записать всю экономику в Parquet. Вызывается только явным сохранением.

        Запись идёт в соседний каталог и подменяет прежний целиком: недописанный
        checkpoint не должен выглядеть как рабочий.
        """
        path = Path(path)
        staging = path.with_name(path.name + '.staging')
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        account_rows, stock_rows, reserve_rows = self._account_rows()
        payloads = {
            'meta': self._meta_rows(),
            'accounts': account_rows,
            'stocks': stock_rows,
            'reserves': reserve_rows,
            'enterprises': self._enterprise_rows(),
            'forecasts': self._forecast_rows(),
            'plans': self._plan_rows(),
            'population': self._population_rows(),
            'market_history': self._market_rows(),
            'trades': self._trade_rows(),
            'reports': self._report_rows(),
            'rng': self._rng_rows(),
        }
        for name in TABLES:
            _write(staging / f'{name}.parquet', payloads[name], name)
        schema = dict(
            schema_version=SCHEMA_VERSION,
            world=world_fingerprint(self.world_graph),
            tiles=_tiles_signature(self.world_graph['tiles']),
            month=self.engine.meta.get('month', 0),
            created_at=time.time(),
            rules_version=getattr(self.engine, 'RULES_VERSION', None),
            counts={name: len(rows) for name, rows in payloads.items()},
        )
        (staging / 'schema.json').write_text(_dump(schema), encoding='utf-8')
        if path.exists():
            shutil.rmtree(path)
        os.replace(staging, path)
        self.checkpoint_path = path
        self.checkpoint_seconds = time.perf_counter() - started
        return self.checkpoint_path

    # --- сведения -----------------------------------------------------------

    def summary(self):
        return dict(month=self.engine.meta.get('month', 0),
                    gold=self.engine.gold(),
                    accounts=len(self.engine.accounts),
                    enterprises=len(self.engine.enterprises),
                    reports=len(self.reports),
                    checkpoint=str(self.checkpoint_path) if self.checkpoint_path else None)


def checkpoint_size(path):
    """Полный размер checkpoint и разбивка по таблицам."""
    path = Path(path)
    if not path.exists():
        return dict(total=0, files={})
    files = {file.name: file.stat().st_size for file in sorted(path.glob('*')) if file.is_file()}
    return dict(total=sum(files.values()), files=files)


def save_checkpoint(state, path):
    """Команда сохранения: `save_checkpoint(path)`."""
    if not isinstance(state, EconomyState):
        raise TypeError('save_checkpoint ожидает EconomyState')
    return state.save_checkpoint(path)


def load_checkpoint(path, world_graph):
    """Команда загрузки: `load_checkpoint(path, world_graph)`."""
    return EconomyState.load_checkpoint(path, world_graph)


def _exists(db, identity):
    try:
        db.get_entity(identity)
        return True
    except Exception:
        return False


def default_config():
    """Конфиг экономики из авторских данных: одинаков для нового мира и checkpoint."""
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'content/economy.json'
    return json.loads(path.read_text(encoding='utf-8'))


def default_checkpoint_path(db):
    """Checkpoint лежит рядом с миром: `data/world.economy` для `data/world.lbug`."""
    world = Path(db.path)
    return world.with_suffix(world.suffix + '.economy')
