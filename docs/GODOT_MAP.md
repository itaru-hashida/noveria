# Godot-карта: первый вертикальный срез

Сейчас Godot — тонкий клиент. Python поднимает TCP-сервер, читает мир через
`WorldService.world_graph()`, а Godot получает снимок карты по newline-delimited
JSON. Ядро и LadybugDB остаются в Python; переносить их в Godot не нужно.

Из корня репозитория:

```powershell
python scripts/godot_server.py
```

В другом окне откройте папку `godot/` в Godot 4.x и нажмите Run. Клиент
подключится к `127.0.0.1:8765` и нарисует terrain-клетки. Если сервер не запущен,
Godot покажет статус подключения.

Протокол пока намеренно минимальный:

```json
{"type":"get_map"}
```

Ответ содержит `width`, `height`, `tiles`, `entities`, `links` и `campaign`.
Следующий шаг — добавить команды хода и поток событий, не меняя этот контракт.
