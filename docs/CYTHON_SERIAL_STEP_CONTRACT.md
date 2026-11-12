# Cython V3: контракт первой контрольной остановки

Дата: 12.11.2026. База: `750ffea1e29bd49b4cf544c109d8fa29498a222e`.

Это контракт до переноса игровой логики. Собран только технический каркас.
Обычный `Economy.step()` остаётся V3; `native_mode='cython'` пока завершается
`NotImplementedError` до изменения состояния, а отсутствие расширения выдаёт
ошибку сборки. Fallback на V3 при явном native-запросе отсутствует.

## 1. Фактический порядок вызовов

После пролога месяца выполняется:

```text
update_prices()
prepare_recovery_quotes()
sell_orders(exclude=())                  # planning_mode='v3'
households = shuffled(household accounts)
household_requests(basic_needs)
household_requests(extra_needs)
owner_requests()
recover_enterprises()
public_project_requests()
→ project_requests()                     # за пределами прототипа
→ plan_enterprises()                     # за пределами прототипа
```

На реальном мире, seed `73129`, `detail=none`, первый месяц: 458 счетов,
236 предприятий, 1506 созданных заявок и 37 вызовов `transfer` внутри границы
(в первом месяце все переводы имеют нулевую сумму и не создают flow). За
12 месяцев каждый месяц были четыре RNG-вызова внутри границы:

1. `sell_orders`: канонически отсортированные счета, `rng.shuffle`;
2. выбор `households` в `step`: канонически отсортированные домохозяйства;
3. `owner_requests`: канонически отсортированные private/merchant;
4. `recover_enterprises`: канонически отсортированные предприятия.

В первом месяце длины списков: 452, 168, 12 и 230. Длины и момент вызовов
нужно сохранять. `pack` не выполняет перестановки и не заготавливает числа.
Cython вызывает `self.rng.shuffle()` с GIL над списком числовых индексов в том
же месте; индексы отображаются на те же канонически отсортированные объекты.
Порядок `households` возвращается Python для последующих `match`, top-up и
потребления. `existing` для `plan_enterprises` остаётся исходным списком пролога.

## 2. Вход `pack`

### Индексация

- Счета и предприятия: порядок уже отсортированных в прологе словарей;
  отдельные канонические таблицы ID для всех `shuffled` и `sorted`.
- Товары: стабильный числовой индекс; отдельно сохраняются порядок `cfg.goods`,
  порядок ключей каждого рецепта и сортировка по ID там, где использует V3.
- Площадки: стабильный числовой индекс; route component и distance остаются
  точными `float64`, полученными существующим `path()`.
- ID заявки не генерируется отдельным счётчиком: это
  `f'{month}-{len(report["orders"])}'` на момент создания.

Не создаются матрицы «все товары × все заявки × все площадки». Маршрутные
данные и книги строятся только для реально используемых пар и товаров; стоимость
этой подготовки измеряется отдельно.

### Неизменяемые внутри границы данные

- `meta.month`, `meta.public_orders`, `meta.price_feedback`, `meta.market_history`;
- `cfg`, `rules`, `goods`: цены, веса, inputs, equipment, durable.wear_rate,
  resource, labor и остальные используемые параметры;
- `tiles`: `province_id`, `resources`; route components и расстояния;
- население из `entities[group_id].properties.population`;
- классовые переключатели V3 и `EPS`.

### Изменяемые счета

| Группа | Поля |
|---|---|
| Идентичность и география | `id`, `kind`, `state_id`, `province_id`, `tile_id`, `cell_id`, `group_id` |
| Деньги | `balance`, `reserved`, `protected`, `operating_protected` |
| Товары | `stock`, `stock_reserved`; `values` внутри границы не изменяются |
| Цены и история | `ask_prices`, `stock_sales_history` |
| Домохозяйства | `needs` |
| Собственники | `owner_free_cash`, `owner_consumption_budget`, `project_reserve` |
| Частный проект | `project.working_cash`, `delivery`, `capacity`, `location.tile_id`, `materials` |
| Городские проекты | `public_projects`: `id`, `mode`, `max_workers`, `material_bill`, `equipment`, `materials`, `equipment_held`, `support_stock` |

