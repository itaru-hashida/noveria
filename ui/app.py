import json
import streamlit as st
from core.context_builder import ContextBuilder
from core.db import ENTITY_TYPES, RELATIONS
from core.history_service import HistoryService
from main import create_application
from modules import demo, diplomacy, social, world
from ui.graph import graph_figure


@st.cache_resource
def application():
    return create_application()


def choose(db, label, kind=None, key=None, optional=False):
    query = st.text_input('Поиск: '+label, key=f'{key}-search')
    rows = db.list_entities(kind=kind, search=query, limit=100)
    names = {r['id']: f'{r["name"]} · {r["kind"]}' for r in rows}
    return st.selectbox(label, ([None] if optional else [])+list(names),
                        format_func=lambda i: names.get(i, 'Все / не выбрано'), key=key)


def event_list(events):
    if not events:
        st.info('Событий по этим фильтрам пока нет.')
    for event in events:
        with st.expander(f'Ход {event["turn"]}.{event["sequence"]} · {event["summary"]} · {event["source"]}'):
            st.caption(f'{event["type"]} | {event["created_at"]} | значимость {event["importance"]}')
            payload=event['payload']
            if 'old' in payload or 'new' in payload:
                st.write(f'{payload.get("parameter","Изменение")}: {payload.get("old","—")} → {payload.get("new","—")}')
            st.write('Участники: '+', '.join(p['role']+': '+p['id'] for p in event['entities']))
            with st.expander('Технические подробности'): st.json(event)


def entity_card(db, identity):
    if not identity:
        return
    entity = db.get_entity(identity)
    st.subheader(entity['name'])
    st.caption(f'{entity["kind"]} · {identity}')
    st.json(entity['properties'])
    for relation in db.links(identity, limit=100):
        st.write(f'{db.get_entity(relation["source"])["name"]} → {relation["kind"]} → {db.get_entity(relation["target"])["name"]}')
    event_list(db.history(entity_id=identity, limit=10))


def overview(db):
    st.header('Обзор мира')
    st.write(f'{db.meta()["name"]} · Ход {db.meta()["turn"]}')
    counts = db.counts()
    for col, kind, label in zip(st.columns(4), ['State','Province','City','Character'], ['Государства','Провинции','Города','Персонажи']):
        col.metric(label, counts.get(kind, 0))
    entity_card(db, choose(db, 'Государство', 'State', 'overview-state'))
    st.subheader('Последние события')
    event_list(db.history(limit=5))


def browser(db):
    st.header('Мир')
    state = choose(db, 'Государство', 'State', 'browser-state', True)
    if state:
        provinces = [r['target'] for r in db.links(state, 'includes') if r['source']==state]
        province = st.selectbox('Провинция', [None]+provinces, format_func=lambda i: db.get_entity(i)['name'] if i else 'Выберите провинцию')
        if province:
            cities = [r['target'] for r in db.links(province, 'includes') if r['source']==province]
            city = st.selectbox('Город', [None]+cities, format_func=lambda i: db.get_entity(i)['name'] if i else 'Выберите город')
            entity_card(db, city or province)
        else:
            entity_card(db, state)
    st.divider()
    kind = st.selectbox('Тип сущности', ENTITY_TYPES)
    query = st.text_input('Поиск по названию')
    page = st.number_input('Страница', min_value=1, step=1)
    rows = db.list_entities(kind, query, limit=25, offset=(page-1)*25)
    names = {r['id']:r['name'] for r in rows}
    entity_card(db, st.selectbox('Сущность', list(names), format_func=names.get))


