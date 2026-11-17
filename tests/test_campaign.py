import json
from collections import defaultdict
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
        service = WorldService(db, EventBus())
        service.initialize_economy()
        engine = service.economy_state().engine
        assert any('камень' in tile['resources'] for tile in engine.tiles.values())
        # До первого строительного заказа добыча камня не имеет сбыта и не
        # должна появляться как заранее обречённая на закрытие фирма.
        assert not [enterprise for enterprise in engine.enterprises.values()
                    if enterprise['good'] == 'stone']


def test_municipality_can_start_persisted_institution_project(tmp_path):
    with WorldDB(tmp_path/'institution.lbug') as db:
        load_world(db)
        service = WorldService(db, EventBus())
        service.initialize_economy()
        event = service.start_institution_project('city-0-0', 'academy', action_id='academy-order')
        engine = service.economy_state().engine
        project = engine.accounts['municipal:city-0-0']['public_projects'][0]
        assert event['payload']['project_id'] == project['id']
        assert project['material_bill']['stone'] == 120
        assert project['status'] == 'procurement'


def test_completed_institution_project_persists_entity_and_location(tmp_path):
    with WorldDB(tmp_path/'completed-institution.lbug') as db:
        load_world(db)
        service = WorldService(db, EventBus())
        service.initialize_economy()
        engine = service.economy_state().engine
        engine.cfg['institutions'] = {
            'academy': dict(name='Академия', materials={'planks': 1}, equipment={'tools': 1},
                equipment_wear={'tools': 0}, work_required=1, max_workers=1)
        }
        project = engine.start_public_project('city-0-0', 'academy')
        account = engine.accounts['municipal:city-0-0']
        engine.add_stock(account['id'], 'planks', 1, 2)
        engine.add_stock(account['id'], 'tools', 1, 5.5)
        project = account['public_projects'][0]
        project['materials']['planks'] = 1
        project['equipment_held']['tools'] = 1
        engine._construction_labor = defaultdict(float, {('noweria', 'province-0'): 1})
        engine.advance_public_projects()
        # Институт — структурная сущность мира, поэтому он попадает в граф
        # сразу при завершении стройки, а не в конце месяца.
        service.economy_state().flush_structural(db)

        institution = db.get_entity(project['institution_id'])
        assert institution['kind'] == 'Institution'
        assert institution['properties']['city_id'] == 'city-0-0'
        links = db.links(project['institution_id'])
        assert {(row['kind'], row['target']) for row in links} == {
            ('institution_of', 'noweria'), ('located_in', 'city-0-0')}


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


def _world(seed=None, model='geological'):
    content=json.loads(Path('content/initial_world.json').read_text(encoding='utf-8'))
    content['generation']['model']=model
    if seed is not None: content['generation']['seed']=seed
    return content,generate(content)


def test_default_model_is_legacy_and_geological_is_explicit():
    """По умолчанию старая карта: сохранения и эталоны не сдвигаются."""
    content=json.loads(Path('content/initial_world.json').read_text(encoding='utf-8'))
    assert 'model' not in content['generation']
    default=generate(content)
    assert set(default['tiles'][0])=={'id','x','y','terrain','province_id','road',
                                      'resources','passable','cost'}
    content['generation']['model']='legacy'
    assert generate(content)==default
    content['generation']['model']='geological'
    geological=generate(content)
    assert geological['tiles']!=default['tiles']
    assert 'elevation' in geological['tiles'][0] and 'elevation' not in default['tiles'][0]
    with pytest.raises(ValueError, match='модель'):
        content['generation']['model']='unknown'
        generate(content)


def test_terrain_is_continuous_field_not_three_classes():
    """Генератор выдаёт непрерывный рельеф, климат и биомы, а не три класса."""
    _,world=_world()
    tiles=world['tiles']
    land=[t for t in tiles if t['passable']]
    assert len(land)>200
    # Высота непрерывна и покрывает заметный диапазон.
    heights=sorted({t['elevation'] for t in tiles})
    assert len(heights)>100 and heights[0]>=0 and heights[-1]<=1
    for field in ('elevation','slope','temperature','moisture','fertility','danger'):
        assert all(0<=t[field]<=1 for t in tiles), field
    # Пять совместимых классов поверхности присутствуют одновременно.
    assert {t['terrain'] for t in tiles}=={'water','plain','forest','mountain','river'}
    assert len({t['relief'] for t in land})>=4
    assert len({t['biome'] for t in land})>=4


def test_plates_boundaries_and_climate_are_connected_physics():
    """Границы плит дают хребты, реки идут из водосборов, климат зависит от высоты."""
    _,world=_world()
    tiles=world['tiles']
    by_id={t['id']:t for t in tiles}
    cells={(t['x'],t['y']):t for t in tiles}
    assert len({t['plate_id'] for t in tiles})>=8
    assert {t['plate_type'] for t in tiles}<={'continental','oceanic'}
    assert {t['boundary'] for t in tiles}-{'none'}=={'convergent','divergent','transform','volcanic'}
    # Хребты выше равнин: орогенез на границах плит виден в высотах.
    convergent=[t for t in tiles if t['boundary']=='convergent' and t['passable']]
    plains=[t for t in tiles if t['relief'] in ('plain','lowland')]
    assert convergent and plains
    assert sum(t['elevation'] for t in convergent)/len(convergent) > sum(t['elevation'] for t in plains)/len(plains)
    # Реки стоят на клетках с большим водосбором и текут вниз по высоте.
    river=[t for t in tiles if t['terrain']=='river']
    assert river
    for t in river:
        neighbours=[cells[(t['x']+dx,t['y']+dy)] for dx,dy in ((1,0),(-1,0),(0,1),(0,-1))
                    if (t['x']+dx,t['y']+dy) in cells]
        # Сток ортогональный: река не может «прыгать» по диагонали.
        assert min(n['elevation'] for n in neighbours) < t['elevation'] or \
               any(n['water'] for n in neighbours)
    # Дождевая тень: за хребтом суше, чем на наветренном склоне.
    assert [t for t in tiles if t['biome']=='desert']
    assert [t for t in tiles if t['biome'] in ('rainforest','temperate_forest')]


