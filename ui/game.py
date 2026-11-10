import streamlit as st
from uuid import uuid4
from modules.population import aggregate
from modules.geography import route
from core.integrity import validate_world
from core.labels import territory_labels
from ui.map_view import map_figure, LABELS

MONTHS=['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь']


def card(service,identity,snapshot=None):
    if not service.db.campaign():
        from ui.app import entity_card
        entity_card(service.db, identity)
        return
    snapshot=snapshot or service.db.snapshot(); index={e['id']:e for e in snapshot['entities']}; links=snapshot['links']
    tiles=service.tiles(); by_id={t['id']:t for t in tiles}; owners={r['target']:r['source'] for r in links if r['kind']=='owns'}
    if identity in by_id:
        t=by_id[identity]; st.subheader(f'Клетка {t["x"]}, {t["y"]}')
        st.write(LABELS[t['terrain']], ' · Дорога' if t['road'] else ' · Без дороги')
        st.write('Ресурсы: '+(', '.join(t['resources']) or 'нет'))
        if t['province_id']:
            st.write('Провинция: '+index[t['province_id']]['name'])
            st.write('Государство: '+index[owners[t['province_id']]]['name'])
            if st.button('Открыть провинцию',key='tile-province'):
                st.session_state['map-selection']=t['province_id']; st.rerun()
        cities=[e for e in index.values() if e['kind']=='City' and e['properties'].get('tile_id')==identity]
        for c in cities:
            if st.button('Город: '+c['name'],key='tile-'+c['id']): st.session_state['map-selection']=c['id']; st.rerun()
        return
    e=index.get(identity)
    if not e: return
    st.subheader(e['name']); p=e['properties']; st.caption(e['kind'])
    st.write(p.get('description',''))
    if e['kind'] in ('State','Province','City'):
        rows=aggregate(snapshot['entities'],links,identity)
        st.metric('Население',f'{sum(r["population"] for r in rows):,}'.replace(',',' '))
        st.dataframe([{'Страта':r['category'],'Жители':r['population'],'Лояльность':round(r['loyalty'],1),'Благосостояние':round(r['welfare'],1),'Влияние':round(r['influence'],1)} for r in rows],hide_index=True)
    if e['kind']=='State':
        provinces=[r['target'] for r in links if r['source']==identity and r['kind']=='owns' and index[r['target']]['kind']=='Province']
        rulers=[index[r['source']]['name'] for r in links if r['target']==identity and r['kind']=='rules']
        st.write('Правитель: '+(', '.join(rulers) or 'не назначен')); st.write(f'Провинций: {len(provinces)}')
        st.write('Столицы провинций: '+', '.join(index[index[i]['properties']['capital_id']]['name'] for i in provinces))
        xy={(t['x'],t['y']):t for t in tiles}; neighbors=set()
        for t in tiles:
            if owners.get(t['province_id'])!=identity: continue
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
                n=xy.get((t['x']+dx,t['y']+dy))
                other=owners.get(n['province_id']) if n else None
                if other and other!=identity: neighbors.add(other)
        st.write('Географические соседи: '+', '.join(index[i]['name'] for i in sorted(neighbors)))
        for r in links:
            if r['kind']=='diplomacy' and identity in (r['source'],r['target']):
                neighbor=r['target'] if r['source']==identity else r['source']
                st.write(f'{index[neighbor]["name"]}: отношения {r["properties"].get("value",0)}')
    if e['kind']=='Province':
        st.write('Владелец: '+index[owners[identity]]['name'])
        st.write('Столица: '+index[p['capital_id']]['name'])
        land=[t for t in tiles if t['province_id']==identity]
        st.write(f'Площадь: {len(land)} клеток')
        st.write('Рельеф: '+', '.join(sorted({LABELS[t['terrain']] for t in land})))
        st.write('Ресурсы: '+', '.join(sorted({r for t in land for r in t['resources']})))
        xy={(t['x'],t['y']):t for t in tiles}; neighbors=set()
        for t in land:
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
                n=xy.get((t['x']+dx,t['y']+dy))
                if n and n['province_id'] and n['province_id']!=identity: neighbors.add(n['province_id'])
        st.write('Соседние провинции: '+', '.join(index[i]['name'] for i in sorted(neighbors)))
    if e['kind']=='City':
        pid=p.get('province_id'); t=by_id.get(p.get('tile_id'))
        if pid: st.write('Провинция: '+index[pid]['name'])
        if t: st.write(f'Координаты: {t["x"]}, {t["y"]} · '+('Дорога доступна' if t['road'] else 'Дороги нет'))
        other_cities=[c['id'] for c in index.values() if c['kind']=='City' and c['id']!=identity and 'tile_id' in c['properties']]
        if other_cities:
            destination=st.selectbox('Маршрут в город',other_cities,format_func=lambda i:index[i]['name'],key='card-route-'+identity)
            if st.button('Рассчитать путь',key='card-route-button-'+identity):
                result=route(tiles,p['tile_id'],index[destination]['properties']['tile_id'])
                st.write(f'Клеток: {len(result["path"])} · стоимость: {result["cost"]}' if result['reachable'] else 'Нет пути')
    if e['kind']=='SocialGroup': st.dataframe([p])
    related=[r for r in links if identity in (r['source'],r['target'])]
    with st.expander('Связанные объекты'):
        for r in related[:30]:
            other=r['target'] if r['source']==identity else r['source']
            if st.button(f'{r["kind"]} · {index[other]["name"]}',key=f'card-{identity}-{other}-{r["kind"]}'):
                st.session_state['map-selection']=other; st.rerun()