Для `project_cash_reserve()` также читаются существующие `report.orders`:
`participant`, `side`, `purpose`, `good`, `status`, `remaining`, `reserved`.

### Изменяемые предприятия

`id`, `good`, `owner_id`, `status`, `closed`, `suspended`, `available_from`,
`unviable_months`, `price`, `production_price`, `capacity`, `tile_id`,
`province_id`, `state_id`, `sales_history`, `sales_forecast`, `planned_output`,
`production_plan.output`.

`production_outlook()` рассчитывает результат на копии предприятия и не должен
записывать прогноз в исходный объект внутри `recover_enterprises()`.

### Отчёт на входе и выходе

- `orders`: читается длина и существующие записи; новые записи добавляются в конец;
- `trades`: читается `production_outlook()`; в обычном `step` здесь ещё пусто;
- `flows`: пополняется переводами recovery;
- `lifecycle`: пополняется при повторном открытии;
- `owner_consumption`: строки собственников и порядок их order ID.

## 3. Изменяемое native-состояние

Все фазы используют одно состояние вызова. Любая операция сразу меняет native
`accounts`, `enterprises`, `orders`, `sells`, проекты и report-буферы. Нельзя
вычислять последующую фазу по входному снимку `pack`.

Кроме плоских числовых массивов нужны упорядоченные структуры:

- `orders` в порядке создания;
- `sells`: порядок первого появления товара и порядок офферов внутри товара;
  оба влияют на обход при отмене офферов во время recovery;
- `public_orders` и `price_feedback` в исходном порядке;
- `sales_history` и `stock_sales_history` в исходном порядке;
- recovery quotes в порядке `(depth(good), enterprise.id)`.

Арифметика остаётся `float64`, без fast-math, изменения порядка слагаемых и
«улучшенного» округления. Места, где Python 3.12 использует компенсированный
`sum()`, либо сохраняются на Python, либо получают отдельно доказанную точную
реализацию; обычный native `+=` такой эквивалентностью не обладает.

## 4. Журнал и возвращаемый результат

Каждое событие содержит сквозной `execution_seq`. Предлагаемые события:

| Событие | Содержимое и обязательный apply |
|---|---|
| `set_enterprise_price` | Индекс фирмы и итоговая цена |
| `set_account_ask_price` | Счёт, товар и итоговая цена, включая `setdefault` без заявки |
| `clear_production_price` / `set_production_price` | Индекс фирмы и итоговая recovery-цена |
| `create_order` | Полный итоговый объект заявки после ограничения деньгами/складом; резерв счёта меняется ровно один раз |
| `patch_order` | `need_quantity`, `budget_price`, `unfunded_need`, `adaptive_household_budget`, `project_id` и другие фактические поля |
| `set_household_need` | Итоговые `needed`, `consumed=0`, `deficit=needed` |
| `set_owner_budget` | Три поля счёта и исходная строка `owner_consumption` |
| `transfer` | Итоговые sender, recipient, amount, reason; один перевод и один flow |
| `cancel_order` | ID и reason; снятие денежного/товарного резерва по состоянию на этот seq |
| `reopen_enterprise` | `status='idle'`, `closed=None`, `unviable_months=0` и исходная lifecycle-строка |
| `add_operating_protection` | `operating_protected=hold` и прибавление `hold` к `protected` |
| `claim_public_stock` | Итоговый project bucket, изменения offer `quantity/remaining/backing`, `stock_reserved` |
| `invalidate_market_books` | Исходный момент полной инвалидации в начале `sell_orders` |

`create_order` содержит ID, participant, good, quantity, remaining, price, side,
purpose, status, reason, expires, final_consumption, state/province/tile,
reserved и backing. Apply не повторно вычисляет quantity, price, бюджет,
спрос или маршрут, не вызывает RNG и не создаёт дополнительных событий.

Возвращаются также:

- итоговые рабочие массивы для независимого сравнения с состоянием после apply;
- `sells` как индексы заявок с исходным порядком товаров и офферов;
- порядок `households`;
- итоговые recovery quotes как `(enterprise_index, production_price)` по товару;
- итоговые версии кэшей и диагностические счётчики;
- trace решений и ID для сравнения с V3, не только итоговые суммы.

## 5. Кэши и версии

