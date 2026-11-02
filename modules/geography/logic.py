"""Детерминированный рост связных территорий по взвешенному соседству."""
import heapq
import math
import random
import networkx as nx


def topology(tiles):
    graph=nx.Graph()
    cells={(t['x'],t['y']):t for t in tiles}
    for t in tiles:
        if t['passable']:
            graph.add_node(t['id'])
    for (x,y),t in cells.items():
        if not t['passable']: continue
        for xy in ((x+1,y),(x,y+1)):
            n=cells.get(xy)
            if n and n['passable']:
                graph.add_edge(t['id'],n['id'],weight=(t['cost']+n['cost'])/2 * (.35 if t['road'] and n['road'] else 1))
    return graph


def route(tiles, start, end):
    graph=topology(tiles)
    try:
        path=nx.shortest_path(graph,start,end,weight='weight')
        return dict(path=path,cost=nx.path_weight(graph,path,'weight'),reachable=True)
    except (nx.NetworkXNoPath,nx.NodeNotFound):
        return dict(path=[],cost=None,reachable=False)


def _partition(graph, seeds, rng):
    owners={}; queue=[]
    # Общая стоимость ребра гарантирует связность дерева каждого региона.
    edges=sorted(tuple(sorted((a,b))) for a,b in graph.edges)
    costs={edge:1+rng.random()*2 for edge in edges}
    for identity,node in seeds.items(): heapq.heappush(queue,(0,identity,node))
    while queue:
        distance,identity,node=heapq.heappop(queue)
        if node in owners: continue
        owners[node]=identity
        for neighbor in sorted(graph[node]):
            if neighbor not in owners:
                heapq.heappush(queue,(distance+costs[tuple(sorted((node,neighbor)))],identity,neighbor))
    return owners


def _seeds(graph, identities, rng):
    nodes=sorted(graph)
    if len(nodes)<len(identities): raise ValueError('Недостаточно клеток для провинций')
    selected=[rng.choice(nodes)]
    while len(selected)<len(identities):
        distances=nx.multi_source_dijkstra_path_length(graph,selected)
        selected.append(max(nodes,key=lambda n:(distances[n],n)))
    return dict(zip(sorted(identities),selected))


def generate(content):
    config=content['generation']; rng=random.Random(config['seed'])
    w,h=config['width'],config['height']
    if not 20<=w<=100 or not 20<=h<=100: raise ValueError('Размер карты вне диапазона 20–100')
    tiles=[]
    for y in range(h):
        coast=2+int(1.5+math.sin(y*.48))
        for x in range(w):
            sea=x<coast or x>=w-coast
            terrain='water' if sea else ('river' if x==w//2+int(2*math.sin(y*.3)) else
                'mountain' if math.sin(x*.39)+math.cos(y*.5)>1.25 else
                'forest' if math.sin(x*.27-y*.4)>.35 else 'plain')
            cost={'water':0,'plain':1,'river':2,'forest':2,'mountain':5}[terrain]
            tiles.append(dict(id=f'tile-{x}-{y}',x=x,y=y,terrain=terrain,province_id='',road=False,
                              resources=[] if sea else [dict(plain='зерно',forest='лес',mountain='железо',river='рыба')[terrain]],
                              passable=not sea,cost=cost))
    graph=topology(tiles)
    states=[e['id'] for e in content['entities'] if e['kind']=='State']
    state_seeds=_seeds(graph,states,rng)
    state_cells=_partition(graph,state_seeds,rng)
    ownership={r['target']:r['source'] for r in content['links'] if r['kind']=='owns'}
    provinces=[e for e in content['entities'] if e['kind']=='Province']
    province_cells={}
    for state in sorted(states):
        sub=graph.subgraph([n for n in graph if state_cells[n]==state])
        ids=[p['id'] for p in provinces if ownership[p['id']]==state]
        province_cells.update(_partition(sub,_seeds(sub,ids,rng),rng))
    by_id={t['id']:t for t in tiles}
    for identity,pid in province_cells.items(): by_id[identity]['province_id']=pid
    placements={}; occupied=set()
    for city in sorted((e for e in content['entities'] if e['kind']=='City'),key=lambda e:e['id']):
        pid=next(r['source'] for r in content['links'] if r['kind']=='includes' and r['target']==city['id'])
        candidates=[t for t in tiles if t['province_id']==pid and t['id'] not in occupied]
        candidates.sort(key=lambda t:(t['terrain']=='mountain',-sum(by_id[n]['terrain']=='river' for n in graph[t['id']]),t['cost'],rng.random()))
        tile=candidates[0]
        if tile['terrain']=='mountain':
            tile['terrain']='plain'; tile['cost']=1
        occupied.add(tile['id']); placements[city['id']]=dict(tile_id=tile['id'],province_id=pid)
    # Минимальное дерево соединяет все города, включая соседние государства.
    complete=nx.Graph()
    for city,p in placements.items(): complete.add_node(city)
    ids=sorted(placements)
    for i,a in enumerate(ids):
        ta=by_id[placements[a]['tile_id']]
        for b in ids[i+1:]:
            tb=by_id[placements[b]['tile_id']]
            complete.add_edge(a,b,weight=abs(ta['x']-tb['x'])+abs(ta['y']-tb['y']))
    for a,b in nx.minimum_spanning_tree(complete).edges:
        for node in nx.shortest_path(graph,placements[a]['tile_id'],placements[b]['tile_id'],weight='weight'):
            by_id[node]['road']=True
    result=dict(tiles=tiles,placements=placements,config=config)
    validate_geography(result,content)
    return result


def validate_geography(result, content, require_connected=True):
    tiles=result['tiles']; graph=topology(tiles); coords={(t['x'],t['y']) for t in tiles}
    if len(coords)!=len(tiles): raise ValueError('Повтор координат карты')
    by_id={t['id']:t for t in tiles}
    provinces={e['id']:e for e in content['entities'] if e['kind']=='Province'}
    owners={r['target']:r['source'] for r in content['links'] if r['kind']=='owns'}
    for t in tiles:
        if t['passable'] and t['province_id'] not in provinces: raise ValueError('Суша без провинции')
        if t['road'] and not t['passable']: raise ValueError('Дорога на воде')
    for pid,p in provinces.items():
        nodes=[t['id'] for t in tiles if t['province_id']==pid]
        if not nodes or not nx.is_connected(graph.subgraph(nodes)): raise ValueError(f'Разорвана провинция {pid}')
        capital=result['placements'].get(p['properties'].get('capital_id'))
        if not capital or capital['province_id']!=pid: raise ValueError('Столица вне провинции')
    for state in {owners[p] for p in provinces}:
        nodes=[t['id'] for t in tiles if owners.get(t['province_id'])==state]
        if require_connected and nodes and not nx.is_connected(graph.subgraph(nodes)): raise ValueError(f'Разорвана территория {state}')
    for city,p in result['placements'].items():
        t=by_id.get(p['tile_id'])
        if not t or t['province_id']!=p['province_id'] or t['terrain'] in ('water','mountain'):
            raise ValueError(f'Некорректное размещение города {city}')
