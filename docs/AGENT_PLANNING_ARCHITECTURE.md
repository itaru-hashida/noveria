# Агентная архитектура: схема данных и порядок фаз

Ветка `experiment/parallel-agent-planning`. Черновик от 10.11.2026, до кода.
Цель — shadow-прототип рядом с V3, без замены траектории.

## Решения владельца

Приняты в этой сессии, меняются только явно:

1. **Пул процессов** `ProcessPoolExecutor` — основной механизм. Потоки — только
   контрольный замер. Эмуляция порядка — только для отладки FIFO, не для
   оценки скорости.
2. **Снимок один раз** в `shared_memory`/mmap; в задачу идёт `agent_id` и
   индексы. Полные dict между worker-ами не пересылаются.
3. **Лаг в один месяц на звено цепочки.** Два раунда внутри месяца не нужны.
4. **Внешний `budget_cap`**, посчитанный до запуска worker-ов. Внутри `plan()`
   деньги не меняются.
5. **Корректность FIFO, отчёт и отдельное время** на 240 фирмах; ускорение
   здесь не требуется. Обязателен synthetic scaling на 1000–2000 и ~5000 фирм,
   workers `1/2/4/8`.
6. **FIFO по моменту `enqueue`**, а не по порядку обхода futures.

## Пул процессов возможен

`docs/SANDBOX_MULTIPROCESSING.md` — рабочая подмена
`multiprocessing.connection.Pipe` с явным `SECURITY_ATTRIBUTES`. Проверено:
`ProcessPoolExecutor.map` и `mp.Pool.map` работают.

`multiprocessing.Queue` строится на том же `Pipe`, значит включается тем же
патчем. Модуль патча — `shadow/sandbox_mp.py`, импортируется один раз при
старте прототипа, до создания пула.

## Замеренная база

ПАРАМЕТРЫ: seed=73129 months=12 warmup=2 detail=none

команда: `python audit/profile_run.py --seed 73129 --months 12 --warmup 2 --detail none --no-cprofile`

результат: **0,608 s/ход**, всего 7,3 s.

| фаза | всего, s | доля |
|---|---|---|
| `project_requests` | 1,73 | 23,8% |
| `recover_enterprises` | 1,46 | 20,0% |
| `match` | 0,82 | 11,2% |
| `pay_dividends` | 0,49 | 6,8% |
| `input_requests` | 0,48 | 6,6% |
| `household_requests` | 0,33 | 4,5% |
| `produce` | 0,26 | 3,6% |
| `update_prices` | 0,25 | 3,4% |
| `validate` | 0,23 | 3,1% |

Планирование, которое переезжает в агентов:
`recover_enterprises + input_requests + project_requests + update_prices` =
**3,92 s из 7,3 s = 54% месяца**. `match` (11%) остаётся последовательным по
постановке и не ускоряется — это потолок Amdahl для этой работы.

Размеры мира: 1200 плиток, 243 фирмы, 78 торговых площадок, 12 товаров,
353 оффера. Полная матрица расстояний 1200×1200 float32 = **5,8 МБ**,
строится 3,2 s.

## Схема данных снимка

Строится **один раз за месяц**, до запуска пула, только для чтения. Всё —
числовые массивы; строки и dict в горячей части не участвуют.

