import copy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core.db import WorldDB
from core.events import EventBus
from core.world_service import WorldService
from modules.economy import Economy
from ui.game import territory_labels


def small_market(service):
    engine=Economy.load(service.db)
    engine.bootstrap()
    engine.accounts={}
    loc=dict(state_id='noweria',province_id='province-0',city_id=None,tile_id='tile-1-1')
    # Для диагностических сценариев явно задаётся небольшой дорожный граф.
    import networkx as nx
    engine.graph=nx.Graph()
    engine.graph.add_edge('near','market',weight=1)
    engine.graph.add_edge('far','market',weight=100)
    engine.graph.add_node('isolated')
    for identity,tile,gold in [('near-seller','near',0),('far-seller','far',0),
                               ('buyer','market',100),('carrier:noweria','market',0)]:
        engine.account(identity,'merchant',gold,**dict(loc,tile_id=tile))
    engine.reset_report()
    return engine


def test_market_selects_landed_price_and_conserves_money(service):
    engine=small_market(service)
    engine.add_stock('near-seller','ore',10,20)
    engine.add_stock('far-seller','ore',10,10)
    sells={'ore':[engine.order('far-seller','ore',10,'sell',1,'Продажа'),
                  engine.order('near-seller','ore',10,'sell',2,'Продажа')]}
    buy=engine.order('buyer','ore',4,'buy',10,'Сырьё')
    before=engine.gold()
    engine.match(buy,sells)
    assert engine.report['trades'][0]['seller']=='near-seller'
    assert engine.accounts['buyer']['stock']['ore']==4
    assert engine.accounts['near-seller']['stock']['ore']==6
    assert engine.accounts['carrier:noweria']['balance']==pytest.approx(4*2*engine.cfg['transport_rate'])
    assert engine.gold()==pytest.approx(before)
    assert buy['status']=='filled'
    engine.match(engine.order('buyer','ore',10000,'buy',10,'Сырьё'),sells)
    assert all(a['balance']>=0 for a in engine.accounts.values())
    assert all(q>=0 for a in engine.accounts.values() for q in a['stock'].values())
    assert sum(t['quantity'] for t in engine.report['trades'])<=20


@pytest.mark.parametrize('scenario', ['resources_near','remote_no_resources','crossroads','oversupply','expensive_input','closed_supplier'])
def test_diagnostic_trade_scenarios(service,scenario):
    engine=small_market(service)
    seller='near-seller'; price=1; q=10; demand=4
    if scenario=='remote_no_resources':
        seller='far-seller'
    elif scenario=='crossroads':
        engine.graph.add_edge('far','market',weight=.5)
        seller='far-seller'
    elif scenario=='oversupply':
        q=100
    elif scenario=='expensive_input':
        seller='far-seller'; price=5
    elif scenario=='closed_supplier':
        q=0
    engine.add_stock(seller,'wood',q,q*price)
    order=engine.order(seller,'wood',q,'sell',price,'Продажа')
    sells={'wood':[order]} if order else {}
    buy=engine.order('buyer','wood',demand,'buy',10,'Сырьё')
    engine.match(buy,sells)
    actual=sum(t['quantity'] for t in engine.report['trades'])
    distance,_=engine.path(engine.accounts[seller]['tile_id'],'market')
    landed=price+distance*engine.goods['wood']['weight']*engine.cfg['transport_rate']
    assert actual==pytest.approx(min(demand,q,100/landed))
    assert engine.accounts['buyer']['balance']==pytest.approx(100-actual*landed)
    assert engine.total_stock()['wood']==pytest.approx(q)
    if q==0:
        assert buy['reason']=='no_physical_stock'


def test_unreachable_route_and_delivery_limit(service):
    engine=small_market(service)
    engine.accounts['near-seller']['tile_id']='isolated'
    engine.add_stock('near-seller','wood',10,10)
    sell=engine.order('near-seller','wood',10,'sell',1,'Продажа')
    buy=engine.order('buyer','wood',1,'buy',10,'Покупка')
    engine.match(buy,{'wood':[sell]})
    assert buy['reason']=='no_route'
    engine.accounts['near-seller']['tile_id']='far'
    sell['tile_id']='far'
    buy=engine.order('buyer','wood',1,'buy',1,'Покупка')
    engine.match(buy,{'wood':[sell]})
    assert buy['reason']=='landed_price_above_limit'


def test_economy_persistence_idempotency_and_rollback(service):
    assert service.initialize_economy()
    before=copy.deepcopy(service.db.economy_records('EconomyMeta'))
    assert not service.initialize_economy()
    assert service.db.economy_records('EconomyMeta')==before
    report=service.advance_turn(command_id='economy-once')
    records=service.db.economy_records('EconomyMonth')
    accounts=service.db.economy_records('EconomyAccount')
    assert service.advance_turn(command_id='economy-once')==report
    assert service.db.economy_records('EconomyMonth')==records
    assert service.db.economy_records('EconomyAccount')==accounts
    def fail(ctx):
        raise ValueError('rollback after economy')
    service.turns.register_phase('failed',fail)
    with pytest.raises(ValueError,match='rollback after economy'):
        service.advance_turn(command_id='rollback-economy')
    assert service.db.economy_records('EconomyAccount')==accounts
    assert service.db.economy_records('EconomyMonth')==records
    assert service.db.turn_result('rollback-economy') is None


