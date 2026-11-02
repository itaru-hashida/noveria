import math
import random

CATEGORIES=('Крестьяне','Купцы','Чиновники')


def validate_parameter(parameter,value):
    if parameter in ('population','loyalty','welfare','influence'):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError('Социальный показатель должен быть конечным неотрицательным числом')
        if parameter=='population' and not isinstance(value,int): raise ValueError('Население должно быть целым')
        if parameter in ('welfare','influence') and value>100: raise ValueError('Показатель должен быть не выше 100')


def create_groups(content, geography):
    rng=random.Random(geography['config']['seed']+1); groups=[]
    for entity in sorted(content['entities'],key=lambda e:e['id']):
        if entity['kind'] not in ('City','Province'): continue
        if entity['kind']=='Province':
            land=[t for t in geography['tiles'] if t['province_id']==entity['id']]
            total=sum(700 if t['terrain']=='plain' else 350 for t in land)
            shares=(.83,.10,.07)
        else:
            total=rng.randint(3500,16000); shares=(.18,.57,.25)
        sizes=[int(total*s) for s in shares]; sizes[-1]=total-sum(sizes[:-1])
        for i,category in enumerate(CATEGORIES):
            groups.append(dict(id=f'stratum-{entity["id"]}-{i}',kind='SocialGroup',
                name=f'{category} · {entity["name"]}',properties=dict(category=category,owner_id=entity['id'],
                population=sizes[i],loyalty=rng.randint(50,85),welfare=rng.randint(30,75),influence=[20,45,70][i],mvp=True)))
    return groups


def aggregate(entities, links, identity):
    index={e['id']:e for e in entities}
    selected={identity}
    if index[identity]['kind']=='State':
        selected.update(r['target'] for r in links if r['source']==identity and r['kind']=='owns'
                        and index[r['target']]['kind'] in ('Province','City'))
    else:
        selected.update(r['target'] for r in links if r['source'] in selected and r['kind']=='includes'
                        and index[r['target']]['kind']=='City')
    result=[]
    for category in CATEGORIES:
        groups=[e['properties'] for e in entities if e['kind']=='SocialGroup' and e['properties'].get('owner_id') in selected and e['properties'].get('category')==category]
        count=sum(g['population'] for g in groups)
        row=dict(category=category,population=count)
        for field in ('loyalty','welfare','influence'):
            row[field]=sum(g['population']*g[field] for g in groups)/count if count else 0
        result.append(row)
    return result