```python
class Collision:                      # несовместимость с V3 по построению
    pass


class Snapshot:
    """Неизменяемый снимок месяца. Хранится в shared_memory, не копируется."""

    month: int                        # scalar
    revision: int                     # scalar, растёт раз в месяц

    # --- агенты (фирмы) ---
    agent_id:      int32[n_agents]    # внутренний номер, не строковый id
    agent_firm:    int32[n_agents]    # индекс в таблице строковых id (для отчёта)
    agent_good:    int32[n_agents]    # товар фирмы
    agent_tile:    int32[n_agents]    # площадка фирмы, индекс в market_tiles
    agent_province:int32[n_agents]
    agent_state:   int32[n_agents]
    agent_capacity:float64[n_agents]
    agent_price:   float64[n_agents]  # цена продажи прошлого месяца
    agent_stock:   float64[n_agents, n_goods]      # свой склад, включая свой товар
    agent_values:  float64[n_agents, n_goods]
    agent_balance: float64[n_agents]
    agent_budget:  float64[n_agents]  # budget_cap, посчитан заранее
    agent_hist:    float64[n_agents, 3, 3]         # sold, output, input_limited

    # --- товары ---
    good_inputs:   float64[n_goods, n_goods]       # рецепт: доля входа
    good_labor:    float64[n_goods]
    good_equip:    float64[n_goods, n_goods]       # износ на единицу выпуска
    good_weight:   float64[n_goods]
    good_taxed:    uint8[n_goods]                  # входит в потребкорзину

    # --- рынок: предложение ---
    offer_good:    int32[n_offers]
    offer_seller:  int32[n_offers]    # номер агента (не всякий продавец — фирма)
    offer_seller_tile: int32[n_offers]
    offer_price:   float64[n_offers]
    offer_remaining: float64[n_offers]
    offer_index:   int32[n_goods, n_markets + 1]   # CSR: офферы на площадку и товар

    # --- рынок: спрос (публичная книга заявок) ---
    ask_buyer:     int32[n_asks]
    ask_buyer_tile:int32[n_asks]
    ask_good:      int32[n_asks]
    ask_price:     float64[n_asks]
    ask_remaining: float64[n_asks]
    ask_backed:    float64[n_asks]     # reserved: обеспеченная деньгами часть
    ask_index:     int32[n_goods, n_markets + 1]

    # --- доставка ---
    distance:      float32[n_markets, n_markets]   # симметрично, 0 по диагонали
    reachable:     uint8[n_markets, n_markets]     # разные транспортные компоненты

    # --- сигнал рынка ---
    signal_volume: float64[n_goods, n_provinces]
    signal_unmet:  float64[n_goods, n_provinces]
    signal_landed: float64[n_goods, n_provinces]   # -1 = нет данных
```

### Почему матрица по площадкам, а не по плиткам

Сейчас 78 площадок: 78² = 6084 пары, 24 КБ float32. Полная матрица по 1200
плиткам — 5,8 МБ, и она не нужна: доставка считается только между продавцом и
покупателем, а оба стоят на торговых площадках (посадочных тайлах фирм и
городов). При росте до 30 государств × 30 провинций плиток будет ~41 000,
матрица 41 000² float32 = 6,7 ГБ — и это убивает архитектуру на бумаге ещё до
кода. Индекс по площадкам растёт как число рынков, а не клеток: при 5000
фирмах это ~1500² = 9 МБ.

Это же правило чинит квадратичный участок V3 (`customer_quotes` сканирует весь
ордербук), о котором предупреждает `AGENTS.md` §9.

### Что снимок НЕ содержит

- внутренние планы других фирм (`planned_output`, `production_plan`, маржа);
- чужие счета (`balance`, `reserved` других агентов);
- чужие склады (`stock`);
- владельца фирмы как источник денег — его видно только через `budget_cap`.

Пекарня не знает мельницу. Она видит 353 оффера, цены, доставку и спрос.

## AgentView и чистый plan()

```python
@dataclass(frozen=True, slots=True)
class AgentView:
    agent: int            # номер в снимке
    good: int
    tile: int             # индекс площадки
    capacity: float
    price: float
    stock: tuple[float, ...]      # свой склад, длиной n_goods
    balance: float
    budget: float                 # budget_cap
    history: tuple[tuple[float, float, float], ...]   # 3 последних месяца
```

`plan(view, snapshot_view) -> tuple[Intent, ...]`

Требование: **никаких `id()`-кэшей и никакого общего состояния**. Кэши V3
(`_landed_cache`, `_customer_quotes_cache`, `_unit_cost_cache`,
`_production_outlook_cache`) ключуются по `id()` живых объектов; в дочернем
процессе `id` чужой, и переносить их нельзя. Поэтому:

- `distance` и `reachable` живут в снимке, а не в кэше;
- `lru_cache(maxsize=...)` только по неизменяемым числовым ключам
  `(revision, good_a, good_b)`;
- версия снимка (`revision`) входит в каждый ключ.

## Intent и Outcome

