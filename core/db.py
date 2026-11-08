"""Единственная точка доступа к LadybugDB."""
from __future__ import annotations

import json
import base64
import logging
import os
import zlib
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

import ladybug

log = logging.getLogger(__name__)
ENTITY_TYPES = ('State', 'Province', 'City', 'Character', 'SocialGroup', 'Army', 'Institution')
RELATIONS = {
    'includes': {('State', 'Province'), ('Province', 'City')},
    'owns': {('State', 'City'), ('State', 'Province')},
    'rules': {('Character', 'State')},
    'diplomacy': {('State', 'State')},
    'kinship': {('Character', 'Character')},
    'alliance': {('Character', 'Character')},
    'located_in': {('SocialGroup', 'Province'), ('SocialGroup', 'City'), ('Character', 'City')},
    'institution_of': {('Institution', 'State')},
    'belongs_to': {('Army', 'State')},
}


class NotFound(ValueError):
    pass


class IntegrityError(ValueError):
    pass


class PostCommitCheckpointError(RuntimeError):
    """Данные сохранены; повторять действие после ошибки checkpoint нельзя."""

    committed = True


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class WorldDB:
    def __init__(self, path='data/world.lbug', read_only=False, buffer_pool_size=None):
        if buffer_pool_size is None:
            try:
                memory_mb = int(os.environ.get('RPG_DB_BUFFER_POOL_MB', '128' if read_only else '1024'))
            except ValueError as error:
                raise ValueError('RPG_DB_BUFFER_POOL_MB должен быть положительным целым числом МиБ') from error
            if memory_mb <= 0:
                raise ValueError('RPG_DB_BUFFER_POOL_MB должен быть положительным целым числом МиБ')
            buffer_pool_size = memory_mb * 1024**2
        self.path = str(path)
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.closed = False
        self.database = ladybug.Database(self.path, buffer_pool_size=buffer_pool_size,
                                        max_num_threads=2, read_only=read_only)
        self.connection = ladybug.Connection(self.database)
        if not read_only:
            self.initialize()
        log.info('БД открыта: %s', self.path)

    def _query(self, query, params=None):
        with self.lock:
            result = self.connection.execute(query, params or {})
            try:
                return result.get_all()
            finally:
                result.close()

    def initialize(self):
        statements = [
            'CREATE NODE TABLE IF NOT EXISTS Entity(id STRING PRIMARY KEY, kind STRING, name STRING, properties STRING)',
            'CREATE REL TABLE IF NOT EXISTS Link(FROM Entity TO Entity, kind STRING, properties STRING)',
            'CREATE NODE TABLE IF NOT EXISTS Event(id STRING PRIMARY KEY, turn INT64, sequence INT64, type STRING, source STRING, importance INT64, summary STRING, payload STRING, parameter STRING, action_id STRING, created_at STRING)',
            'CREATE REL TABLE IF NOT EXISTS Affects(FROM Event TO Entity, role STRING)',
            'CREATE NODE TABLE IF NOT EXISTS GameMeta(id STRING PRIMARY KEY, turn INT64, seeded BOOL, name STRING)',
            "MERGE (m:GameMeta {id: 'game'}) ON CREATE SET m.turn=1, m.seeded=false, m.name='Новерия'",
        ]
        for statement in statements:
            self._query(statement)
        self._query('CREATE NODE TABLE IF NOT EXISTS Campaign(id STRING PRIMARY KEY, version INT64, settings STRING)')
        self._query('CREATE NODE TABLE IF NOT EXISTS Tile(id STRING PRIMARY KEY, x INT64, y INT64, terrain STRING, province_id STRING, road BOOL, resources STRING, passable BOOL, cost DOUBLE)')
        self._query('CREATE NODE TABLE IF NOT EXISTS TurnResult(id STRING PRIMARY KEY, turn INT64, report STRING)')
        self._query('CREATE NODE TABLE IF NOT EXISTS PendingCommand(id STRING PRIMARY KEY, kind STRING, payload STRING, status STRING)')
        for table in ('EconomyAccount', 'Enterprise', 'EconomyMeta', 'EconomyMonth'):
            self._query(f'CREATE NODE TABLE IF NOT EXISTS {table}(id STRING PRIMARY KEY, data STRING)')

    @contextmanager
    def transaction(self):
        with self.lock:
            self._query('BEGIN TRANSACTION')
            committing = False
            try:
                yield
                committing = True
                self._query('COMMIT')
            except BaseException as error:
                # После durable commit откат невозможен: не маскируем сохранённое действие.
                if committing and isinstance(error, RuntimeError) and str(error).startswith(
                    'Transaction committed successfully, but the post-commit checkpoint failed.'
                ):
                    log.error('Транзакция сохранена, но checkpoint не выполнен; требуется перезапуск БД: %s', error)
                    raise PostCommitCheckpointError(str(error)) from error
                try:
                    self._query('ROLLBACK')
                except RuntimeError:
                    log.exception('Откат недоступен; исходная ошибка транзакции сохранена')
                log.exception('Транзакция отменена')
                raise

    def close(self):
        with self.lock:
            if not self.closed:
                self.connection.close()
                self.database.close()
                self.closed = True

    @contextmanager
    def read_transaction(self):
        """Единый зафиксированный снимок без права записи, включая внешних читателей."""
        with self.lock:
            self._query('BEGIN TRANSACTION READ ONLY')
            try:
                yield
                self._query('COMMIT')
            except BaseException:
                self._query('ROLLBACK')
                raise

    def economy_month_ids(self):
        rows = self._query('MATCH (n:EconomyMonth) RETURN n.id')
        return sorted(int(row[0]) for row in rows)

    def economy_month(self, month):
        rows = self._query('MATCH (n:EconomyMonth {id:$id}) RETURN n.data', {'id': str(month)})
        if not rows:
            return None
        data = rows[0][0]
        if data.startswith('z:'):
            data = zlib.decompress(base64.b64decode(data[2:])).decode('utf-8')
        return json.loads(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _entity(row):
        return dict(id=row[0], kind=row[1], name=row[2], properties=json.loads(row[3]))

    def get_entity(self, entity_id):
        rows = self._query('MATCH (e:Entity {id:$id}) RETURN e.id,e.kind,e.name,e.properties', {'id': entity_id})
        if not rows:
            raise NotFound(f'Сущность не найдена: {entity_id}')
        return self._entity(rows[0])

    def list_entities(self, kind=None, search='', limit=100, offset=0):
        if kind is not None and kind not in ENTITY_TYPES:
            raise ValueError('Неизвестный тип сущности')
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError('Недопустимый размер страницы')
        where, params = [], {'limit': limit, 'offset': offset}
        if search:
            where.append('(contains(lower(e.name), lower($search)) OR contains(e.id, $search))')
            params['search'] = search
        if kind:
            where.append('e.kind=$kind')
            params['kind'] = kind
        condition = ' WHERE ' + ' AND '.join(where) if where else ''
        rows = self._query('MATCH (e:Entity)' + condition +
                           ' RETURN e.id,e.kind,e.name,e.properties ORDER BY e.name,e.id SKIP $offset LIMIT $limit', params)
        return [self._entity(row) for row in rows]

    def create_entity(self, entity_id, kind, name, properties):
        if kind not in ENTITY_TYPES or not name.strip() or not entity_id:
            raise ValueError('Укажите допустимый тип, идентификатор и название')
        if not isinstance(properties, dict):
            raise ValueError('Свойства должны быть объектом JSON')
        if self._query('MATCH (e:Entity {id:$id}) RETURN e.id', {'id': entity_id}):
            raise IntegrityError('Идентификатор уже существует')
        self._query('CREATE (:Entity {id:$id,kind:$kind,name:$name,properties:$properties})',
                    dict(id=entity_id, kind=kind, name=name.strip(), properties=encode(properties)))
        log.info('Создана сущность %s (%s)', entity_id, kind)

    def update_parameter(self, entity_id, parameter, value):
        from modules.population import validate_parameter
        validate_parameter(parameter,value)
        entity = self.get_entity(entity_id)
        if not parameter.strip() or len(parameter) > 80:
            raise ValueError('Некорректное имя параметра')
        if parameter == 'name':
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Название не должно быть пустым')
            old = entity['name']
            self._query('MATCH (e:Entity {id:$id}) SET e.name=$name', dict(id=entity_id, name=value.strip()))
        else:
            old = entity['properties'].get(parameter)
            entity['properties'][parameter] = value
            self._query('MATCH (e:Entity {id:$id}) SET e.properties=$p',
                        dict(id=entity_id, p=encode(entity['properties'])))
        log.info('Параметр %s.%s изменён', entity_id, parameter)
        return old

    def links(self, entity_id=None, kind=None, limit=500, offset=0):
        if kind is not None and kind not in RELATIONS:
            raise ValueError('Неизвестный тип связи')
        if not 1 <= limit <= 2000 or offset < 0:
            raise ValueError('Недопустимый размер страницы')
        clauses, params = [], dict(limit=limit, offset=offset)
        if entity_id:
            clauses.append('(a.id=$id OR b.id=$id)')
            params['id'] = entity_id
        if kind:
            clauses.append('r.kind=$kind')
            params['kind'] = kind
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self._query('MATCH (a:Entity)-[r:Link]->(b:Entity)' + where +
                           ' RETURN a.id,b.id,r.kind,r.properties ORDER BY a.id,b.id,r.kind SKIP $offset LIMIT $limit', params)
        return [dict(source=a, target=b, kind=k, properties=json.loads(p)) for a, b, k, p in rows]

    def put_link(self, source, target, kind, properties=None, replace=False):
        a, b = self.get_entity(source), self.get_entity(target)
        if source == target or (a['kind'], b['kind']) not in RELATIONS.get(kind, set()):
            raise IntegrityError('Недопустимая связь между типами сущностей')
        # Единственный владелец, родитель и действующий правитель.
        exclusive = kind in ('owns', 'includes', 'rules', 'belongs_to')
        if exclusive:
            side, identity = ('a', source) if kind == 'belongs_to' else ('b', target)
            existing = self._query(f'MATCH (a:Entity)-[r:Link]->(b:Entity) WHERE {side}.id=$id AND r.kind=$kind RETURN a.id,b.id',
                                   dict(id=identity, kind=kind))
            conflicts = [x for x in existing if x != [source, target]]
            if conflicts and not replace:
                raise IntegrityError('Связь конфликтует с текущим назначением; используйте специальное действие')
            if replace:
                self._query(f'MATCH (a:Entity)-[r:Link]->(b:Entity) WHERE {side}.id=$id AND r.kind=$kind DELETE r',
                            dict(id=identity, kind=kind))
        self._query('MATCH (a:Entity {id:$a}),(b:Entity {id:$b}) MERGE (a)-[r:Link {kind:$kind}]->(b) SET r.properties=$p',
                    dict(a=source, b=target, kind=kind, p=encode(properties or {})))

    def remove_link(self, source, target, kind):
        self._query('MATCH (a:Entity {id:$a})-[r:Link]->(b:Entity {id:$b}) WHERE r.kind=$kind DELETE r',
                    dict(a=source, b=target, kind=kind))

    def meta(self):
        row = self._query("MATCH (m:GameMeta {id:'game'}) RETURN m.turn,m.seeded,m.name")[0]
        return dict(turn=row[0], seeded=row[1], name=row[2])

    def set_turn(self, turn):
        if not isinstance(turn, int) or turn < 1:
            raise ValueError('Ход должен быть положительным целым числом')
        self._query("MATCH (m:GameMeta {id:'game'}) SET m.turn=$turn", {'turn': turn})

    def mark_seeded(self):
        self._query("MATCH (m:GameMeta {id:'game'}) SET m.seeded=true")

    def record_event(self, event):
        turn = self.meta()['turn']
        sequence = self._query('MATCH (e:Event) WHERE e.turn=$turn RETURN count(e)', {'turn': turn})[0][0] + 1
        params = {k: event[k] for k in ('id', 'type', 'source', 'importance', 'summary', 'action_id', 'created_at')}
        params.update(turn=turn, sequence=sequence, payload=encode(event['payload']), parameter=event['payload'].get('parameter',''))
        self._query('CREATE (:Event {id:$id,turn:$turn,sequence:$sequence,type:$type,source:$source,importance:$importance,summary:$summary,payload:$payload,parameter:$parameter,action_id:$action_id,created_at:$created_at})', params)
        for entity in event['entities']:
            self.get_entity(entity['id'])
            self._query('MATCH (v:Event {id:$event}),(e:Entity {id:$entity}) CREATE (v)-[:Affects {role:$role}]->(e)',
                        dict(event=event['id'], entity=entity['id'], role=entity['role']))
        log.info('Записано событие %s: %s', event['id'], event['type'])
        return dict(event, turn=turn, sequence=sequence)

    def history(self, entity_id=None, event_type=None, turn=None, source=None, importance=0, limit=50, offset=0, parameter=None,
                turn_from=None, turn_to=None, action_id=None, scope_ids=None):
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError('Недопустимый размер страницы')
        match = 'MATCH (e:Event)'
        clauses, params = ['e.importance >= $importance'], dict(importance=importance, limit=limit, offset=offset)
        if entity_id:
            match = 'MATCH (e:Event)-[:Affects]->(n:Entity)'
            clauses.append('n.id=$entity_id')
            params['entity_id'] = entity_id
        for field, value in [('type', event_type), ('turn', turn), ('source', source)]:
            if value is not None:
                clauses.append(f'e.{field}=${field}')
                params[field] = value
        if parameter:
            clauses.append('e.parameter=$parameter')
            params['parameter'] = parameter
        if turn_from is not None:
            clauses.append('e.turn >= $turn_from'); params['turn_from']=turn_from
        if turn_to is not None:
            clauses.append('e.turn <= $turn_to'); params['turn_to']=turn_to
        if action_id:
            clauses.append('e.action_id=$action_id'); params['action_id']=action_id
        if scope_ids is not None:
            match='MATCH (e:Event)-[:Affects]->(n:Entity)'
            clauses.append('n.id IN $scope'); params['scope']=scope_ids
        rows = self._query(match + ' WHERE ' + ' AND '.join(clauses) +
                           ' RETURN DISTINCT e.id,e.turn,e.sequence,e.type,e.source,e.importance,e.summary,e.payload,e.action_id,e.created_at ORDER BY e.turn DESC,e.sequence DESC SKIP $offset LIMIT $limit', params)
        keys = ('id', 'turn', 'sequence', 'type', 'source', 'importance', 'summary', 'payload', 'action_id', 'created_at')
        events = []
        for row in rows:
            item = dict(zip(keys, row))
            item['payload'] = json.loads(item['payload'])
            item['entities'] = [dict(id=i, role=r) for i, r in self._query(
                'MATCH (e:Event {id:$id})-[a:Affects]->(n:Entity) RETURN n.id,a.role', {'id': item['id']})]
            events.append(item)
        return events

    def counts(self):
        result = dict(self._query('MATCH (e:Entity) RETURN e.kind,count(e)'))
        result['Event'] = self._query('MATCH (e:Event) RETURN count(e)')[0][0]
        return result

    def relation_counts(self):
        result = dict(self._query('MATCH (:Entity)-[r:Link]->(:Entity) RETURN r.kind,count(r)'))
        result['affects'] = self._query('MATCH (:Event)-[r:Affects]->(:Entity) RETURN count(r)')[0][0]
        return result

    def graph(self, types=None, relations=None, focus=None, depth=2, max_nodes=100):
        types = set(ENTITY_TYPES if types is None else types)
        relations = set(RELATIONS if relations is None else relations)
        if not types <= set(ENTITY_TYPES) or not relations <= set(RELATIONS):
            raise ValueError('Неизвестный фильтр графа')
        if not 0 <= depth <= 5 or not 1 <= max_nodes <= 500:
            raise ValueError('Глубина 0–5, количество узлов 1–500')
        nodes = {}
        if focus:
            entity = self.get_entity(focus)
            if entity['kind'] in types:
                nodes[focus] = entity
            frontier = list(nodes)
            for _ in range(depth):
                next_frontier = []
                for entity_id in frontier:
                    # Лимит в запросе исключает неограниченную загрузку соседей.
                    rows = self._query('MATCH (a:Entity)-[r:Link]-(b:Entity) WHERE a.id=$id AND r.kind IN $relations AND b.kind IN $types RETURN DISTINCT b.id,b.kind,b.name,b.properties ORDER BY b.id LIMIT $limit',
                                       dict(id=entity_id, relations=sorted(relations), types=sorted(types), limit=max_nodes))
                    for row in rows:
                        if row[0] not in nodes and len(nodes) < max_nodes:
                            nodes[row[0]] = self._entity(row)
                            next_frontier.append(row[0])
                frontier = next_frontier
        else:
            rows = self._query('MATCH (e:Entity) WHERE e.kind IN $types RETURN e.id,e.kind,e.name,e.properties ORDER BY e.kind,e.id LIMIT $limit',
                               dict(types=sorted(types), limit=max_nodes))
            nodes = {r[0]: self._entity(r) for r in rows}
        rows = self._query('MATCH (a:Entity)-[r:Link]->(b:Entity) WHERE a.id IN $ids AND b.id IN $ids AND r.kind IN $relations RETURN a.id,b.id,r.kind,r.properties ORDER BY a.id,b.id,r.kind LIMIT 2000',
                           dict(ids=list(nodes), relations=sorted(relations)))
        edges = [dict(source=a, target=b, kind=k, properties=json.loads(p)) for a, b, k, p in rows]
        return dict(nodes=list(nodes.values()), edges=edges, bounded=True, max_nodes=max_nodes, depth=depth)

    def check_integrity(self):
        issues = []
        for a, b, k, ak, bk in self._query('MATCH (a:Entity)-[r:Link]->(b:Entity) RETURN a.id,b.id,r.kind,a.kind,b.kind'):
            if a == b or (ak, bk) not in RELATIONS.get(k, set()):
                issues.append(f'Недопустимая связь: {a} {k} {b}')
        for kind in ('owns', 'includes', 'rules', 'belongs_to'):
            side = 'a' if kind == 'belongs_to' else 'b'
            rows = self._query(f'MATCH (a:Entity)-[r:Link]->(b:Entity) WHERE r.kind=$kind WITH {side}.id AS id,count(r) AS total WHERE total>1 RETURN id,total', {'kind': kind})
            issues.extend(f'Несколько связей {kind}: {row[0]}' for row in rows)
        return issues

    def campaign(self):
        rows=self._query("MATCH (c:Campaign {id:'game'}) RETURN c.version,c.settings")
        return {**json.loads(rows[0][1]),'version':rows[0][0]} if rows else None

    def save_campaign(self, settings):
        self._query("MERGE (c:Campaign {id:'game'}) SET c.version=2,c.settings=$settings",dict(settings=encode(settings)))

    def store_tiles(self, tiles):
        if len({(t['x'],t['y']) for t in tiles})!=len(tiles): raise IntegrityError('Дубли координат')
        rows=[dict(t,resources=encode(t['resources']),cost=float(t['cost'])) for t in tiles]
        self._query('UNWIND $rows AS t CREATE (:Tile {id:t.id,x:t.x,y:t.y,terrain:t.terrain,province_id:t.province_id,road:t.road,resources:t.resources,passable:t.passable,cost:t.cost})',dict(rows=rows))

    def tiles(self):
        rows=self._query('MATCH (t:Tile) RETURN t.id,t.x,t.y,t.terrain,t.province_id,t.road,t.resources,t.passable,t.cost ORDER BY t.y,t.x')
        keys=('id','x','y','terrain','province_id','road','resources','passable','cost')
        result=[dict(zip(keys,row)) for row in rows]
        for t in result: t['resources']=json.loads(t['resources'])
        return result

    def assign_tiles(self,ids,province_id):
        self._query('MATCH (t:Tile) WHERE t.id IN $ids SET t.province_id=$province',dict(ids=ids,province=province_id))

    def replace_properties(self,identity,properties):
        self.get_entity(identity)
        self._query('MATCH (e:Entity {id:$id}) SET e.properties=$p',dict(id=identity,p=encode(properties)))

    def snapshot(self):
        rows=self._query('MATCH (e:Entity) RETURN e.id,e.kind,e.name,e.properties ORDER BY e.id')
        links=self._query('MATCH (a:Entity)-[r:Link]->(b:Entity) RETURN a.id,b.id,r.kind,r.properties ORDER BY a.id,b.id,r.kind')
        return dict(entities=[self._entity(r) for r in rows],
                    links=[dict(source=a,target=b,kind=k,properties=json.loads(p)) for a,b,k,p in links])

    def turn_result(self, command_id):
        rows=self._query('MATCH (r:TurnResult {id:$id}) RETURN r.report',dict(id=command_id))
        return json.loads(rows[0][0]) if rows else None

    def save_turn_result(self, command_id, report):
        self._query('CREATE (:TurnResult {id:$id,turn:$turn,report:$report})',dict(id=command_id,turn=report['turn'],report=encode(report)))

    def turn_reports(self,limit=12):
        return [json.loads(row[0]) for row in self._query('MATCH (r:TurnResult) RETURN r.report ORDER BY r.turn DESC LIMIT $limit',dict(limit=limit))]

    def queue_command(self,command_id,kind,payload):
        self._query("CREATE (:PendingCommand {id:$id,kind:$kind,payload:$payload,status:'pending'})",dict(id=command_id,kind=kind,payload=encode(payload)))

    def pending_commands(self):
        return [dict(id=i,kind=k,payload=json.loads(p)) for i,k,p in self._query("MATCH (c:PendingCommand) WHERE c.status='pending' RETURN c.id,c.kind,c.payload ORDER BY c.id")]

    def complete_command(self,command_id):
        self._query("MATCH (c:PendingCommand {id:$id}) SET c.status='completed'",dict(id=command_id))

    def economy_records(self, table):
        if table not in ('EconomyAccount', 'Enterprise', 'EconomyMeta', 'EconomyMonth'):
            raise ValueError('Неизвестная экономическая таблица')
        result = []
        for row in self._query(f'MATCH (n:{table}) RETURN n.data ORDER BY n.id'):
            data = row[0]
            if data.startswith('z:'):
                data = zlib.decompress(base64.b64decode(data[2:])).decode('utf-8')
            result.append(json.loads(data))
        return result

    def store_economy(self, table, records):
        if table not in ('EconomyAccount', 'Enterprise', 'EconomyMeta', 'EconomyMonth'):
            raise ValueError('Неизвестная экономическая таблица')
        if not records:
            return
        rows = [dict(id=r['id'], data=encode(r)) for r in records]
        if table == 'EconomyMonth':
            for row in rows:
                row['data'] = 'z:' + base64.b64encode(zlib.compress(row['data'].encode('utf-8'))).decode('ascii')
            self._query(f'UNWIND $rows AS r CREATE (:{table} {{id:r.id,data:r.data}})', dict(rows=rows))
        else:
            self._query(f'UNWIND $rows AS r MERGE (n:{table} {{id:r.id}}) SET n.data=r.data', dict(rows=rows))

    def update_welfare(self, values):
        rows = []
        for entity in self.snapshot()['entities']:
            if entity['id'] in values:
                props = dict(entity['properties'], welfare=values[entity['id']])
                rows.append(dict(id=entity['id'], data=encode(props)))
        if rows:
            self._query('UNWIND $rows AS r MATCH (n:Entity {id:r.id}) SET n.properties=r.data', dict(rows=rows))

    def update_social_groups(self, values):
        """Сохраняет изменяемые свойства социальных групп (численность и welfare)."""
        rows = []
        for entity in self.snapshot()['entities']:
            properties = values.get(entity['id'])
            if entity['kind'] == 'SocialGroup' and properties is not None:
                rows.append(dict(id=entity['id'], data=encode(properties)))
        if rows:
            self._query('UNWIND $rows AS r MATCH (n:Entity {id:r.id}) SET n.properties=r.data', dict(rows=rows))

    def fork_to(self, destination):
        """Логическая копия под блокировкой, без копирования открытого файла БД."""
        destination = Path(destination)
        if destination.exists() or str(destination.resolve()) == str(Path(self.path).resolve()):
            raise ValueError('Для копии требуется новый путь')
        with self.lock, WorldDB(destination) as target, target.transaction():
            snap = self.snapshot()
            for e in snap['entities']:
                target.create_entity(e['id'], e['kind'], e['name'], e['properties'])
            for r in snap['links']:
                target.put_link(r['source'], r['target'], r['kind'], r['properties'])
            target.store_tiles(self.tiles())
            if self.campaign():
                target.save_campaign(self.campaign())
            target.set_turn(self.meta()['turn'])
            target.mark_seeded()
            for table in ('EconomyAccount', 'Enterprise', 'EconomyMeta', 'EconomyMonth'):
                target.store_economy(table, self.economy_records(table))
            for c in self.pending_commands():
                target.queue_command(c['id'], c['kind'], c['payload'])
