import json

from scripts.godot_server import _map_network


def test_map_network_projects_real_city_placements_and_physical_roads():
    tiles = [
        {"id": "a", "x": 0, "y": 0, "road": True, "passable": True},
        {"id": "b", "x": 1, "y": 0, "road": True, "passable": True},
        {"id": "c", "x": 2, "y": 0, "road": True, "passable": True},
        {"id": "d", "x": 1, "y": 1, "road": False, "passable": True},
    ]
    entities = [
        {"id": "city-b", "kind": "City", "name": "Beta", "properties": {"tile_id": "c"}},
        {"id": "city-a", "kind": "City", "name": "Alpha", "properties": {"tile_id": "a"}},
    ]

    cities, routes = _map_network(tiles, entities)

    assert cities == [{"x": 0, "y": 0, "name": "Alpha"}, {"x": 2, "y": 0, "name": "Beta"}]
    assert routes == [{"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 2, "y": 0}]}]
    assert all(set(route) == {"points"} for route in routes)


def test_map_network_does_not_turn_non_road_tiles_into_trade_routes():
    tiles = [
        {"id": "a", "x": 0, "y": 0, "road": False, "passable": True},
        {"id": "b", "x": 1, "y": 0, "road": False, "passable": True},
    ]
    entities = [
        {"id": "city-a", "kind": "City", "name": "Alpha", "properties": {"tile_id": "a"}},
        {"id": "city-b", "kind": "City", "name": "Beta", "properties": {"tile_id": "b"}},
    ]

    cities, routes = _map_network(tiles, entities)

    assert len(cities) == 2
    assert routes == []


def test_map_network_does_not_bridge_disconnected_road_components():
    tiles = [
        {"id": "a", "x": 0, "y": 0, "road": True, "passable": True},
        {"id": "b", "x": 1, "y": 0, "road": True, "passable": True},
        {"id": "c", "x": 3, "y": 0, "road": True, "passable": True},
    ]
    entities = [
        {"id": "city-a", "kind": "City", "name": "Alpha", "properties": {"tile_id": "a"}},
        {"id": "city-b", "kind": "City", "name": "Beta", "properties": {"tile_id": "c"}},
    ]

    _, routes = _map_network(tiles, entities)

    assert routes == []


def test_real_snapshot_payload_has_extended_map_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("NOWERIA_DB", str(tmp_path / "snapshot.lbug"))
    from scripts.godot_server import build_map

    payload = build_map()

    assert payload["version"] == 2
    assert payload["cities"]
    assert all(set(city) == {"x", "y", "name"} for city in payload["cities"])
    assert payload["routes"]
    assert all(set(route) == {"points"} for route in payload["routes"])
    assert all(route["points"] for route in payload["routes"])
    assert all("elevation" in tile for tile in payload["tiles"])
    assert max(tile["elevation"] for tile in payload["tiles"]) > 0.0
    assert {tile["relief"] for tile in payload["tiles"]} >= {"hills", "mountains"}
    json.dumps(payload, ensure_ascii=False, allow_nan=False)
