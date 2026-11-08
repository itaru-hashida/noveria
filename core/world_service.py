"""Координация атомарных действий и уведомлений после фиксации БД."""
from datetime import datetime, timezone
from uuid import uuid4

from core.db import IntegrityError


class WorldService:
    def __init__(self, db, bus):
        self.db, self.bus = db, bus
        from core.turn_service import TurnService
        self.turns=TurnService(self)
        self._tiles_cache=None

    def tiles(self):
        if self._tiles_cache is None: self._tiles_cache=self.db.tiles()
        return self._tiles_cache

    def initialize_economy(self):
        from modules.economy import initialize
        return initialize(self)

    def simulate_economy(self, months, destination, progress=None):
        from modules.economy import run_stress
        return run_stress(self, months, destination, progress=progress)

    def event(self,event_type,payload,entities,source='player',importance=5,summary=None,action_id=None):
        return dict(id=str(uuid4()),type=event_type,payload=payload,entities=entities,source=source,
                    importance=importance,summary=summary or event_type,action_id=action_id or str(uuid4()),
                    created_at=datetime.now(timezone.utc).isoformat())

    def notify(self,event):
        self.bus.publish(event['type'],event,source=event['source'])
        self.bus.process_queue()

    def _execute(self, event_type, payload, entities, operation, source='player', importance=5,
                 summary=None, action_id=None):
        event = dict(id=str(uuid4()), type=event_type, payload=payload, entities=entities,
                     source=source, importance=importance, summary=summary or event_type,
                     action_id=action_id or str(uuid4()), created_at=datetime.now(timezone.utc).isoformat())
        with self.db.transaction():
            operation(payload)
            errors=self.db.check_integrity()
            if errors: raise IntegrityError('; '.join(errors))
            if self.db.campaign():
                from core.integrity import validate_world
                validate_world(self.db.snapshot(),self.db.tiles(),self.db.campaign(),require_connected=False)
            event = self.db.record_event(event)
        self._tiles_cache=None
        self.notify(event)
        return event

    def create_entity(self, kind, name, properties=None, entity_id=None, source='player', action_id=None):
        entity_id = entity_id or str(uuid4())
        if properties is not None and not isinstance(properties,dict): raise ValueError('Свойства должны быть объектом JSON')
        from modules.population import validate_parameter
        for field,value in (properties or {}).items(): validate_parameter(field,value)
        if self.db.campaign() and kind in ('City','Province'):
            return self.create_territory(kind,name,properties or {},entity_id,source,action_id)
        if self.db.campaign() and kind=='SocialGroup':
            raise ValueError('Три страты создаются автоматически вместе с территорией; редактируйте существующие группы')
        return self._execute('entity_created', dict(kind=kind, name=name, properties=properties or {}),
                             [dict(id=entity_id, role='created')],
                             lambda p: self.db.create_entity(entity_id, kind, name, p['properties']),
                             source, summary=f'Создана сущность: {name}', action_id=action_id)

    def start_institution_project(self, city_id, kind, mode='normal', source='player', action_id=None):
        """Заказать муниципальную постройку из статической сметы экономики."""
        def operation(payload):
            from modules.economy import Economy
            engine = Economy.load(self.db)
            project = engine.start_public_project(city_id, kind, mode)
            engine.save(self.db, include_month=False)
            payload.update(project_id=project['id'], institution_id=project['institution_id'],
                kind=kind, city_id=city_id, mode=mode, materials=project['material_bill'],
                equipment=project['equipment'], work_required=project['work_required'])
        return self._execute('institution_project_started', dict(kind=kind, city_id=city_id),
            [dict(id=city_id, role='construction_site')], operation, source,
            summary='Начата муниципальная стройка: '+kind, action_id=action_id)

    def create_territory(self,kind,name,properties,entity_id,source='admin',action_id=None):
        """Создание города или выделение небольшой провинции из существующей."""
        from modules.population import create_groups
        from modules.geography import topology
        import networkx as nx
        props=dict(properties)
        pid=props.get('province_id')
        tile_id=props.get('tile_id')
        state_id=props.get('state_id')
        def operation(payload):
            tiles=self.db.tiles(); by_id={t['id']:t for t in tiles}
            tile=by_id.get(tile_id)
            if not tile or tile['terrain'] in ('water','mountain'): raise ValueError('Выберите свободную проходимую клетку равнины/леса/реки')
            snap=self.db.snapshot(); index={e['id']:e for e in snap['entities']}
            if any(e['kind']=='City' and e['properties'].get('tile_id')==tile_id for e in snap['entities']):
                raise ValueError('Клетка уже занята городом')
            parent=tile['province_id']
            if kind=='Province':
                if not state_id or self.db.get_entity(state_id)['kind']!='State': raise ValueError('Укажите state_id государства')
                old_nodes=[t['id'] for t in tiles if t['province_id']==parent and t['id']!=tile_id]
                if not old_nodes or not nx.is_connected(topology(tiles).subgraph(old_nodes)):
                    raise ValueError('Выделение клетки разрывает исходную провинцию')
                pid_new=entity_id; city_id=entity_id+'-capital'
                p=dict(props,capital_id=city_id); p.pop('province_id',None); p.pop('tile_id',None); p.pop('state_id',None)
                self.db.create_entity(entity_id,'Province',name,p)
                self.db.put_link(state_id,entity_id,'includes'); self.db.put_link(state_id,entity_id,'owns')
                self.db.assign_tiles([tile_id],entity_id); tile['province_id']=entity_id
                self.db.create_entity(city_id,'City',props.get('city_name',name+' — столица'),dict(tile_id=tile_id,province_id=entity_id,canon=False))
                self.db.put_link(entity_id,city_id,'includes'); self.db.put_link(state_id,city_id,'owns')
                created=[self.db.get_entity(entity_id),self.db.get_entity(city_id)]
            else:
                if pid!=parent: raise ValueError('province_id должен совпадать с провинцией клетки')
                state=next(r['source'] for r in snap['links'] if r['target']==parent and r['kind']=='owns')
                self.db.create_entity(entity_id,'City',name,props)
                self.db.put_link(parent,entity_id,'includes'); self.db.put_link(state,entity_id,'owns')
                created=[self.db.get_entity(entity_id)]
            geography=dict(tiles=tiles,config=self.db.campaign())
            for group in create_groups(dict(entities=created),geography):
                self.db.create_entity(group['id'],group['kind'],group['name'],group['properties'])
                self.db.put_link(group['id'],group['properties']['owner_id'],'located_in')
            payload['created']=[e['id'] for e in created]
        return self._execute('entity_created',dict(kind=kind,name=name,properties=props),
                             [dict(id=entity_id,role='created')],operation,source,
                             summary=f'Создана территория: {name}',action_id=action_id)

    def change_parameter(self, entity_id, parameter, value, source='player', action_id=None):
        if parameter in ('tile_id','province_id','owner_id','category','capital_id'):
            raise ValueError('Структурное поле изменяется только специальным действием')
        def operation(payload):
            payload['old'] = self.db.update_parameter(entity_id, parameter, value)
        return self._execute('parameter_changed', dict(parameter=parameter, new=value),
                             [dict(id=entity_id, role='subject')], operation, source,
                             summary=f'Изменён параметр {parameter}', action_id=action_id)

    def create_link(self, source_id, target_id, kind, properties=None, source='admin'):
        if kind == 'owns':
            return self.transfer_territory(target_id, source_id, source=source)
        if kind == 'rules':
            return self.appoint_ruler(target_id, source_id, source=source)
        if kind == 'diplomacy':
            return self.set_diplomacy(source_id, target_id, (properties or {}).get('value', 0), source=source)
        return self._execute('relation_changed', dict(source=source_id, target=target_id, kind=kind, new=properties or {}),
                             [dict(id=source_id, role='source'), dict(id=target_id, role='target')],
                             lambda p: self.db.put_link(source_id, target_id, kind, properties), source,
                             summary=f'Связь: {kind}')

    def transfer_territory(self, territory_id, state_id, source='player'):
        territory, state = self.db.get_entity(territory_id), self.db.get_entity(state_id)
        if territory['kind'] not in ('City', 'Province') or state['kind'] != 'State':
            raise IntegrityError('Нужны город/провинция и государство')
        affected = [dict(id=territory_id, role='territory'), dict(id=state_id, role='new_owner')]

        def operation(payload):
            owners = [r['source'] for r in self.db.links(territory_id, 'owns') if r['target'] == territory_id]
            payload['old'] = owners
            affected.extend(dict(id=i, role='old_owner') for i in owners if i != state_id)
            self.db.put_link(state_id, territory_id, 'owns', replace=True)
            if territory['kind'] == 'Province':
                self.db.put_link(state_id, territory_id, 'includes', replace=True)
            # Города имеют собственное владение; передача провинции его не меняет.
        return self._execute('territory_transferred', dict(new=state_id, territory=territory_id), affected,
                             operation, source, importance=8, summary=f'Передана территория: {territory["name"]}')

    def appoint_ruler(self, state_id, character_id, source='player'):
        if self.db.get_entity(state_id)['kind'] != 'State' or self.db.get_entity(character_id)['kind'] != 'Character':
            raise IntegrityError('Нужны государство и персонаж')
        affected = [dict(id=state_id, role='state'), dict(id=character_id, role='new_ruler')]

        def operation(payload):
            old = [r['source'] for r in self.db.links(state_id, 'rules') if r['target'] == state_id]
            payload['old'] = old
            affected.extend(dict(id=i, role='old_ruler') for i in old if i != character_id)
            self.db.put_link(character_id, state_id, 'rules', replace=True)
        return self._execute('ruler_changed', dict(new=character_id), affected, operation, source,
                             importance=9, summary='Назначен правитель')

    def set_diplomacy(self, first, second, value, source='player'):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -100 <= value <= 100:
            raise ValueError('Отношение должно быть от -100 до 100')
        first, second = sorted((first, second))

        def operation(payload):
            old = [r for r in self.db.links(first, 'diplomacy') if r['target'] == second]
            payload['old'] = old[0]['properties'].get('value') if old else None
            self.db.put_link(first, second, 'diplomacy', {'value': value})
        return self._execute('diplomacy_changed', dict(new=value),
                             [dict(id=first, role='state'), dict(id=second, role='state')], operation,
                             source, summary='Изменены дипломатические отношения')

    def advance_turn(self, source='player',command_id=None):
        if self.db.campaign(): return self.turns.finish(command_id,source)
        def operation(payload):
            payload['old'] = self.db.meta()['turn']
            payload['new'] = payload['old'] + 1
            self.db.set_turn(payload['new'])
        return self._execute('turn_advanced', {}, [], operation, source, summary='Начат следующий ход')

    def change_social(self,entity_id,parameter,value,source='admin'):
        if self.db.get_entity(entity_id)['kind']!='SocialGroup' or parameter not in ('population','loyalty','welfare','influence'):
            raise ValueError('Недопустимый социальный параметр')
        return self.change_parameter(entity_id,parameter,value,source=source)

    def schedule_social(self,entity_id,parameter,value,command_id=None,source='admin'):
        from modules.population import validate_parameter
        validate_parameter(parameter,value)
        if self.db.get_entity(entity_id)['kind']!='SocialGroup' or parameter not in ('population','loyalty','welfare','influence'):
            raise ValueError('Недопустимая команда')
        identity=command_id or str(uuid4())
        return self._execute('command_scheduled',dict(command_id=identity,parameter=parameter,new=value),
                             [dict(id=entity_id,role='subject')],
                             lambda p:self.db.queue_command(identity,'set_social',dict(entity_id=entity_id,parameter=parameter,value=value)),
                             source,summary='Социальное распоряжение запланировано',action_id=identity)