```python
@dataclass(frozen=True, slots=True)
class Intent:
    agent: int
    kind: str             # 'buy' | 'sell'
    good: int
    quantity: float
    price: float
    purpose: str          # 'Сырьё' | 'Оборудование' | 'Продажа запасов'
    enqueued_ns: int      # только для диагностики, порядок задаёт очередь


@dataclass(frozen=True, slots=True)
class Outcome:
    agent: int
    intent_seq: int
    filled: float
    average_price: float
    spent: float
    reason: str           # '' | 'Нет предложения' | 'Не хватило денег' | ...
    budget_left: float
    stock_left: tuple[float, ...]
```

## FIFO: обычная очередь поступления заявок

Под FIFO понимается **обычная очередь поступления заявок**, а не порядок
`agent_id`, не сортировка по приоритету и не «сначала собрать всех, потом
канонически отсортировать». Предприятия параллельно считают `plan()`. Как
только предприятие закончило расчёт, оно кладёт свою заявку в общую очередь.
`MarketExecutor` берёт заявки с начала очереди и обрабатывает в порядке
фактического поступления.

```text
Firm A закончил → enqueue(A)
Firm C закончил → enqueue(C)
Firm B закончил → enqueue(B)

очередь:  A → C → B
клиринг:  A → C → B
```

Скорость завершения worker-а — **часть новой модели** и может давать разный
порядок между запусками. Это допустимая недетерминированность. Если несколько
worker-ов закончили практически одновременно, порядок определяется атомарным
порядком `put()`.

### Streaming execution, не gather

Наивная схема разрушает FIFO:

```text
executor.map(...)  →  список результатов в порядке ВХОДА задач
```

Проверено (`tmp/mp_probe/fifo_control.py`): задачи поданы `[1, 2, 3]` с
задержками `0.30 / 0.15 / 0.01 s`, то есть готовность `[3, 2, 1]`.

| способ | получено | что это |
|---|---|---|
| `executor.map` | `[1, 2, 3]` | порядок входа, FIFO потерян |
| `as_completed` | `[3, 2, 1]` | порядок готовности |
| `Queue` + `put` из worker-а | `[3, 2, 1]` | порядок готовности |

Нужен producer/callback, выполняющий атомарный `queue.put(intent)` **сразу**
после завершения `plan()`:

```text
ProcessPoolExecutor
    ↓ worker завершился
callback / result handler
    ↓ немедленный queue.put()
FIFO IntentQueue
    ↓
MarketExecutor: intent = queue.get(); outcome = execute(intent)
```

Пока очередь пуста, executor ждёт новые заявки через long polling. Окно
закрытия нужно **только** для перехода к следующей фазе, а не для
предварительного сбора всех предприятий. При `deadline` — принудительное
закрытие; агенты, не успевшие подать намерение, фиксируются как `timeout` и в
этом месяце не торгуют.

### Грабли передачи очереди (Windows/spawn)

`multiprocessing.Queue` **нельзя** передать аргументом задачи:

```text
RuntimeError: Queue objects should only be shared between processes through inheritance
```

Очередь передаётся наследованием при создании процесса — то есть
`initializer`/`initargs` пула. Проверено (`tmp/mp_probe/streaming_fifo.py`):
порядок поступления `[3, 2, 1]`, время в очереди монотонно растёт.

### Что пишется в отчёт shadow-режима

```text
agent_id
enqueue_sequence     № постановки в очередь
enqueue_time         perf_counter_ns в worker-е
dequeue_sequence     № взятия исполнителем
outcome              исполнено / частично / отказ + причина
```

`enqueue_time` — только диагностика. Сравнивать часы между процессами нельзя,
на порядок они не влияют: порядок задаёт сам `put()`, а `enqueue_sequence` и
`dequeue_sequence` пишутся из одной точки и потому согласованы. Равенство
`enqueue_sequence == dequeue_sequence` и есть проверка, что FIFO не нарушен
внутри месяца.

## Порядок фаз месяца

```text
1. snapshot(N)        сборка снимка, запись в shared_memory      (main)
2. budget(N)          budget_cap по правилам V3 до пула          (main, последовательно)
3. plan(N)            пул: plan() на неизменяемом снимке         (workers, параллельно)
4. streaming          worker завершился → queue.put(intent)       (workers, по готовности)
5. execute            MarketExecutor: get → reserve → execute     (main, последовательно)
6. observe            агенты получают Outcome                     (main)
7. produce/consume/settle/pay_dividends — фазы V3 без изменений   (main)
8. snapshot(N+1)
```

