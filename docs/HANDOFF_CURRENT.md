# Текущий handoff

Ветка: `kimi/step7-native-project-numeric-ledgers`

Последний commit: `430bdd5 Move outer match loop to Cython`

## Цель

Persistent Cython simulation core; Python — scripting/orchestration/server API;
Godot — отдельный TCP-клиент.

## Уже есть

- project requests batch в Cython;
- lazy market support и persistent distance cache;
- update-prices persistent state;
- stock kernel;
- OrderStore и Cython OfferView;
- CashStore;
- Cython commit одной сделки;
- Cython inner match loop и внешний match driver;
- EconomyCore command/event API;
- uv lock/setup scripts.

## Exact/status

- project/match focused exact проходит на seed 17 и 73129;
- RNG сохраняется;
- известный многоходовый ULP report mismatch остаётся отдельной non-blocking задачей;
- полные pytest-прогоны могут упираться в Windows sandbox `tmp_path` errors.

## Следующий архитектурный шаг

Не переносить новые микрофункции. Следующий срез должен связать текущие
OrderStore/CashStore/stock boundary с последовательным trade execution и только
потом переносить остальные market phases.

Каждому агенту выдавать только узкий список файлов. Он обязан:

1. не читать весь репозиторий;
2. показать реально прочитанные файлы;
3. сделать focused tests и exact replay;
4. сделать отдельный commit и push;
5. остановиться при выходе за scope.

Архитектурные миграции принимаются по ownership/API/exact, оптимизации — только
по измеримому benchmark. Эти критерии не смешивать.
