# Architecture viewer: экономика Noveria

Минимальный браузерный просмотр архитектуры экономики: кто владеет состоянием,
в каком порядке идёт месяц и где именно в коде это записано. Viewer ничего не
меняет в игре — он только читает `architecture.json`.

Три представления:

1. **Карта экономики** — Cytoscape-граф: узлы (класс, функция, store, kernel,
   boundary), рёбра (вызов, запись, владение, привязка, callback). Есть zoom/pan,
   поиск по классу/функции/файлу/строке, фильтр по домену, фильтр Python/Cython,
   подсветка владельцев, прямых записей и callbacks, клик по узлу с переходом к
   файлу и строке.
2. **Месячный ход** — Mermaid sequence diagram и таблица шагов. Варианты Python
   (`native_mode="off"`) и Cython (`native_mode="cython"`) показаны отдельно, с
   перечнем отличий.
3. **Владение и записи** — карточки по ключевым состояниям и таблица нарушений.

## Что вошло в анализ

Читались только вход хода, экономика V3, её native-границы, состояние/сохранение
и focused-тесты владения. Список файлов зафиксирован в `generate_architecture.py`
(`ANALYSED_FILES`) и попадает в `meta` JSON.

Python:

- `core/turn_service.py`, `core/world_service.py`, `core/economy_state.py`,
  `core/lab.py`, `core/db.py`
- `modules/economy/v3.py`, `modules/economy/support.py`, `modules/economy/__init__.py`
- `modules/economy/native/*.py` — границы и bindings (`monthly_boundary`,
  `monthly_hooks`, `cash_boundary`, `order_boundary`, `stock_store_boundary`,
  `stock_boundary`, `production_boundary`, `match_boundary`, `trade_boundary`,
  `consumption_boundary`, `owner_consumption_boundary`, `topup_boundary`,
  `quote_boundary`, `demand_boundary`, `money_boundary`, `dividend_boundary`,
  `enterprise_settlement_boundary`, `settlement_boundary`, `wage_boundary`,
  `project_completion_boundary`, `project_slot`, `material_boundary`,
  `delivery_boundary`, `material_finalize_boundary`, `cancel_boundary`,
  `plan_boundary`, `report_history_boundary`, `event_buffer_boundary`,
  `event_writer`, `__init__`)

Cython:

- `monthly_driver.pyx`, `serial_step.pyx`, `cash_store.pyx`, `stock_store.pyx`,
  `order_store.pyx`, `world_state.pyx`, `stock_kernel.pyx`, `production_kernel.pyx`,
  `match_kernel.pyx`, `match_driver.pyx`, `trade_kernel.pyx`, `consumption_kernel.pyx`,
  `quote_kernel.pyx`, `demand_kernel.pyx`, `money_kernel.pyx`, `settlement_kernel.pyx`,
  `plan_kernel.pyx`

Не читались: worldgen, routing, UI, остальные тесты, временные файлы и артефакты
сборки. Сгенерированные `.c`, `build/` и `.pyd` в анализ не входят.

## Требования

- Python 3.12 (только для генератора JSON)
- Node.js ≥ 20 и npm (для viewer)

## Установка зависимостей

```bash
# из корня репозитория
cd architecture/viewer
npm install
```

Генератор использует только стандартную библиотеку Python — дополнительных
пакетов не нужно.

## Как сгенерировать architecture.json

```bash
# из корня репозитория
python architecture/generate_architecture.py
```

Скрипт:

- сам находит строки по объявлению символа (`ast` для `.py`, разбор для `.pyx`);
- проверяет, что найденная строка существует и содержит ожидаемый якорь;
- падает с понятной ошибкой, если символ или якорь не найден;
- проверяет, что каждая связь, шаг, владелец и нарушение ссылаются на
  существующие узлы, и что висячих узлов нет;
- пишет `architecture/architecture.json` и копию в
  `architecture/viewer/public/architecture.json`.

Проверить JSON без браузера:

```bash
node architecture/check_architecture.mjs
```

Проверяет разделы, локации (файл есть, строка в диапазоне, якорь в строке),
ссылочную целостность и висячие узлы.

## Как запустить viewer

Разработка (HMR):

```bash
cd architecture/viewer
npm run dev
# http://127.0.0.1:5178/
```

Сборка и статический предпросмотр:

```bash
cd architecture/viewer
npm run build
npm run preview
# http://127.0.0.1:5178/
```

`npm run build` кладёт JSON рядом с бандлом (`dist/architecture.json`), поэтому
`dist` можно открыть и любым статическим сервером.

### Ссылка «открыть файл и строку»

Клик по локации ведёт в редактор по схеме `vscode://file/<путь>:<строка>`. База
пути задаётся переменной окружения:

```bash
VITE_REPO_BASE=vscode://file/D:/rpg/step8-cash/ npm run dev
```