def graph_page(db):
    st.header('Граф мира')
    if st.button('Сбросить фильтры'):
        for key in ('graph-types','graph-relations','graph-focus','graph-focus-search','graph-depth','graph-limit'):
            st.session_state.pop(key, None)
        st.rerun()
    types = st.multiselect('Типы узлов', ENTITY_TYPES, default=list(ENTITY_TYPES), key='graph-types')
    relations = st.multiselect('Типы связей', list(RELATIONS), default=list(RELATIONS), key='graph-relations')
    focus = choose(db, 'Центр графа', key='graph-focus', optional=True)
    depth = st.slider('Глубина обхода', 0, 5, 2, key='graph-depth')
    limit = st.slider('Максимум узлов', 10, 300, 100, key='graph-limit')
    data = db.graph(types, relations, focus, depth, limit)
    st.caption(f'Показано {len(data["nodes"])} узлов, {len(data["edges"])} связей. Лимит {limit}; глубина {depth}. Стрелки — направление; колесо мыши — масштаб. Активные типы: {", ".join(types)}')
    st.plotly_chart(graph_figure(data), width='stretch', config={'scrollZoom':True})
    entity_card(db, focus)


def history_page(db):
    st.header('История')
    identity = choose(db, 'Затронутая сущность', key='history-entity', optional=True)
    event_type = st.text_input('Тип события (пусто — все)') or None
    source = st.text_input('Источник (пусто — все)') or None
    turn = st.number_input('Ход (0 — все)', min_value=0, step=1)
    importance = st.slider('Минимальная значимость', 0, 10, 0)
    from_turn=st.number_input('С хода',min_value=1,step=1)
    to_turn=st.number_input('По ход (0 — без ограничения)',min_value=0,step=1)
    action=st.text_input('ID действия / команды (необязательно)') or None
    page = st.number_input('Страница истории', min_value=1, step=1)
    event_list(db.history(entity_id=identity, event_type=event_type, source=source,
                          turn=turn or None, importance=importance, limit=20, offset=(page-1)*20,
                          turn_from=from_turn,turn_to=to_turn or None,action_id=action))
    if identity:
        parameter = st.text_input('История параметра', 'loyalty')
        events = HistoryService(db).parameter_history(identity, parameter)
        st.dataframe([dict(turn=e['turn'],sequence=e['sequence'],old=e['payload']['old'],new=e['payload']['new']) for e in reversed(events)])
        st.caption('Только сохранённые изменения, без вымышленных промежуточных значений.')