def test_monthly_invariants_for_all_states(service):
    service.initialize_economy()
    initial=Economy.load(service.db)
    initial_stock=initial.total_stock()
    for i in range(12):
        service.advance_turn(command_id=f'checked-{i}')
    engine=Economy.load(service.db)
    records=[r for r in service.db.economy_records('EconomyMonth') if r['month']>0]
    assert all(all(r['invariants'].values()) for r in records)
    assert engine.gold()==pytest.approx(initial.gold(),abs=1e-6)
    assert {e['state_id'] for e in engine.enterprises.values()}=={e['id'] for e in engine.entities.values() if e['kind']=='State'}
    assert {engine.accounts[e['owner_id']]['kind'] for e in engine.enterprises.values()}=={'private','merchant','municipal','treasury'}
    assert any(t['buyer_state']!=t['seller_state'] for r in records for t in r['trades'])
    assert any(f['reason']=='Заработная плата' and engine.accounts[f['recipient']]['kind']=='household' for r in records for f in r['flows'])
    after=engine.total_stock()
    for g in engine.goods:
        assert after[g]==pytest.approx(initial_stock[g]+sum(r['production'].get(g,0)-r['inputs_used'].get(g,0)-r['consumed'].get(g,0)-r['construction_used'].get(g,0) for r in records),abs=1e-6)
    with WorldDB(service.db.path,read_only=True) as reopened:
        assert reopened.economy_records('Enterprise')==service.db.economy_records('Enterprise')


def test_production_limits_and_investment_ownership(service):
    engine=Economy.load(service.db); engine.bootstrap()
    initial=engine.gold()
    firm=next(e for e in engine.enterprises.values() if e['good']=='bread')
    engine.accounts[firm['id']]['stock']['flour']=0
    report=engine.step()
    assert firm['output'] <= sum(
        t['quantity']/.8 for t in report['trades'] if t['buyer']==firm['id'] and t['good']=='flour')
    assert firm['workers'] == pytest.approx(firm['output']*engine.goods['bread']['labor'])
    assert engine.gold()==pytest.approx(initial)
    for e in engine.enterprises.values():
        assert e['output']<=e['capacity']+1e-7 or any(a['enterprise']==e['id'] and a['action']=='contracted' for a in report['lifecycle'])
    other=next(a for a in engine.accounts.values() if a['kind']=='merchant' and a['state_id']!=firm['state_id'])
    with pytest.raises(ValueError,match='Иностранное'):
        engine.open_enterprise('bread',engine.location(firm['province_id']),other['id'],5)


def test_safe_stress_copy(service,tmp_path):
    service.initialize_economy()
    service.schedule_social('stratum-province-0-0','loyalty',62,command_id='pending-copy')
    before=service.db.economy_records('EconomyAccount')
    history=service.db.counts()['Event']
    destination=tmp_path/'stress-copy.lbug'
    reports=service.simulate_economy(12,destination)
    assert len(reports)==13
    assert service.db.meta()['turn']==1
    assert service.db.economy_records('EconomyAccount')==before
    assert service.db.counts()['Event']==history
    assert service.db.pending_commands()
    with WorldDB(destination) as db:
        assert db.meta()['turn']==13
        assert not db.pending_commands()
        assert db.get_entity('stratum-province-0-0')['properties']['loyalty']==62
    with pytest.raises(ValueError,match='новый путь'):
        service.simulate_economy(12,destination)


def test_territory_labels(service):
    labels=territory_labels(service.db.snapshot())
    assert 'город' in labels['city-0-0']
    assert 'Новерская Республика' in labels['city-0-0']
    assert 'провинция Лунария' in labels['city-0-0']
    assert 'провинция' in labels['province-0']


def test_economy_ui_sections(tmp_path,monkeypatch):
    monkeypatch.setenv('NOWERIA_DB',str(tmp_path/'economy-ui.lbug'))
    from ui.app import application
    application.clear()
    app=AppTest.from_file(Path(__file__).resolve().parents[1]/'main.py',default_timeout=60).run()
    try:
        app.sidebar.radio[0].set_value('Экономика и торговля').run()
        assert not app.exception
        next(b for b in app.button if b.label=='Инициализировать экономику').click().run()
        assert not app.exception
        application().advance_turn(command_id='ui-economy')
        app.run()
        for section in ['Торговля','Предприятия','Провинции','Диагностика','Обзор']:
            next(s for s in app.selectbox if s.label=='Раздел').set_value(section).run()
            assert not app.exception,section
        next(s for s in app.selectbox if s.label=='Государство').set_value('noweria').run()
        next(s for s in app.selectbox if s.label=='Товар').set_value('bread').run()
        assert not app.exception
        next(b for b in app.button if b.label=='Запустить тестовую симуляцию').click().run(timeout=60)
        assert not app.exception
        next(r for r in app.radio if r.label=='Источник анализа').set_value('Последний тестовый прогон').run()
        assert not app.exception
        assert application().db.meta()['turn']==2
    finally:
        application().db.close()
        application.clear()
