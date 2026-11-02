"""Проверки согласованного снимка кампании без запросов к БД."""
from collections import Counter
from modules.geography import validate_geography
from modules.population import validate_parameter


def validate_world(snapshot,tiles,config, require_connected=True):
    entities=snapshot['entities']; links=snapshot['links']; index={e['id']:e for e in entities}
    provinces={e['id']:e for e in entities if e['kind']=='Province'}
    cities={e['id']:e for e in entities if e['kind']=='City'}
    if len(tiles)!=config['width']*config['height']: raise ValueError('Неверное количество клеток')
    coordinates={(t['x'],t['y']) for t in tiles}
    expected={(x,y) for y in range(config['height']) for x in range(config['width'])}
    if coordinates!=expected or len({t['id'] for t in tiles})!=len(tiles):
        raise ValueError('Неверные координаты или повтор ID клетки')
    for state in (e for e in entities if e['kind']=='State'):
        capital=state['properties'].get('capital_id')
        if capital is not None and capital not in cities:
            raise ValueError('Столица государства должна быть городом')
    for e in list(provinces.values())+list(cities.values()):
        for kind in ('owns','includes'):
            rel=[r for r in links if r['target']==e['id'] and r['kind']==kind]
            if len(rel)!=1: raise ValueError(f'{e["name"]}: требуется ровно одна связь {kind}')
        if e['kind']=='Province':
            parents={r['source'] for r in links if r['target']==e['id'] and r['kind'] in ('owns','includes')}
            if len(parents)!=1: raise ValueError('Владелец и государство провинции расходятся')
        if e['kind']=='City':
            parent=next(r['source'] for r in links if r['target']==e['id'] and r['kind']=='includes')
            if e['properties'].get('province_id')!=parent: raise ValueError('Город и политическая провинция расходятся')
    owners={r['target']:r['source'] for r in links if r['kind']=='owns'}
    for e in cities.values():
        # Город может иметь независимого владельца (захват), география следует провинции.
        if owners.get(e['id']) not in index: raise ValueError('Нет владельца города')
    placements={i:{k:e['properties'].get(k) for k in ('tile_id','province_id')} for i,e in cities.items()}
    if len({p['tile_id'] for p in placements.values()})!=len(placements): raise ValueError('Два города на одной клетке')
    check=dict(entities=entities,links=links)
    validate_geography(dict(tiles=tiles,placements=placements),check,require_connected=require_connected)
    groups=[e for e in entities if e['kind']=='SocialGroup' and 'category' in e['properties']]
    counts=Counter()
    for g in groups:
        p=g['properties']; owner=index.get(p['owner_id'])
        if not owner or owner['kind'] not in ('City','Province'): raise ValueError('Страта без территории')
        if p['category'] not in ('Крестьяне','Купцы','Чиновники'): raise ValueError('Неизвестная страта')
        counts[(p['owner_id'],p['category'])]+=1
        rel=[r for r in links if r['source']==g['id'] and r['kind']=='located_in']
        if len(rel)!=1 or rel[0]['target']!=p['owner_id']: raise ValueError('Страта и located_in расходятся')
        for field in ('population','loyalty','welfare','influence'): validate_parameter(field,p[field])
    for identity in set(provinces)|set(cities):
        if any(counts[(identity,c)]!=1 for c in ('Крестьяне','Купцы','Чиновники')):
            raise ValueError(f'Не хватает трёх страт: {identity}')
    return dict(tiles=len(tiles),provinces=len(provinces),cities=len(cities),population=sum(g['properties']['population'] for g in groups))
