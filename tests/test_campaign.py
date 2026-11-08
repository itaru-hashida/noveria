import json
from pathlib import Path
import pytest
import networkx as nx
from modules.geography import generate,topology,route,validate_geography
from modules.population import aggregate
from core.integrity import validate_world
from core.db import WorldDB
from core.world_service import WorldService
from core.events import EventBus
from loader import load_world,validate_content
from modules.economy import Economy


def test_generator_reproducible():
    content=json.loads(Path('content/initial_world.json').read_text(encoding='utf-8'))
    a=generate(content); b=generate(content)
    assert a==b
    assert len(a['tiles'])==1200
    assert len({(t['x'],t['y']) for t in a['tiles']})==1200
    validate_geography(a,content)
    graph=topology(a['tiles'])
    assert nx.is_connected(graph)
    ids=list(a['placements'])
    result=route(a['tiles'],a['placements'][ids[0]]['tile_id'],a['placements'][ids[-1]]['tile_id'])
    assert result['reachable'] and result['cost']>0
    assert all('камень' in tile['resources'] for tile in a['tiles'] if tile['terrain']=='mountain')


def test_generated_stone_deposits_wait_for_private_demand(tmp_path):
    with WorldDB(tmp_path/'stone.lbug') as db:
        load_world(db)
        WorldService(db, EventBus()).initialize_economy()
        engine = Economy.load(db)
        assert any('камень' in tile['resources'] for tile in engine.tiles.values())
        # До первого строительного заказа добыча камня не имеет сбыта и не
        # должна появляться как заранее обречённая на закрытие фирма.
        assert not [enterprise for enterprise in engine.enterprises.values()
                    if enterprise['good'] == 'stone']


def test_generator_reproducible_across_processes():
    import os
    import subprocess
    import sys
    code = (
        "import hashlib,json; from pathlib import Path; from modules.geography import generate; "
        "world=generate(json.loads(Path('content/initial_world.json').read_text(encoding='utf-8'))); "
        "print(hashlib.sha256(json.dumps(world,sort_keys=True).encode()).hexdigest())"
    )
    results = [subprocess.check_output([sys.executable,'-c',code],
               env=dict(os.environ,PYTHONHASHSEED=seed),text=True).strip() for seed in ('1','42')]
    assert results[0]==results[1]


def test_population_aggregation(service):
    snap=service.db.snapshot()
    summary=validate_world(snap,service.tiles(),service.db.campaign())
    groups=[e for e in snap['entities'] if e['kind']=='SocialGroup']
    assert len(groups)==3*(26+30)
    province=aggregate(snap['entities'],snap['links'],'province-0')
    city=aggregate(snap['entities'],snap['links'],'city-0-0')
    rural=[g['properties'] for g in groups if g['properties']['owner_id']=='province-0']
    assert sum(r['population'] for r in province)==sum(r['population'] for r in city)+sum(g['population'] for g in rural)
    assert summary['population']==sum(g['properties']['population'] for g in groups)
    for row in province:
        source=[g['properties'] for g in groups if g['properties']['owner_id'] in ('province-0','city-0-0') and g['properties']['category']==row['category']]
        assert row['loyalty']==pytest.approx(sum(g['population']*g['loyalty'] for g in source)/row['population'])
    for invalid in (-1,'fifty',float('nan')):
        with pytest.raises(ValueError): service.change_parameter(groups[0]['id'],'population',invalid)


def test_month_idempotency_year_and_rollback(service):
    config=service.db.campaign(); config['month']=12; service.db.save_campaign(config)
    group='stratum-province-0-0'
    service.schedule_social(group,'loyalty',63,command_id='order-1')
    report=service.advance_turn(command_id='month-1')
    assert report['month']==1 and report['year']==2
    assert [p['name'] for p in report['phases']]==['commands','economy','integrity','snapshot']
    assert service.db.get_entity(group)['properties']['loyalty']==63
    assert service.advance_turn(command_id='month-1')==report
    count=service.db.counts()['Event']
    service.schedule_social(group,'loyalty',22,command_id='order-2')
    def fail(ctx): raise ValueError('phase failure')
    service.turns.register_phase('failure',fail)
    with pytest.raises(ValueError): service.advance_turn(command_id='month-failed')
    assert service.db.meta()['turn']==2
    assert service.db.get_entity(group)['properties']['loyalty']==63
    assert service.db.campaign()['status']=='ready'
    assert service.db.turn_result('month-failed') is None
    assert service.db.counts()['Event']==count+1
    assert service.db.pending_commands()[0]['id']=='order-2'