def play(service):
    st.header('Новерская Республика')
    config=service.db.campaign()
    if not config:
        st.warning('Это старое сохранение без географии. Используйте команду пересоздания из README.'); return
    snapshot=service.db.snapshot(); states=[e for e in snapshot['entities'] if e['kind']=='State']
    states.sort(key=lambda e:e['id']!='noweria'); index={e['id']:e for e in snapshot['entities']}
    selected=st.selectbox('Ваше государство',[e['id'] for e in states],format_func=lambda i:index[i]['name'])
    left,right=st.columns([3,1]); left.subheader(f'{MONTHS[config["month"]-1]}, {config["year"]} год')
    st.session_state.setdefault('turn-command',str(uuid4()))
    if right.button('Завершить месяц',type='primary'):
        try:
            report=service.advance_turn(command_id=st.session_state['turn-command'])
            st.session_state['turn-command']=str(uuid4()); st.session_state['last-report']=report; st.rerun()
        except (ValueError,RuntimeError) as error: st.error(str(error))
    if 'last-report' in st.session_state: st.success(st.session_state['last-report']['summary'])
    st.caption(f'{len(states)} государств · {sum(e["kind"]=="Province" for e in snapshot["entities"])} провинций · {sum(e["kind"]=="City" for e in snapshot["entities"])} городов')
    layer=st.radio('Слой',['Государства','Провинции','Рельеф','Ресурсы','Дороги','Население'],horizontal=True)
    route_path=[]
    with st.expander('Маршрут между городами'):
        cities=[e['id'] for e in snapshot['entities'] if e['kind']=='City']
        a=st.selectbox('Откуда',cities,format_func=lambda i:index[i]['name']); b=st.selectbox('Куда',cities,index=min(1,len(cities)-1),format_func=lambda i:index[i]['name'])
        if st.checkbox('Показать маршрут'):
            result=route(service.tiles(),index[a]['properties']['tile_id'],index[b]['properties']['tile_id'])
            route_path=result['path']; st.write(f'Стоимость: {result["cost"]:.1f} · клеток: {len(route_path)}' if result['reachable'] else 'Маршрут недоступен')
    col,panel=st.columns([3,2])
    with col:
        pops={e['id']:sum(r['population'] for r in aggregate(snapshot['entities'],snapshot['links'],e['id'])) for e in snapshot['entities'] if e['kind']=='Province'}
        event=st.plotly_chart(map_figure(service.tiles(),snapshot,layer,selected,route_path,pops),key='world-map',
            on_select='rerun',selection_mode=('points',),width='stretch',config={'scrollZoom':True,'displaylogo':False})
        points=event.selection.points
        if points and points != st.session_state.get('last-map-points'):
            st.session_state['last-map-points'] = points
            choice=points[-1].get('customdata')
            if choice: st.session_state['map-selection']=choice
        options=[e['id'] for e in snapshot['entities'] if e['kind'] in ('State','Province','City')]
        chosen=st.selectbox('Перейти к объекту',options,index=options.index(selected),format_func=lambda i:index[i]['name'])
        if st.button('Открыть карточку'): st.session_state['map-selection']=chosen
    with panel: card(service,st.session_state.get('map-selection',selected),snapshot)
    st.subheader('Последние события')
    for e in service.db.history(limit=5): st.write(f'Ход {e["turn"]} · {e["summary"]}')


