"""Явное обновление авторского сценария; не вызывается при старте игры."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / 'content/initial_world.json'
    old = json.loads(path.read_text(encoding='utf-8'))
    canon = (ROOT / 'docs' / 'noweria.md').read_text(encoding='utf-8')
    capitals = ['Лунария','Феррата','Валларис','Эниополис','Эллина','Соларис','Оникс','Фирополис']
    keep = {f'city-{i}-0' for i in range(8)}
    removed = {e['id'] for e in old['entities'] if e['kind']=='SocialGroup' or
               (e['kind']=='City' and e['id'].startswith('city-') and e['id'] not in keep)}
    entities = [e for e in old['entities'] if e['id'] not in removed]
    links = [r for r in old['links'] if r['source'] not in removed and r['target'] not in removed]
    by_id = {e['id']:e for e in entities}
    lines = [line for line in canon.splitlines() if re.match(r'\*\*\d+\.',line)]
    for i,line in enumerate(lines):
        p = by_id[f'province-{i}']
        p['name'] = line.split('. ',1)[1].split('**')[0]
        p['properties'] = dict(canon=True,description=line.split(' — ',1)[1],
                               resources=line.split('Ресурсы: ',1)[1].split('.')[0].split(', '))
        by_id[f'city-{i}-0']['name'] = capitals[i]
    # Правитель сохранён из сценария: текущий сокращённый канон его не уточняет.
    by_id['noweria']['properties'] = dict(canon=True,description='Главное государство сценария. '+canon.split('**Итого:**')[-1].strip())
    def entity(i,k,n,**p):
        e=dict(id=i,kind=k,name=n,properties=p); entities.append(e); by_id[i]=e
    def link(a,b,k,**p):
        links.append(dict(source=a,target=b,kind=k,properties=p))
    entity('senate','Institution','Сенат',canon=True,description='Политический институт Лунарии, упомянутый в актуальном каноне.')
    link('senate','noweria','institution_of')
    scenarios = [
        ('septaria','Септарийский Доминион','Милитаризированная аристократия горных долин.','Северин Септар',
         ['Септара','Кальдис','Веррум','Дорсена','Тарквель'],['Септариум','Кальдория','Верруна','Дорс','Тарквена'],['железо','камень']),
        ('kasseria','Республика Кассерия','Торговая республика речных равнин.','Ливия Кассера',
         ['Кассера','Меркатия','Салинор','Ривента'],['Кассериум','Меркат','Салина','Ривент'],['зерно','соль']),
        ('valtoria','Валторийский Принципат','Централизованная монархия плодородных предгорий.','Аврел Валтор',
         ['Валтор','Луценна','Оливера','Кастрен','Флавента'],['Валториум','Луцен','Оливар','Кастренум','Флавен'],['лес','зерно']),
    ]
    for sid,name,desc,ruler,provinces,cities,resources in scenarios:
        entity(sid,'State',name,canon=False,description=desc)
        entity(sid+'-ruler','Character',ruler,canon=False)
        link(sid+'-ruler',sid,'rules')
        for i,(p,c) in enumerate(zip(provinces,cities)):
            pid=f'{sid}-p{i}'; cid=f'{sid}-c{i}'
            entity(pid,'Province',p,canon=False,description=desc,resources=resources)
            entity(cid,'City',c,canon=False)
            for kind in ('includes','owns'): link(sid,pid,kind)
            link(pid,cid,'includes'); link(sid,cid,'owns')
        a,b=sorted(('noweria',sid)); link(a,b,'diplomacy',value=-15 if sid=='septaria' else 15)
    for p in [e for e in entities if e['kind']=='Province']:
        cities=[r['target'] for r in links if r['source']==p['id'] and r['kind']=='includes']
        p['properties']['capital_id']=cities[0]
        p['properties'].setdefault('resources',['лес','камень'])
    for s in [e for e in entities if e['kind']=='State']:
        provinces=[r['target'] for r in links if r['source']==s['id'] and r['kind']=='includes']
        s['properties']['capital_id']=by_id[provinces[0]]['properties']['capital_id']
    data=dict(version=2,generation=dict(seed=73129,width=40,height=30,year=1,month=1),entities=entities,links=links)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': main()