def actions(service, admin=False):
    db = service.db
    source = 'admin' if admin else 'player'
    st.header('Администрирование' if admin else 'Песочница')
    action = st.selectbox('Действие', ['Изменить параметр','Лояльность','Правитель','Передача территории','Дипломатия','Создать сущность','Создать связь','Следующий ход','Демонстрация'])
    if action == 'Изменить параметр':
        identity = choose(db, 'Сущность', key='parameter-entity')
        parameter = st.text_input('Параметр', 'loyalty')
        value = st.text_area('Новое значение JSON', '55')
        operation = lambda: (service.change_social(identity,parameter,json.loads(value),source=source)
                             if db.get_entity(identity)['kind']=='SocialGroup' and parameter in ('population','loyalty','welfare','influence')
                             else service.change_parameter(identity, parameter, json.loads(value), source=source))
    elif action == 'Лояльность':
        identity = choose(db, 'Социальная группа', 'SocialGroup', 'loyalty-group')
        value = st.number_input('Новая лояльность', min_value=0.0, value=55.0)
        operation = lambda: social.change_loyalty(service, identity, value, source=source)
    elif action == 'Правитель':
        state = choose(db, 'Государство', 'State', 'ruler-state')
        character = choose(db, 'Персонаж', 'Character', 'ruler-character')
        operation = lambda: world.appoint_ruler(service, state, character, source=source)
    elif action == 'Передача территории':
        kind = st.selectbox('Тип территории', ['City','Province'])
        territory = choose(db, 'Территория', kind, 'transfer-territory')
        state = choose(db, 'Новый владелец', 'State', 'transfer-state')
        st.caption('Передача провинции сохраняет индивидуальное владение её городами.')
        operation = lambda: world.transfer_territory(service, territory, state, source=source)
    elif action == 'Дипломатия':
        first = choose(db, 'Первое государство', 'State', 'diplomacy-first')
        second = choose(db, 'Второе государство', 'State', 'diplomacy-second')
        value = st.slider('Отношение', -100, 100, 0)
        operation = lambda: diplomacy.set_relation(service, first, second, value, source=source)
    elif action == 'Создать сущность':
        kind = st.selectbox('Тип', ENTITY_TYPES)
        name = st.text_input('Название')
        properties = st.text_area('Свойства JSON', '{}')
        if kind=='City': st.info('Укажите province_id и tile_id свободной клетки этой провинции. Три страты будут созданы автоматически.')
        if kind=='Province': st.info('Выделение провинции: state_id, tile_id свободной клетки и необязательное city_name. Создаются столица и страты; клетка не должна разрывать старую провинцию.')
        operation = lambda: service.create_entity(kind, name, json.loads(properties), source=source)
    elif action == 'Создать связь':
        first = choose(db, 'Откуда', key='link-first')
        second = choose(db, 'Куда', key='link-second')
        kind = st.selectbox('Вид связи', list(RELATIONS))
        st.caption('Для владельца, правителя и дипломатии используйте специальные действия.')
        operation = lambda: service.create_link(first, second, kind, source=source)
    elif action == 'Следующий ход':
        operation = lambda: service.advance_turn(source=source)
    else:
        st.info('Демо: лояльность выбранной группы последовательно станет 70 → 55 → 40. Три записи истории с общим action_id.')
        group = choose(db, 'Группа для демо', 'SocialGroup', 'demo-group')
        operation = lambda: demo.run_demo(service, group, source='admin' if admin else 'demo')
    confirmed = st.checkbox('Подтверждаю изменение сохранения', key=f'confirm-{action}-{source}')
    if st.button('Выполнить', disabled=not confirmed, type='primary'):
        try:
            result = operation()
            st.success('Изменения сохранены')
            st.json(result)
        except (ValueError, RuntimeError, TypeError) as error:
            st.error(str(error))
    if admin:
        st.subheader('Состояние БД')
        st.json(dict(path=db.path,meta=db.meta(),counts=db.counts(),relations=db.relation_counts()))
        if st.button('Проверить целостность'):
            issues = db.check_integrity()
            if issues:
                st.error('; '.join(issues))
            else:
                st.success('Нарушений целостности не найдено')
        identity = choose(db, 'Контекст LLM', key='context-entity', optional=True)
        if st.button('Собрать контекст'):
            st.json(ContextBuilder(db).build(identity))


def run():
    st.set_page_config(page_title='Новерия', page_icon='🏛', layout='wide')
    st.title('Новерия · Политическая RPG')
    try:
        service = application()
    except Exception as error:
        st.error(f'Ошибка инициализации: {error}')
        st.stop()
    from ui.game import play,directory,population_page,diagnostics
    from ui.economy import economy_page
    page = st.sidebar.radio('Навигация', ['Игра / Карта','Государства','Провинции и города','Население','Экономика и торговля','История событий','Граф связей','Администрирование','Диагностика'])
    st.sidebar.caption(f'Ход {service.db.meta()["turn"]} · Сохранение в LadybugDB')
    if page=='Игра / Карта': play(service)
    elif page=='Государства': directory(service,('State',))
    elif page=='Провинции и города': directory(service,('Province','City'))
    elif page=='Население': population_page(service)
    elif page=='Экономика и торговля': economy_page(service)
    elif page=='История событий': history_page(service.db)
    elif page=='Граф связей': graph_page(service.db)
    elif page=='Диагностика': diagnostics(service)
    else:
        view=st.radio('Раздел',['Действия','Просмотр сущностей'],horizontal=True)
        if view=='Действия': actions(service,admin=True)
        else:
            browser(service.db)
