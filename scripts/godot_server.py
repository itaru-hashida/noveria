"""Минимальный TCP-мост Python -> Godot для просмотра текущей карты.

Протокол: одна JSON-команда на строку, один JSON-ответ на строку.
Команды: ``{"type":"get_map"}`` и ``{"type":"ping"}``.
"""
from __future__ import annotations

import argparse
import json
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_map():
    from main import create_application

    service = create_application()
    graph = service.world_graph()
    tiles = graph["tiles"]
    width = max((int(tile["x"]) for tile in tiles), default=-1) + 1
    height = max((int(tile["y"]) for tile in tiles), default=-1) + 1
    return {
        "type": "map",
        "version": 1,
        "width": width,
        "height": height,
        "tiles": tiles,
        "entities": graph["snapshot"].get("entities", []),
        "links": graph["snapshot"].get("links", []),
        "campaign": graph.get("campaign"),
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