По умолчанию используется `vscode://file/D:/rpg/step8-cash/`.

## Проверки

```bash
cd architecture/viewer

npm run typecheck   # tsc для приложения и для тестов фильтров
npm test            # 14 тестов фильтров на реальном architecture.json
npm run smoke       # headless Edge/Chrome: карта, поиск, фильтры, sequence, ownership
```

`npm run smoke` требует запущенного viewer (`npm run dev` или `npm run preview`)
и браузер Edge/Chrome. Скрипт управляет браузером через CDP и проверяет, что
страница реально смонтировалась, canvas карты не пустой, поиск и фильтр домена
сужают набор узлов, Mermaid отрисовал диаграмму, а таблицы шагов и нарушений
заполнены.

## Структура

```
architecture/
  generate_architecture.py    генератор модели (единственный источник JSON)
  check_architecture.mjs      валидатор JSON и ссылок
  architecture.json           результат
  viewer/
    index.html
    package.json
    vite.config.ts
    tsconfig.json             приложение
    tsconfig.test.json        тесты фильтров
    smoke_viewer.mjs          рантайм-проверка в браузере
    public/architecture.json  копия для dev-сервера
    src/
      types.ts                схема JSON
      data.ts                 загрузка, валидация формы, ссылки на файл:строка
      filters.ts              чистые фильтры карты (тестируются отдельно)
      filters.test.ts         node --test по реальному JSON
      graph.ts                Cytoscape: создание графа и видимость
      GraphView.tsx           карта + поиск/фильтры/подсветка
      SequenceView.tsx        Mermaid + таблица шагов месяца
      OwnershipView.tsx       владельцы и нарушения
```

## Что подтверждено кодом

Каждая связь в JSON имеет `source_location`, и генератор сверяет якорь с
фактической строкой файла, поэтому «придуманных» рёбер в модели нет.

Подтверждённые владельцы состояния:

| Состояние | Владелец | Где |
|---|---|---|
| `balance`, `reserved`, `protected` | `CashStore` (+ `CashStorePython` как эталон) | `native/cash_store.pyx`, `native/cash_boundary.py` |
| `stock`, `stock_reserved` | `StockStore` (+ `StockStorePython`) | `native/stock_store.pyx`, `native/stock_store_boundary.py` |
| статус/остаток/цена/количество заявки | `OrderStore` (+ `OrderStorePython`) | `native/order_store.pyx`, `native/order_boundary.py` |
| отчёты, checkpoint | `EconomyState` | `core/economy_state.py` |

Подтверждённые прямые записи (то, что не идёт через владельца):

- `public_projects` — присваивается в `v3.start_public_project` и в
  `wage_boundary` (два места, владельца нет);
- поля проекта (`work_done`, `status`) — `wage_boundary.run_advance_native`;
- `production_plan` — `plan_boundary.PlanHooks.commit_plan`;
- числовые поля предприятия (`status`, `output`, `production_cost`, …) —
  `production_boundary.run_enterprises`;
- метаданные заявки (`project_id`, `reserved_before_top_up`,
  `superseded_by_top_up`, `reserved_at_clearing`) — `v3` и `monthly_hooks`;
- `values` склада — только зеркалом в dict (`stock_store` `set_value`);
- `unmet_solvent` — пишут и kernel, и Python-эталон.

Подтверждённые Python-callbacks из горячего цикла:

- `monthly_driver` → хуки месяца и Python-фазы;
- `match_boundary.run_buyers_python` → `commit_trade` на каждую сделку;
- `quote_kernel`, `money_kernel`, `demand_kernel`, `settlement_kernel` →
  хуки чтения (котировки, касса, склад, население) внутри циклов;
- `match_driver.pyx` содержит callbacks, но в production не подключён.

Каждое из этих мест попало в `violations` со ссылкой на строку.

## Ограничения

- Связи построены по точкам вызова в коде, а не трассировкой выполнения:
  динамические вызовы через `getattr`, словари и monkey-patch в модель не попали.
- Порядок фаз зафиксирован для `planning_mode="v3"` и `investment_mode="v3"`.
  Ветки `lagged_batch`, `snapshot_batch` и `async_snapshot_batch` в сценарий не
  включены.
- Python-эталон месяца (`monthly_step_python`) не показан отдельным вариантом
  sequence: он используется как fallback и в differential-тестах. При этом
  `monthly_boundary` подменяет `monthly_step` на Cython-драйвер, поэтому вариант
  «Python» — это режим фаз, а не режим оркестрации.
- Cython-строки указаны по `.pyx`. После пересборки расширений они не меняются,
  но и `.pyd` не анализировались.
- Точный порядок фаз подтверждён чтением драйвера; порядок внутри фаз (порядок
  обхода счетов и предприятий) задаётся RNG и в модель не вынесен.
- Модель покрывает экономику и её границы. Worldgen, routing, UI и остальной
  репозиторий вне scope.