def test_campaign_reopen(tmp_path):
    path=tmp_path/'campaign.lbug'
    with WorldDB(path) as db:
        load_world(db); tiles=db.tiles(); city=db.get_entity('city-0-0')
        service=WorldService(db,EventBus()); report=service.advance_turn(command_id='persist')
    with WorldDB(path) as db:
        assert not load_world(db,content_path='missing.json')
        assert db.tiles()==tiles and db.get_entity('city-0-0')==city
        assert db.campaign()['seed']==73129
        assert WorldService(db,EventBus()).advance_turn(command_id='persist')==report
        assert db.meta()['turn']==2


def test_history_filters(service):
    service.change_parameter('city-0-0','description','test',source='admin',action_id='audit-command')
    assert len(service.db.history(action_id='audit-command',source='admin',turn_from=1,turn_to=1))==1
    assert not service.db.history(turn_from=2)
    assert any(e['id']=='city-0-0' for e in service.db.list_entities(search='city-0-0'))


def test_city_transfer_population_follows_owner(service):
    snapshot = service.db.snapshot()
    def population(identity):
        return sum(row['population'] for row in aggregate(snapshot['entities'], snapshot['links'], identity))
    city = population('city-0-0')
    before = {identity: population(identity) for identity in ('noweria','velaria','province-0')}
    service.transfer_territory('city-0-0', 'velaria')
    snapshot = service.db.snapshot()
    assert population('noweria') == before['noweria'] - city
    assert population('velaria') == before['velaria'] + city
    assert population('province-0') == before['province-0']
    assert sum(population(e['id']) for e in snapshot['entities'] if e['kind']=='State') == sum(
        e['properties']['population'] for e in snapshot['entities'] if e['kind']=='SocialGroup')


def test_territory_creation_history_and_rollback(service):
    snapshot = service.db.snapshot()
    occupied = {e['properties']['tile_id'] for e in snapshot['entities'] if e['kind']=='City'}
    tile = next(t for t in service.tiles() if t['province_id']=='province-0'
                and t['terrain'] not in ('water','mountain') and t['id'] not in occupied)
    properties = dict(province_id='province-0', tile_id=tile['id'])
    event = service.create_entity('City', 'Новый город', properties, entity_id='new-city', action_id='create-city')
    assert event['action_id'] == 'create-city'
    assert len([e for e in service.db.snapshot()['entities'] if e['properties'].get('owner_id')=='new-city']) == 3
    count = service.db.counts()
    with pytest.raises(ValueError):
        service.create_entity('City', 'На занятой клетке', properties, entity_id='invalid-city')
    assert service.db.counts() == count
    assert not service.db.list_entities(search='invalid-city')


def test_world_integrity_rejects_invalid_grid_and_parent(service):
    import copy
    snapshot = service.db.snapshot()
    tiles = copy.deepcopy(service.tiles())
    tiles[0]['x'] = -1
    with pytest.raises(ValueError, match='координаты'):
        validate_world(snapshot, tiles, service.db.campaign())
    for link in snapshot['links']:
        if link['kind']=='includes' and link['target']=='province-0':
            link['source']='velaria'
    with pytest.raises(ValueError, match='расходятся'):
        validate_world(snapshot, service.tiles(), service.db.campaign())


def test_route_unreachable_and_empty_turn_id(service):
    tiles = [dict(id='a',x=0,y=0,passable=True,cost=1,road=False),
             dict(id='b',x=2,y=0,passable=True,cost=1,road=False)]
    assert route(tiles,'a','b') == dict(path=[],cost=None,reachable=False)
    assert not route(tiles,'missing','b')['reachable']
    with pytest.raises(ValueError, match='command_id'):
        service.advance_turn(command_id='')
    assert service.db.meta()['turn'] == 1
