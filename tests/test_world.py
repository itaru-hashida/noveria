import json
import pytest
from core.db import WorldDB, NotFound, IntegrityError
from core.context_builder import ContextBuilder
from core.history_service import HistoryService
from core.events import EventBus
from core.world_service import WorldService
from loader import load_world
from modules.social import change_loyalty
from modules.demo import run_demo
from modules.diplomacy import set_relation
from modules.world import transfer_territory, appoint_ruler


def test_schema_persistence_and_missing(tmp_path):
    path = tmp_path / 'persistent.lbug'
    with WorldDB(path) as db:
        service = WorldService(db, EventBus())
        service.create_entity('State', 'Test', entity_id='state')
        service.change_parameter('state', 'treasury', 100)
        with pytest.raises(NotFound):
            db.get_entity('absent')
    with WorldDB(path) as db:
        assert db.get_entity('state')['properties']['treasury'] == 100
        assert db.list_entities('State',limit=1)[0]['id'] == 'state'
        assert len(db.history()) == 2


def test_history_values_and_no_consequences(service):
    change_loyalty(service, 'stratum-province-0-0', 100)
    change_loyalty(service, 'stratum-province-0-0', 120)
    change_loyalty(service, 'stratum-province-0-0', 90)
    events = HistoryService(service.db).parameter_history('stratum-province-0-0', 'loyalty')
    assert [(e['payload']['old'], e['payload']['new']) for e in events[:2]] == [(120,90),(100,120)]
    assert service.db.get_entity('stratum-province-0-0')['properties']['loyalty'] == 90
    change_loyalty(service, 'stratum-province-0-0', 70)
    previous = service.db.counts()['Event']
    change_loyalty(service, 'stratum-province-0-0', 55)
    assert service.db.counts()['Event'] == previous+1
    assert service.db.history(limit=1)[0]['payload'] == dict(parameter='loyalty',old=70,new=55)
    assert {e['type'] for e in service.db.history()} == {'parameter_changed'}


def test_territory_ruler_graph_and_context(service):
    transfer_territory(service, 'city-0-0', 'velaria')
    owners = [r['source'] for r in service.db.links('city-0-0','owns')]
    assert owners == ['velaria']
    appoint_ruler(service,'noweria','demo-character')
    assert [r['source'] for r in service.db.links('noweria','rules')] == ['demo-character']
    assert service.db.history(entity_id='itaru')[0]['type'] == 'ruler_changed'
    graph = service.db.graph(focus='city-0-0',depth=1)
    assert any(e['source']=='velaria' and e['target']=='city-0-0' for e in graph['edges'])
    assert not any(e['source']=='noweria' and e['kind']=='owns' for e in graph['edges'])
    context = ContextBuilder(service.db).build('city-0-0',event_limit=1)
    assert any(r['source']=='velaria' and r['kind']=='owns' for r in context['current_relations'])
    assert len(context['recent_history'])+len(context['important_facts']) <= 1
    assert context['recent_history'][0]['payload']['old'] == ['noweria']
    assert service.db.check_integrity() == []


def test_province_transfer(service):
    transfer_territory(service,'province-0','arven')
    assert [r['source'] for r in service.db.links('province-0','owns')] == ['arven']
    assert [r['source'] for r in service.db.links('province-0','includes') if r['target']=='province-0'] == ['arven']
    assert [r['source'] for r in service.db.links('city-0-0','owns')] == ['noweria']


def test_seed_is_idempotent(service):
    before = service.db.counts()
    change_loyalty(service,'stratum-province-0-0',45)
    assert load_world(service.db) is False
    assert service.db.get_entity('stratum-province-0-0')['properties']['loyalty'] == 45
    assert service.db.counts()['City'] == before['City'] == 30
    assert service.db.counts()['State'] == 6
    assert len(service.db.links()) == len(service.db.snapshot()["links"])
    assert service.db.get_entity('itaru')['name'] == 'Итару Первый'


def test_creation_relations_and_filters(service):
    service.create_entity('Character','Новый персонаж',entity_id='new')
    service.create_link('new','city-0-0','located_in')
    assert service.db.links('new')[0]['target'] == 'city-0-0'
    assert service.db.list_entities('Character','Новый')[0]['id'] == 'new'
    assert service.db.history(entity_id='new',source='admin')[0]['type'] == 'relation_changed'
    with pytest.raises(IntegrityError):
        service.create_link('new','city-0-0','owns')
    with pytest.raises(IntegrityError):
        service.db.put_link('arven','city-0-0','owns')
    graph = service.db.graph(types=['State'],max_nodes=2)
    assert len(graph['nodes']) <= 2
    assert {n['kind'] for n in graph['nodes']} == {'State'}
    assert service.db.graph(types=[])['nodes'] == []
    assert service.db.graph(relations=[])['edges'] == []


def test_atomic_rollback(service,monkeypatch):
    initial=service.db.get_entity("stratum-province-0-0")["properties"]["loyalty"]
    def fail(event):
        raise RuntimeError('Ошибка записи журнала')
    monkeypatch.setattr(service.db,'record_event',fail)
    with pytest.raises(RuntimeError):
        change_loyalty(service,'stratum-province-0-0',15)
    assert service.db.get_entity('stratum-province-0-0')['properties']['loyalty'] == initial
    assert service.db.counts()['Event'] == 0
    assert service.bus.queue == []


def test_demo_diplomacy_turn_and_context_limits(service):
    events = run_demo(service,'stratum-province-0-0')
    assert len({e['action_id'] for e in events}) == 1
    assert service.db.get_entity('stratum-province-0-0')['properties']['loyalty'] == 40
    set_relation(service,'noweria','velaria',50)
    set_relation(service,'velaria','noweria',-30)
    assert service.db.links('velaria','diplomacy')[0]['properties']['value'] == -30
    service.advance_turn()
    assert service.db.meta()['turn'] == 2
    assert service.db.history(turn=2)[0]['sequence'] == 1
    context = ContextBuilder(service.db).build('stratum-province-0-0',event_limit=2,max_chars=500)
    assert len(json.dumps(context,ensure_ascii=False)) <= 500
    assert len(context['recent_history'])+len(context['important_facts']) <= 2
    with pytest.raises(ValueError):
        change_loyalty(service,'stratum-province-0-0',float('nan'))
