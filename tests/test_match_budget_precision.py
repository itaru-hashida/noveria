import pytest

from test_economy_v3 import engine, firm, clear


@pytest.mark.parametrize('delivery',[False,True])
@pytest.mark.parametrize('purpose',['Явная заявка','Базовые нужды'])
def test_price_tolerance_releases_actual_backed_cost_without_other_reserves(engine,delivery,purpose):
    clear(engine)
    seller=firm(engine,'wood')['id']
    engine.accounts[seller].update(stock={},values={})
    engine.add_stock(seller,'wood',1000,100)
    buyer=engine.accounts['h']
    buyer.update(balance=1020,protected=10)
    other=engine.order('h','fish',2,'buy',5,'Другая заявка')
    buy=engine.order('h','wood',1000,'buy',1,purpose)
    freight=0
    if delivery:
        engine.graph.add_edge('remote','t',weight=1)
        engine.accounts[seller]['tile_id']='remote'
        engine.paths={}
        freight=engine.cfg['transport_rate']*engine.goods['wood']['weight']
    sell=engine.order(seller,'wood',1000,'sell',1+5e-8-freight,'Продажа')
    gold=engine.gold()
    engine.match(buy,{'wood':[sell]})
    assert buyer['balance']==pytest.approx(20)
    assert other['reserved']==10 and buyer['protected']==10
    assert buyer['reserved']==pytest.approx(10)
    assert buy['reserved']==pytest.approx(0)
    assert buyer['stock']['wood']==pytest.approx(1000/(1+5e-8))
    assert engine.gold()==pytest.approx(gold)