Шаги 3–5 идут **одновременно**: клиринг не ждёт, пока спланируют все, — он
забирает заявки по мере поступления. Шаги 3–5 заменяют
`recover_enterprises + input_requests + update_prices`.
`project_requests` подключается отдельным шагом, когда заработает базовый
цикл: это 23,8% месяца и самая дорогая часть.

Следствие streaming-клиринга, которое надо держать в голове: исполнитель
исполняет заявку, когда книга ещё не полна. Заявка, поданная раньше, может
получить товар, который позже поданный конкурент уже не увидит. Это не дефект,
а суть выбранной модели; в отчёте каждый такой случай виден по
`enqueue_sequence` и остаткам книги на момент исполнения.

## Лаг цепочки: как он выглядит

Месяц N: мельница видит спрос на муку от прошлого месяца и планирует по нему.
Месяц N+1: мука мельницы попадает в офферы, пекарня реагирует на фактическое
предложение.

Следствие, которое надо помнить: цепочка `зерно → мука → хлеб` при звенности 3
разгоняется на 3 месяца медленнее V3. Это не дефект прототипа, а свойство
архитектуры; сравнивать с V3 нужно по инвариантам и по устойчивости, а не по
месячному совпадению отпечатка.

## Проверки и критерии

Обязательные параметры: `seed=73129,17,7,4231`, `months=12`, `warmup=2`,
`detail=none`, карта `40×30`.

Послойно, в этом порядке:

| шаг | что проверяется | как |
|---|---|---|
| 1. FIFO | порядок намерений = порядок `put`, а не futures | эмуляция: задержка в worker-е, проверка порядка в журнале |
| 2. Корректность | ни одной двойной продажи/покупки, деньги и товар сходятся | инварианты V3 + новые проверки резервов |
| 3. Память | снимок не копируется на задачу | размер RSS worker-а, число байт в `queue` |
| 4. Отчёт | по каждому агенту: намерение, номер в FIFO, исполнение, причина, остатки | CSV/JSON в `tmp/` |
| 5. Время 240 фирм | отдельное число, без обещаний | `--plain`, workers 1/2/4/8 |
| 6. Масштабирование | 1000–2000 и ~5000 фирм, одинаковый снимок | синтетический мир, workers 1/2/4/8 |

Скорость на 240 фирмах **не** критерий приёмки: overhead пула на 243 агентах
может съесть весь выигрыш, и это ожидаемый результат, а не провал.

## Открытые вопросы

1. **Обеспеченность спроса.** `ask_backed` (`reserved`) — деньги покупателя.
   В книге заявок V3 это поле публично (`backing`), но это чужие деньги.
   Решение: оставить в снимке для верности прогнозу и записать в отчёт как
   осознанное нарушение границы «агент не видит чужие счета». Альтернатива —
   отдать обеспеченность исполнителю, тогда прогноз выпуска завышается и
   расходится с V3 сильнее.
2. **`budget_cap`.** Для 240 фирм считается правилами V3 последовательно;
   какой именно набор (собственный `free_cash` + доступная часть
   `cash_after_commitments` владельца) — уточняется на шаге 2, по замеру
   расхождения с V3.
3. **Синтетический мир.** Нужен генератор миров на 1000–5000 фирм с
   сохранением связности транспортного графа. Отдельный шаг, потребует
   времени: наивное копирование провинций даёт рассыпание компонент.

## План работ

1. `shadow/sandbox_mp.py` — патч канала, проверка пула (готов, переносится из
   `docs/SANDBOX_MULTIPROCESSING.md`).
2. `shadow/agent_snapshot.py` — сборка массивов и запись в `shared_memory`,
   замер размера и времени сборки.
3. `shadow/agent_planner.py` — чистый `plan()`, сверка с `production_outlook`
   V3 на одном снимке, расхождения по каждому агенту.
4. `shadow/intent_queue.py` + `shadow/market_executor.py` — FIFO и
   последовательный клиринг, проверка инвариантов.
5. `shadow/runner.py` — shadow-прогон рядом с V3 на одном сохранении, отчёт.
6. Синтетический мир и scaling-замер.