def directory(service,kind):
    snapshot=service.db.snapshot(); index={e['id']:e for e in snapshot['entities']}
    query=st.text_input('Поиск по названию или ID')
    choices=[e['id'] for e in snapshot['entities'] if e['kind'] in kind and (query.lower() in e['name'].lower() or query in e['id'])]
    labels=territory_labels(snapshot)
    identity=st.selectbox('Объект',choices,format_func=lambda i:labels.get(i,index[i]['name']))
    if identity: card(service,identity,snapshot)


def population_page(service):
    st.header('Население')
    snapshot=service.db.snapshot(); identities=[e for e in snapshot['entities'] if e['kind'] in ('State','Province','City')]
    index={e['id']:e for e in identities}
    labels=territory_labels(snapshot)
    identity=st.selectbox('Территория',list(index),format_func=labels.get)
    card(service,identity,snapshot)
    st.caption('В провинции: сельские группы + городские группы. Население города повторно не прибавляется.')
    if not service.db.campaign():
        st.info('В этом сохранении нет модели страт. Исходные группы доступны в администрировании.')
        return
    st.caption('В государстве учитываются собственные провинции и города, включая города вне своих провинций.')
    with st.expander('Распоряжение на следующий месяц'):
        groups = {e['id']: e for e in snapshot['entities'] if e['kind']=='SocialGroup'}
        with st.form('social-order'):
            group = st.selectbox('Социальная группа', list(groups), format_func=lambda i: groups[i]['name'], key='order-group')
            parameter = st.selectbox('Показатель', ['population','loyalty','welfare','influence'], key='order-parameter')
            value = st.number_input('Новое значение', min_value=0, value=50, step=1, key='order-value')
            submitted = st.form_submit_button('Запланировать')
        if submitted:
            try:
                service.schedule_social(group, parameter, value, source='player')
                st.success('Распоряжение будет выполнено при завершении месяца.')
            except (ValueError, RuntimeError) as error:
                st.error(str(error))
        pending = service.db.pending_commands()
        if pending:
            st.dataframe([{'Группа': groups[c['payload']['entity_id']]['name'],
                           'Показатель': c['payload']['parameter'], 'Значение': c['payload']['value']}
                          for c in pending], hide_index=True)


def diagnostics(service):
    st.header('Диагностика и отчёты месяцев'); st.json(service.db.campaign())
    if not service.db.campaign():
        st.info('Сохранение прежней версии: карта и отчёты месяцев отсутствуют.')
        if st.button('Проверить связи'):
            issues = service.db.check_integrity()
            st.error('; '.join(issues)) if issues else st.success('Связи согласованы')
        return
    if st.button('Полная проверка мира'):
        try:
            result=validate_world(service.db.snapshot(),service.tiles(),service.db.campaign(),require_connected=False)
            st.success('География и население согласованы'); st.json(result)
        except ValueError as error: st.error(str(error))
    for report in service.db.turn_reports():
        with st.expander(f'{report["completed_month"]}/{report["completed_year"]} · {report["summary"]}'):
            st.dataframe(report['phases']); st.write(report['changes'] or 'Без изменений'); st.json(report['snapshot'])
