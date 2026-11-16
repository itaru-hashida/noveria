from copy import deepcopy

from modules.economy.native.stock_store_boundary import StockStore


def accounts():
    return {'a': {'id': 'a', 'stock': {'wood': 3}, 'stock_reserved': {'wood': 1}}}


def test_stock_store_uses_live_dicts():
    data = accounts()
    store = StockStore(data)
    assert store.get_stock('a', 'wood') == 3
    store.set_stock('a', 'wood', 5)
    assert data['a']['stock']['wood'] == 5
    store.set_reserved('a', 'wood', 2)
    assert data['a']['stock_reserved']['wood'] == 2


def test_stock_store_preserves_object_arithmetic_types():
    data = accounts()
    store = StockStore(data)
    store.add_stock('a', 'wood', 2)
    assert type(data['a']['stock']['wood']) is int
    store.add_reserved('a', 'stone', 0.5)
    assert type(data['a']['stock_reserved']['stone']) is float


def test_stock_store_release_matches_previous_max_semantics():
    data = accounts()
    store = StockStore(data)
    store.release_reserved('a', 'wood', 5)
    assert data['a']['stock_reserved']['wood'] == 0
    assert type(data['a']['stock_reserved']['wood']) is int


def test_stock_store_sync_adopts_replaced_dicts():
    data = accounts()
    store = StockStore(data)
    data['a']['stock'] = {'wood': 9}
    data['a']['stock_reserved'] = {'wood': 4}
    assert store.get_stock('a', 'wood') == 9
    assert store.get_reserved('a', 'wood') == 4


def test_stock_store_deepcopy_does_not_share_accounts():
    data = accounts()
    clone = deepcopy(data)
    first = StockStore(data)
    second = StockStore(clone)
    second.add_stock('a', 'wood', 1)
    assert data['a']['stock']['wood'] == 3
    assert clone['a']['stock']['wood'] == 4
