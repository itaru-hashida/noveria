from pathlib import Path
from streamlit.testing.v1 import AppTest


def test_pages_and_explicit_action(tmp_path, monkeypatch):
    monkeypatch.setenv('NOWERIA_DB', str(tmp_path / 'ui.lbug'))
    from ui.app import application
    application.clear()

    app = AppTest.from_file(Path(__file__).resolve().parents[1] / 'main.py', default_timeout=30).run()
    assert not app.exception
    for page in ['Государства','Провинции и города','Население','История событий','Граф связей','Диагностика','Администрирование']:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, page
    app.sidebar.radio[0].set_value('Администрирование').run()
    for action in ['Лояльность','Правитель','Передача территории','Дипломатия','Создать сущность','Создать связь','Демонстрация']:
        app.selectbox[0].set_value(action).run()
        assert not app.exception, action
    app.selectbox[0].set_value('Лояльность').run()
    app.number_input[0].set_value(55.0).run()
    app.checkbox[0].check().run()
    app.button[0].click().run()
    assert not app.exception
    assert application().db.history(limit=1)[0]['payload']['new'] == 55
    app.selectbox[0].set_value('Следующий ход').run()
    app.checkbox[0].check().run()
    app.button[0].click().run()
    assert not app.exception
    assert application().db.meta()['turn'] == 2
    count = application().db.counts()['Event']
    app.run()
    assert application().db.counts()['Event'] == count
    application().db.close()
    application.clear()


def test_map_layers_and_scheduled_month(tmp_path, monkeypatch):
    monkeypatch.setenv('NOWERIA_DB', str(tmp_path / 'map-ui.lbug'))
    from ui.app import application
    application.clear()
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / 'main.py', default_timeout=30).run()
    try:
        assert not app.exception
        for layer in ['Провинции','Рельеф','Ресурсы','Дороги','Население']:
            next(r for r in app.radio if r.label=='Слой').set_value(layer).run()
            assert not app.exception, layer
        app.sidebar.radio[0].set_value('Население').run()
        app.selectbox(key='order-group').set_value('stratum-province-0-0')
        app.selectbox(key='order-parameter').set_value('loyalty')
        app.number_input(key='order-value').set_value(61)
        next(b for b in app.button if b.label=='Запланировать').click().run()
        assert not app.exception
        assert len(application().db.pending_commands()) == 1
        assert application().db.pending_commands()[0]['payload'] == dict(
            entity_id='stratum-province-0-0', parameter='loyalty', value=61)
        app.sidebar.radio[0].set_value('Игра / Карта').run()
        next(b for b in app.button if b.label=='Завершить месяц').click().run()
        assert not app.exception
        assert application().db.meta()['turn'] == 2
        assert application().db.get_entity('stratum-province-0-0')['properties']['loyalty'] == 61
        assert not application().db.pending_commands()
        event_count = application().db.counts()['Event']
        app.run()
        assert application().db.counts()['Event'] == event_count
    finally:
        application().db.close()
        application.clear()


def test_legacy_save_navigation(tmp_path, monkeypatch):
    from core.db import WorldDB
    path = tmp_path / 'legacy.lbug'
    with WorldDB(path) as db:
        with db.transaction():
            db.create_entity('state','State','Старое государство',{})
            db.create_entity('province','Province','Старая провинция',{})
            db.create_entity('city','City','Старый город',{})
            db.put_link('state','province','owns')
            db.put_link('state','province','includes')
            db.put_link('state','city','owns')
            db.put_link('province','city','includes')
            db.mark_seeded()
    monkeypatch.setenv('NOWERIA_DB', str(path))
    from ui.app import application
    application.clear()
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / 'main.py', default_timeout=30).run()
    try:
        assert not app.exception
        for page in ['Государства','Провинции и города','Население','Диагностика','История событий','Граф связей']:
            app.sidebar.radio[0].set_value(page).run()
            assert not app.exception, page
        assert application().db.campaign() is None
        assert application().db.counts()['City'] == 1
        assert application().db.counts()['Event'] == 0
    finally:
        application().db.close()
        application.clear()