### Обязательное семантическое состояние

- `_market_quote_version`;
- `_market_quote_versions[good]`;
- `_market_quotes_epoch`;
- `_demand_revision[good]`;
- `_trades_by_seller` — внутри границы не меняется;
- `_recovery_quotes` — читается не только recovery, но и последующими
  `quoted_cost()` из `project_requests()` / `project_material_requests()`.

Правила инвалидации исходные:

- начало `sell_orders`: удалить market books, увеличить общую версию и epoch;
- создание sell, изменение/отмена sell: увеличить общую версию и версию товара,
  сбросить `_quoted_cost_cache` и `_reference_price_cache`;
- создание/отмена buy: увеличить `_demand_revision[good]`;
- изменение цены/площадки/состава офферов: не вводить новых правил, а сохранить
  существующие вызовы `invalidate_market_books()` и `mark_market_quotes_dirty()`.

### Производные кэши

`_landed_cache`, `_freight_factors`, `_market_signal_cache`,
`_customer_order_index`, `_customer_quotes_cache`, `_route_order_components`,
`_market_books`, `_market_source_index`, `_quoted_cost_cache`,
`_reference_price_cache`, `_unit_cost_cache`, `_customer_demand_rows_cache`,
`_production_outlook_cache`, `_stock_sales_cache`, `_outlook_goods`.

Их содержимое может быть восстановлено лениво, но только если это не меняет
решения и полная стоимость реального пути входит в замер. Нельзя оставить Python
запись, построенную по устаревшим объектам. При очистке кэша сохраняются точные
финальные версии; `_recovery_quotes` не очищается, а восстанавливается обязательно.
На остановке 2 сравниваются семантические ключи/значения и версии, а не адреса
объектов. `OfferView.first_live` — производный курсор, а не игровое состояние.

## 6. Что читают последующие Python-фазы

### `project_requests()`

- все заявки `report.orders` и `sells`: ID, порядок, стороны, товары, цены,
  `quantity`, `remaining`, `reserved`, `backing`, `status`, purpose, project flags;
- счета: kind, geography, `balance`, `reserved`, `protected`,
  `operating_protected`, `payroll_protected`, `stock`, `stock_reserved`,
  `project` и его материалы/деньги/location;
- предприятия: status, available_from, good, price, capacity, history;
- recovery quotes, market history, route/landed данные;
- версии спроса и предложения, от которых зависят customer/demand/outlook caches.

### `plan_enterprises()`

- исходный `existing` и новый RNG-вызов уже после `project_requests()`;
- `sells` и все живые офферы;
- `status`, `suspended`, `available_from`, цена и прогнозные поля предприятия;
- `free_cash`: `balance - reserved - protected`;
- `operating_protected` из recovery;
- склады, товарные резервы, private/public project locks;
- report orders/trades, recovery quotes, demand/quote/outlook versions.

### Остальной `step()`

- порядок `households` и `sells`;
- `report.owner_consumption`, orders, flows, lifecycle;
- поля needs и необязательные флаги заявок;
- состояние public project buckets для резервирования труда и строительства.

## 7. Apply, replay и ошибки

1. Apply выполняет только события журнала в порядке `execution_seq`.
2. После каждого перенесённого участка сравнивается независимая копия V3 и
   native trace, RNG state, orders/ID, деньги, склады, резервы, статусы, проекты,
   report и семантика кэшей.
3. Полный replay сначала выполняется на независимой копии состояния.
4. Ошибка apply оставляет явную ошибку прототипа; V3 автоматически не
   запускается поверх частично изменённого состояния.
5. До согласования этого контракта перенос `update_prices` и остальных игровых
   методов не начинается.

## 8. Технический каркас

Добавлены `modules/economy/native/serial_step.pyx`, loader, локальный
`setup.py`, `requirements-native.txt` и smoke-тесты. Команды:

```bash
python -m pip install -r requirements-native.txt
cd modules/economy/native
python setup.py build_ext --inplace
```

На Windows используется тот же setuptools/Cython-путь и доступный компилятор
MSVC; платформенных GCC-флагов нет. Проверено на Linux/Python 3.12: расширение
собирается, импортируется, `weighted_sum` принимает typed memoryviews.