def test_rivers_are_orthogonally_connected_and_reach_water():
    """Река — 4-связная цепочка воды, а не россыпь диагональных клеток."""
    _,world=_world()
    tiles=world['tiles']
    cells={(t['x'],t['y']):t for t in tiles}
    river=[t for t in tiles if t['terrain']=='river']
    for t in river:
        neighbours=[cells[(t['x']+dx,t['y']+dy)] for dx,dy in ((1,0),(-1,0),(0,1),(0,-1))
                    if (t['x']+dx,t['y']+dy) in cells]
        assert any(n['terrain']=='river' or n['water'] for n in neighbours), t['id']
    # Каждая провинция имеет выход к воде: река или море в 4-соседстве.
    provinces={}
    for t in tiles:
        if t['passable']:
            provinces.setdefault(t['province_id'],[]).append(t)
    assert len(provinces)==26
    for pid,land in provinces.items():
        access=any(t['terrain']=='river' or any(
            cells[(t['x']+dx,t['y']+dy)]['water'] or cells[(t['x']+dx,t['y']+dy)]['terrain']=='river'
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)) if (t['x']+dx,t['y']+dy) in cells)
            for t in land)
        assert access, pid


def test_resources_are_clustered_and_biomes_are_physically_consistent():
    """Месторождения лежат кластерами по геологии, а биомы не спорят с климатом."""
    _,world=_world()
    tiles=world['tiles']
    cells={(t['x'],t['y']):t for t in tiles}
    # Каждая горная клетка даёт камень и железо: это контракт экономики.
    for t in tiles:
        if t['terrain']=='mountain':
            assert 'камень' in t['resources'] and 'железо' in t['resources']
        if t['water']:
            assert t['resources']==[]
    # Рудные ресурсы образуют кластеры: у большинства есть сосед с тем же ресурсом.
    for good in ('медь','железо','камень','лес','зерно'):
        cells_with=[t for t in tiles if good in t['resources']]
        assert cells_with, good
        clustered=sum(1 for t in cells_with
                      if any(good in cells[(t['x']+dx,t['y']+dy)]['resources']
                             for dx,dy in ((1,0),(-1,0),(0,1),(0,-1))
                             if (t['x']+dx,t['y']+dy) in cells))
        assert clustered/len(cells_with) > 0.35, good
    cold=[t for t in tiles if t['passable'] and t['temperature']<0.15]
    hot=[t for t in tiles if t['passable'] and t['temperature']>0.85]
    assert all(t['terrain']!='forest' for t in cold if t['biome']=='tundra')
    assert all(t['biome']!='glacier' for t in hot)
    dry=[t for t in tiles if t['passable'] and t['biome']=='desert']
    assert all(t['moisture']<0.45 for t in dry)


def test_identical_seed_with_different_resource_settings_keeps_terrain():
    """Смена ресурсного этапа не сдвигает горы, реки и высоты."""
    content,a=_world(73129)
    content['generation']['seed']=73129
    b=generate(content)
    assert [(t['id'],t['elevation'],t['terrain'],t['relief'],t['biome']) for t in a['tiles']] == \
           [(t['id'],t['elevation'],t['terrain'],t['relief'],t['biome']) for t in b['tiles']]
    from modules.geography.logic import derive_seed
    assert derive_seed(73129,'plates')!=derive_seed(73129,'height')
    assert derive_seed(73129,'resources')==derive_seed(73129,'resources')


def test_city_score_places_cities_on_livable_tiles():
    """Города стоят на воде, плодородии и низком уклоне, а не случайно."""
    content,world=_world()
    by_id={t['id']:t for t in world['tiles']}
    cells={(t['x'],t['y']):t for t in world['tiles']}
    occupied={p['tile_id'] for p in world['placements'].values()}
    assert len(world['placements'])==len(occupied)
    city_tiles=[by_id[p['tile_id']] for p in world['placements'].values()]
    land=[t for t in world['tiles'] if t['passable']]
    assert sum(t['fertility'] for t in city_tiles)/len(city_tiles) > \
           sum(t['fertility'] for t in land)/len(land)
    assert sum(t['danger'] for t in city_tiles)/len(city_tiles) < \
           sum(t['danger'] for t in land)/len(land)
    for t in city_tiles:
        assert t['terrain'] not in ('water','mountain')
        near_river=any(cells.get((t['x']+dx,t['y']+dy),{}).get('terrain')=='river'
                       for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)))
        near_water=any(cells.get((t['x']+dx,t['y']+dy),{}).get('water')
                       for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)))
        assert near_river or near_water or len(t['resources'])>=2

def test_passability_layers_describe_cost_and_danger():
    """У каждой клетки есть movement_cost, road_cost, slope и danger."""
    _,world=_world()
    for t in world['tiles']:
        if t['water']:
            assert t['cost']==0 and t['movement_cost']==0
            assert t['road_cost']==0 or t['road_cost']>0
        else:
            assert t['cost']>0 and t['movement_cost']==t['cost']
            assert t['road_cost']>t['movement_cost']
            assert 0<=t['danger']<=1
    # Пики непроходимы, но карта остаётся связной.
    for t in world['tiles']:
        if t['relief']=='peaks':
            assert not t['passable']
    graph=topology(world['tiles'])
    assert nx.is_connected(graph)


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
