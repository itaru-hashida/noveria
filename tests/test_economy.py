import copy
from pathlib import Path
import pytest

from core.db import WorldDB
from core.economy_state import EconomyState
from core.events import EventBus
from core.labels import territory_labels
from core.world_service import WorldService
from modules.economy import Economy


def small_market(service):
    engine=service.economy_state().engine
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
    state = service.economy_state()
    before = copy.deepcopy(state.engine.meta)
    assert not service.initialize_economy()
    assert state.engine.meta == before
    report=service.advance_turn(command_id='economy-once')
    assert state.last_report['month'] == 1
    accounts=copy.deepcopy(state.engine.accounts)
    assert service.advance_turn(command_id='economy-once')==report
    # Повторная команда не двигает память: месяц и счета те же.
    assert state.engine.meta['month'] == 1
    assert state.engine.accounts == accounts
    def fail(ctx):
        raise ValueError('rollback after economy')
    service.turns.register_phase('failed',fail)
    with pytest.raises(ValueError,match='rollback after economy'):
        service.advance_turn(command_id='rollback-economy')
    # Откат хода возвращает экономику: счета и месяц прежние, отчёта нет.
    assert state.engine.accounts == accounts
    assert state.engine.meta['month'] == 1
    assert service.db.turn_result('rollback-economy') is None


def test_monthly_invariants_for_all_states(service):
    service.initialize_economy()
    state = service.economy_state()
    initial_stock=state.engine.total_stock()
    initial_gold=state.engine.gold()
    records=[]
    for i in range(12):
        service.advance_turn(command_id=f'checked-{i}')
        records.append(state.last_report)
    engine=state.engine
    assert all(all(r['invariants'].values()) for r in records)
    assert engine.gold()==pytest.approx(initial_gold,abs=1e-6)
    assert {e['state_id'] for e in engine.enterprises.values()}=={e['id'] for e in engine.entities.values() if e['kind']=='State'}
    assert {engine.accounts[e['owner_id']]['kind'] for e in engine.enterprises.values()}=={'private','merchant','municipal','treasury'}
    assert any(t['buyer_state']!=t['seller_state'] for r in records for t in r['trades'])
    assert any(f['reason']=='Заработная плата' and engine.accounts[f['recipient']]['kind']=='household' for r in records for f in r['flows'])
    after=engine.total_stock()
    for g in engine.goods:
        assert after[g]==pytest.approx(initial_stock[g]+sum(
            r['production'].get(g, 0)-r['inputs_used'].get(g, 0)
            -r['consumed'].get(g, 0)-r['construction_used'].get(g, 0)
            -r['equipment_wear'].get(g, 0) for r in records), abs=1e-6)
    # Товарный и денежный итог переживает checkpoint без допуска.
    checkpoint = Path(service.db.path).parent/'invariants-economy'
    state.save_checkpoint(checkpoint)
    with WorldDB(service.db.path, read_only=True) as reopened:
        loaded = EconomyState.load_checkpoint(
            checkpoint, dict(snapshot=reopened.snapshot(), tiles=reopened.tiles(),
                             campaign=reopened.campaign()))
        assert loaded.engine.total_stock() == after
        assert loaded.engine.gold() == engine.gold()


def test_production_limits_and_investment_ownership(service):
    engine=service.economy_state().engine; engine.bootstrap()
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
    state = service.economy_state()
    before = copy.deepcopy(state.engine.accounts)
    gold = state.engine.gold()
    history=service.db.counts()['Event']
    destination=tmp_path/'stress-copy.lbug'
    reports=service.simulate_economy(12,destination)
    # Отчёт месяца 0 плюс двенадцать отчётов прогона.
    assert len(reports)==13
    assert service.db.meta()['turn']==1
    # Копия работает в памяти: текущая кампания не меняется ни на монету.
    assert state.engine.accounts == before
    assert state.engine.gold() == gold
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
