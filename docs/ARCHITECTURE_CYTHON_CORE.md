# Архитектурная цель: Cython core + Python scripting

## Цель

Прийти к устойчивому числовому ядру экономики на Cython/C, оставив Python
серверным слоем сценариев, правил, AI и пользовательских расширений. UI игры —
отдельный Godot-клиент, подключённый к серверу по TCP.

Это не означает переписать игру одним большим коммитом. Переход делается по
стабильным границам, чтобы каждый этап сохранял exact-поведение и оставался
пригодным для разработки.

## Целевая схема

```text
Godot UI client  <── TCP protocol ──>  Python server/orchestration
                                             |
                                      stable typed API
                                             |
                                  Cython EconomyCore
                                             |
                                  numeric state / RNG
```

### Cython core владеет

- accounts, enterprises, goods и warehouses;
- orders, market books, prices и reservations;
- production, consumption и trade;
- projects, investment queue и construction state;
- deterministic RNG и порядок исполнения;
- persistent numeric buffers и индексы.

### Python остаётся для

- сценариев и модификаторов;
- AI hooks и пользовательских правил;
- событий и orchestration верхнего уровня;
- TCP-сервера и протокола команд/событий;
- отчётов, отладки и tooling;
- загрузки/сохранения и tooling.

Python не должен обращаться к внутренним Cython-массивам по одному полю. Обмен
идёт через стабильные API-команды и пакетные snapshots/deltas:

```python
core.submit_orders(order_batch)
core.apply_script_effect(effect_batch)
core.step()
events = core.drain_events()
```

## Как выйти из текущего модульного монолита

1. **Зафиксировать владельца состояния.** Для каждого поля определить: core,
   scripting или только report/cache. Не переносить формулы, пока ownership не
   ясен.
2. **Стабилизировать typed boundary.** Ввод и вывод фазы описывать плоскими
   массивами/таблицами с версиями schema. Python orchestration может временно
   оставаться прежним.
3. **Переносить вертикальными срезами.** Фаза переносится целиком вместе с её
   reservations, RNG и journal, а не отдельной арифметической функцией.
4. **Сделать persistent core state.** После первого создания мира следующие
   ходы обновляют buffers/deltas, а не pack/unpack всего мира.
5. **Оставить Python compatibility adapter.** Старые Python методы вызывают core
   API, пока все потребители не перейдут на новую границу.
6. **После каждой фазы проверять exact replay.** Сравнивать state, decisions,
   reservations, report и RNG на нескольких seed.

## Практический порядок миграции

1. Карта, recipes, индексы и distance cache.
2. Orders/market core и batch market queries.
3. Production/consumption/trade.
4. Project requests и material requests.
5. Enterprise planning.
6. Остальные step-фазы и затем перенос владения state из Python в core.

Каждый этап должен иметь:

- один стабильный Cython entry point;
- Python adapter только для materialization и scripting hooks;
- benchmark cold/warm;
- exact replay и RNG comparison;
- fallback на старый Python путь до завершения миграции.

## Правила API

- Не передавать `dict`/bound methods в горячий Cython-цикл.
- Не делать Python callback для каждой заявки или клетки.
- Новые игровые правила добавлять как batch commands/effects, а не прямой доступ
  к внутренним массивам.
- Сохранять порядок floating-point операций и deterministic execution order.
- Менять schema только версией и с compatibility adapter.

## Критерий успеха

Игра должна запускаться как сервер с persistent Cython core, а Godot должен быть
тонким клиентом, получающим snapshots/events и отправляющим команды по TCP.
Добавление нового сценарного правила не требует переписывать ядро; добавление
новой числовой механики расширяет typed API и core state контролируемым отдельным
этапом.
