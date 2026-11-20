"""Минимальный TCP-мост Python -> Godot для просмотра текущей карты.

Протокол: одна JSON-команда на строку, один JSON-ответ на строку.
Команды: ``{"type":"get_map"}`` и ``{"type":"ping"}``.
"""
from __future__ import annotations

import argparse
import json
import os
import socketserver
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))


def _point(tile):
    return {"x": int(tile["x"]), "y": int(tile["y"])}


def _tile_elevation(tile):
    """Return a numeric elevation for both old and geological snapshots.

    The legacy generator predates the continuous terrain layers and therefore
    does not persist elevation.  Zero is an explicit sea-level fallback; a
    geological snapshot keeps its generated value unchanged.
    """
    value = tile.get("elevation", 0.0)
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    return value if value == value else 0.0


def _road_path(tiles, start, end):
    """Find a deterministic path over physical road cells only."""
    by_id = {tile["id"]: tile for tile in tiles}
    road_ids = {tile["id"] for tile in tiles if tile.get("road") and tile.get("passable", True)}
    if start not in road_ids or end not in road_ids:
        return []
    by_coordinate = {(int(tile["x"]), int(tile["y"])): tile["id"] for tile in tiles}
    previous = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == end:
            break
        tile = by_id[current]
        neighbours = []
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            neighbour = by_coordinate.get((int(tile["x"]) + dx, int(tile["y"]) + dy))
            if neighbour in road_ids and neighbour not in previous:
                neighbours.append(neighbour)
        for neighbour in sorted(neighbours):
            previous[neighbour] = current
            queue.append(neighbour)
    if end not in previous:
        return []
    path = []
    current = end
    while current is not None:
        path.append(_point(by_id[current]))
        current = previous[current]
    return list(reversed(path))


def _map_network(tiles, entities):
    """Project the seeded city/road snapshot into the Godot map contract."""
    by_id = {tile["id"]: tile for tile in tiles}
    cities = []
    city_tiles = []
    for entity in sorted((e for e in entities if e.get("kind") == "City"), key=lambda e: e["id"]):
        tile = by_id.get(entity.get("properties", {}).get("tile_id"))
        if tile is None:
            continue
        cities.append({"x": int(tile["x"]), "y": int(tile["y"]), "name": entity["name"]})
        city_tiles.append((entity["id"], tile["id"]))

    # The geography generator builds an MST of the placed cities and marks its
    # cells as physical roads.  Reconstruct a deterministic city network from
    # those road cells; no synthetic straight lines are sent to Godot.
    candidates = []
    for index, (city_id, start) in enumerate(city_tiles):
        for other_id, end in city_tiles[index + 1:]:
            path = _road_path(tiles, start, end)
            if path:
                candidates.append((len(path), city_id, other_id, path))
    connected = set()
    routes = []
    for _length, city_id, other_id, path in sorted(candidates):
        if city_id in connected and other_id in connected:
            continue
        routes.append({"points": path})
        connected.update((city_id, other_id))
    return cities, routes


def build_map():
    from main import create_application

    # The Godot preview is intended to display the geological terrain fields.
    # This only affects a newly created database; seeded saves remain immutable.
    os.environ.setdefault("NOVERIA_GENERATION_MODEL", "geological")
    service = create_application()
    graph = service.world_graph()
    tiles = graph["tiles"]
    width = max((int(tile["x"]) for tile in tiles), default=-1) + 1
    height = max((int(tile["y"]) for tile in tiles), default=-1) + 1
    entities = graph["snapshot"].get("entities", [])
    tiles = [dict(tile, elevation=_tile_elevation(tile)) for tile in tiles]
    cities, routes = _map_network(tiles, entities)
    return {
        "type": "map",
        "version": 2,
        "width": width,
        "height": height,
        "tiles": tiles,
        "entities": entities,
        "links": graph["snapshot"].get("links", []),
        "campaign": graph.get("campaign"),
        "cities": cities,
        "routes": routes,
    }


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            try:
                command = json.loads(raw)
                if command.get("type") == "ping":
                    response = {"type": "pong"}
                elif command.get("type", "get_map") == "get_map":
                    response = self.server.map_payload
                else:
                    response = {"type": "error", "message": "unknown command"}
            except (json.JSONDecodeError, AttributeError) as exc:
                response = {"type": "error", "message": str(exc)}
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, payload):
        self.map_payload = payload
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="Noveria map TCP bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    payload = build_map()
    with Server((args.host, args.port), payload) as server:
        print(f"Noveria TCP map server: {args.host}:{args.port} ({len(payload['tiles'])} tiles)", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
