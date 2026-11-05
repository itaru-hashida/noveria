import copy
from types import SimpleNamespace

import pytest

from modules.economy import Economy
from test_supply_response import market
from test_economy_v3 import engine
from core.db import WorldDB
from scripts import research_supply_response, explain_supply_case


def test_game_replenishes_stock_sold_in_current_month(engine):
    f,sells,_=market(engine,'control')
    engine.__class__=Economy
    assert engine.production_outlook(f,sells)['planned_output']==4


def regulated_market(engine, stock=0):
    f, sells, _ = market(engine, 'lead_time', stock=stock)
    engine.__class__ = Economy
    del engine.probe
    return f, sells


def test_itaru_codex_regulator_cuts_output_gradually(engine):
    f, sells = regulated_market(engine)
    for order in engine.report['orders']:
        if order['side'] == 'buy':
            engine.cancel_order(order)
    f.update(planned_output=4, sales_forecast=0,
             sales_history=[dict(sold=0, output=4)]*3)
    assert engine.production_outlook(f, sells)['planned_output'] == pytest.approx(3)


def test_itaru_codex_regulator_allows_immediate_growth(engine):
    f, sells = regulated_market(engine, stock=0)
    f['planned_output'] = 1
    assert engine.production_outlook(f, sells)['planned_output'] == pytest.approx(4)


def test_itaru_codex_regulator_cuts_immediately_when_stock_is_excessive(engine):
    f, sells = regulated_market(engine, stock=12)
    f['planned_output'] = 4
    assert engine.production_outlook(f, sells)['planned_output'] == 0


def test_game_matches_independent_previous_experiment(engine):
    expected=copy.deepcopy(engine)
    for month in range(1,13):
        engine.step();expected.step()
        assert engine.accounts==expected.accounts
        assert engine.enterprises==expected.enterprises
        a,b=copy.deepcopy(engine.meta),copy.deepcopy(expected.meta)
        a.pop('stock_planning_revision');b.pop('stock_planning_revision')
        assert a==b
        assert engine.rng.getstate()==expected.rng.getstate()


def test_existing_save_gets_new_revision_and_roundtrips(engine, tmp_path):
    engine.meta.pop('stock_planning_revision',None)
    engine.step()
    assert engine.meta['stock_planning_revision']==2
    with WorldDB(tmp_path/'stock.lbug') as db:
        for entity in engine.entities.values():
            db.create_entity(entity['id'],entity['kind'],entity['name'],entity['properties'])
        for target,owner in engine.owners.items():
            db.put_link(owner,target,'owns')
        db.store_tiles(list(engine.tiles.values()))
        engine.save(db)
        restored=Economy.load(db)
    assert restored.meta['stock_planning_revision']==2
    restored.step();engine.step()
    assert restored.accounts==engine.accounts
    assert restored.enterprises==engine.enterprises


def test_historical_cli_cannot_silently_reapply_promoted_policy():
    with pytest.raises(ValueError,match='49eb0d0'):
        research_supply_response.run(SimpleNamespace())
    with pytest.raises(ValueError,match='49eb0d0'):
        explain_supply_case.explain('/unused/historical-checkpoint')
