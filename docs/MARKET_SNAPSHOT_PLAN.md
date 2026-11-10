# Общий неизменяемый снимок рынка: обследование и предложение

Этап 1 из задачи «подготовить основу для параллельного планирования».
Здесь только замер и предложение интерфейса. Ни одна экономическая формула,
FIFO, порядок заявок и RNG не менялись; `multiprocessing` не подключался;
`shadow/planner` не переписывался.

## 1. Что уже есть и как устроено

### 1.1 Где строится рыночная картина

| метод | строка | что делает | что кэширует |
|---|---|---|---|
| `_market_book(buyer, good, sells)` | `v3.py:1806` | одна отсортированная по landed-цене доска предложений для пары **товар × площадка покупателя** | `_market_books`, ключ `(id(sell_list), market_tile(buyer), good)`, держит ссылку на список |
| `offers(...)` | `1905` | ленивый `OfferView` поверх книги; `first_live` — курсор по уже выкупленным офферам | своих данных нет, читает книгу |
| `landed(seller, buyer, good, price)` | `1707` | доставка и полная цена по неизменной в течение месяца сети | `_landed_cache`, ключ `(seller_tile, buyer_tile, good)` |
| `quoted_cost(buyer, good, quantity, sells)` | `1941` | средневзвешенная цена объёма по книге + хвост по `reference_price` | `_quoted_cost_cache` с версией месяца |
| `reference_price(...)` | `1922` | цена лучшего оффера или сигнал рынка | `_reference_price_cache` |
| `customer_quotes(e)` | `2202` | заявки покупателей на товар `e['good']` с вычетом доставки | через `_customer_quote_index` |
| `_customer_quote_index(tile, good, orders)` | `1766` | пары (заявка, доставка) для площадки | `_customer_quotes_cache` |
| `customer_demand_groups(e)` | `2240` | агрегат одинакового спроса: участник, площадка, цена, предел, доставка, объём, ограниченный платёжеспособностью | `_customer_demand_groups_cache` |
| `reachable_demand(e, sells)` | `2272` | сколько из этого агрегата реально покроется книгой | не кэшируется |
| `production_outlook(e, sells)` | `2453` | прогноз выпуска: себестоимость, чистая цена, достижимый спрос, план | `_production_outlook_cache` с версиями книг |
| `investment_demand(candidate, sells)` | `2720` | остаточный спрос для инвестиционного кандидата | `_investment_demand_cache` |

Все кэши живут строго один отчётный месяц и чистятся в `reset_report`
(`v3.py:80`). Инвалидация точечная: `mark_market_quotes_dirty(good)`
(`v3.py:1657`) поднимает общую версию, версию товара и сбрасывает
`_quoted_cost_cache` с `_reference_price_cache`.

### 1.2 Чем это уже связано с будущим снимком

`shadow/agent_snapshot.py` собирает плоские массивы: `agent_*`, `good_*`,
`offer_*` (CSR по товару), `ask_*` (CSR), `distance` (площадка × площадка).
Это и есть первый набросок общего снимка: неизменяемые массивы, числовые ID,
одна область `shared_memory`. Чего в нём нет — агрегата спроса
(`customer_demand_groups`) и покадровых цен книги по площадкам покупателя:
`distance` без веса и тарифа, а landed считается как `distance*weight*rate`.

## 2. Замер

ПАРАМЕТРЫ: seed=73129 months=12 warmup=2 detail=none
команда:   `python audit/profile_run.py --seed 73129 --months 12 --warmup 2 \
                --detail none --no-cprofile --market-probe`

Без пробы тот же прогон: **8.1 s, 0.677 s/ход**. С пробой: 0.813 s/ход —
накладные расходы счётчиков около 20%, поэтому абсолютные секунды в таблице
ниже годятся для приоритизации, а не для «скорости игры».

```
метод                      вызовов    секунд мкс/вызов попаданий промахов
production_outlook            5858     2.819     481.2     1569     4289  (26.8%)
offers                      133021     1.741      13.1        -        -
quoted_cost                  60065     1.476      24.6    13446    46619  (22.4%)
_market_book                151088     1.444       9.6   144412     6676  (95.6%)
customer_quotes              13653     1.355      99.2     5786     7867  (42.4%)
landed                      877195     1.220       1.4   737970   139225  (84.1%)
investment_demand            16697     1.213      72.6    11343     5354  (67.9%)
customer_demand_groups        9759     0.931      95.4     1958     7801  (20.1%)
reachable_demand              4405     0.879     199.5        -        -
reference_price              15575     0.800      51.4     4070    11505  (26.1%)
итого: 1287316 вызовов, 13.876 s
```

По фазам хода (без пробы, из того же прогона):

```
    1.92    23.6%      138  recover_enterprises
    1.78    21.9%       12  project_requests
    0.98    12.0%    19211  match
    0.57     7.0%       12  pay_dividends
    0.52     6.4%     2804  input_requests
```

Кто именно дёргает рыночную картину (с пробой, за 12 месяцев):

```
371909  recover_enterprises    landed=300308, book=27049, reference=3949
244953  project_requests       landed=156597, book=30005, demand=5354, quotes=4511
152581  match                  book=53544, landed=43480, quoted_cost=17244
115416  other                  landed=102324, book=4364
 89394  update_prices          landed=84684, customer_quotes=4710
 82143  input_requests         landed=39452, quoted_cost=12630, book=9680
```

Уникальных книг за 12 месяцев — **355** пар «товар × площадка», то есть около
30 в месяц. Каждая строится в среднем дважды за месяц, остальные 95,6%
обращений — попадания в уже готовую книгу.

Разбивка по товарам (суммарно `landed` + `_market_book` + `offers`):

```
   вызовов    секунд     товар
    208024     3.005  tools
    245357     2.409  cloth
    234247     2.214  bread
    108410     1.384  planks
     83407     1.087  flour
    138204     0.978  fish
     99998     0.936  wood
     54466     0.549  flax
```

`tools` первые и по времени, и по вызовам, хотя это промежуточный товар: он
нужен и как оборудование, и как вход. Ниже — то же по `cProfile`, `tottime`:

```
  877195    0.801    1.707  v3.py:1707(landed)
 151088    0.533    1.568  v3.py:1806(_market_book)
   9759    0.918    1.997  v3.py:2240(customer_demand_groups)
  13653    0.645    1.716  v3.py:2202(customer_quotes)
  60065    0.399    1.834  v3.py:1941(quoted_cost)
  16697    0.406    1.742  v3.py:2720(investment_demand)
   445166   0.384    0.428  v3.py:27(__iter__)          # обход OfferView
```

## 3. Вывод по замеру

**Сама книга уже кэшируется и не является узким местом.** 95,6% попаданий,
355 уникальных пар за 12 месяцев, построение — 1,44 s на 12 месяцев с пробой.
Выносить «книгу» в общий снимок не даст ничего: она там уже есть.

Дорогое — **повторный обход книги и повторная агрегация спроса**:

1. `landed` — 877 тыс. вызовов, 84% попаданий, но каждое попадание это
   построение кортежного ключа и словарный поиск в горячем цикле. Считается
   в `recover_enterprises` (300 тыс.), `project_requests` (157 тыс.),
   `update_prices` (85 тыс.), `match` (43 тыс.).
2. `customer_demand_groups` и `customer_quotes` — 20% и 42% попаданий.
   Агрегат пересобирается почти каждый раз, хотя внутри месяца не меняется:
   в ключе стоит `id(orders)` и длина, а `_planning_orders` — новый список
   каждый месяц, зато внутри месяца он один и тот же.
3. `production_outlook` — 482 мкс на вызов при 5 858 вызовах. Внутри
   `reachable_demand` (200 мкс) и `unit_cost`/`net_price`.

То есть общий снимок нужен не для «книги», а для **производных от книги**:
матрицы доставки, агрегата спроса и кривой landed-цен по площадке.

## 4. Схема данных

### 4.1 Только товар × рынок/площадка × месяц (immutable)

| поле | ключ | откуда | размер сейчас |
|---|---|---|---|
| `freight[good][seller_tile][buyer_tile]` | товар, площадка продавца, площадка покупателя | `landed(..., price=0)`, зависит от веса и тарифа контента | 355 книг × офферы; вырождается в `tile × tile × good` |
| `reachable[seller_tile][buyer_tile]` | площадка × площадка | `path()` вернул `None` | `78 × 78` сейчас, `~41 000²` в целевом мире — только разреженно |
| `book[good][buyer_tile]` | товар, площадка покупателя | `_market_book`: список `(landed_price, offer_id)` + `quotes[offer_id]` | в среднем 31 оффер на книгу |
| `demand[good][market_tile]` | товар, площадка производителя | `customer_demand_groups`: `(participant, tile, price, limit, freight, volume)` | десятки строк на пару |
| `public_share[good]`, `input_buyers[good]`, `good_sellers[good]` | товар | `agent_snapshot.build` | 12 значений |
| `recipes`, `weights`, `transport_rate`, `tax` | контент | `content/economy.json` | 12 товаров |

Всё это внутри месяца не меняется: сеть, веса, тариф, состав офферов на момент
`sell_orders`, рецепты. `update_prices` идёт **до** `sell_orders`, поэтому цены
продавцов на момент публикации книги тоже фиксированы.

### 4.2 Что меняется после каждой сделки (mutable)

| поле | почему нельзя в снимок |
|---|---|
| `order.remaining`, `order.status` | сделка списывает остаток той же заявки; `OfferView` читает живые объекты |
| `account.balance`, `reserved`, `protected`, `stock`, `stock_reserved` | деньги и товар переходят между счетами |
| `book.first_live` | курсор «до первого живого оффера» двигается при обходе |
| `_market_quote_version`, `_market_quote_versions`, `_market_quotes_epoch` | растут при каждой сделке и сбрасывают `quoted_cost`/`reference_price` |
| `_demand_revision[good]` | растёт при появлении и отмене заявок |
| состав `sells` | `sell_orders` строит заново каждый месяц |
| новые заявки в `report['orders']` | `input_requests` дописывает входные заявки уже после первого прохода |
| `planned_output`, `production_plan`, `production_observation` | результат планирования, зависит от порядка |

Граница проходит по `order.remaining`: **кривая цен immutable, остаток — нет**.
Поэтому снимок должен хранить `(landed_price, offer_id)` и отдавать остаток
чтением живого объекта, а не собственной копией.

### 4.3 Что нужно фирме для планирования

Личное (уже есть в `agent_*`): `good`, `tile`, `capacity`, `price`,
свободный склад по своему товару и входам, касса после обязательств,
история продаж за 3 месяца, `sales_forecast`, `planned_output`.

Публичное: по каждому входу и по своему товару — кривая `(landed_price,
remaining)` от площадки фирмы, агрегат спроса с пределом платёжеспособности,
`input_buyers` для деления уровня книги, `good_sellers` для доли рынка,
история рынка за 3 месяца.

### 4.4 Что нужно инвестиционному расчёту

То же плюс: `labor[state][province]`, `resource_limits[province][resource]`,
очередь проектов `projects[good]`, `unit_cost` конкурентов (цена + площадка +
рецепт), остаточный спрос `investment_demand`. Инвестиционный обзор уже держит
собственный ledger (`build_investment_state`, `v3.py:467`) — там резервы труда,
ресурсов и входов; это отдельный слой, в снимок он не входит.

## 5. Предлагаемый минимальный интерфейс

Без переписывания движка: объект-обёртка над существующими кэшами, который
строится один раз на фазу и отдаёт те же значения, что методы сейчас.

```python
class MarketSnapshot:
    """Неизменяемая часть рыночной картины одного месяца.

    Строится из sells и meta после update_prices и до первого match.
    Внутри — те же данные, что сейчас лежат в _market_books, _landed_cache
    и _customer_demand_groups_cache, но с явным временем жизни и без записи
    в движок.
    """

    month: int
    goods: tuple[str, ...]
    tiles: tuple[str, ...]           # торговые площадки: cell_id или tile_id

    freight: np.ndarray              # (n_goods, n_tiles, n_tiles) float64
    reachable: np.ndarray            # (n_tiles, n_tiles) uint8
    book_price: np.ndarray           # (n_goods, n_tiles, n_offers) float64
    book_offer: np.ndarray           # (n_goods, n_tiles, n_offers) int32
    book_ptr: np.ndarray             # (n_goods, n_tiles + 1) int32, CSR

    demand_participant: np.ndarray   # (n_goods, n_demand) int32
    demand_tile: np.ndarray
    demand_price: np.ndarray
    demand_limit: np.ndarray
    demand_freight: np.ndarray
    demand_volume: np.ndarray
    demand_ptr: np.ndarray

    public_share: np.ndarray         # (n_goods,) float64
    input_buyers: np.ndarray         # (n_goods,) int32
    good_sellers: np.ndarray         # (n_goods,) int32

    def offer_curve(self, good_index, tile_index): ...
    def demand_rows(self, good_index, tile_index): ...
```

Правила объекта:

- **не хранит остатков.** `remaining` и `status` читаются у живой заявки по
  `book_offer`; снимок даёт только цену и порядок.
- **не хранит денег и счетов.** Платёжеспособность заявки остаётся в
  `order['reserved']`, её нельзя заморозить: `match` меняет резерв.
- **строится один раз** на `(month, sells)` и обнуляется в `reset_report`.
- **не является источником истины для сделок.** `match` продолжает работать с
  живыми объектами; снимок — только для чтения планировщиками.
- **read-only для worker-ов.** Массивы могут уехать в `shared_memory` как есть,
  потому что остаток в них не входит.

Порядок внедрения, если решим:

1. `freight` и `reachable` — матрица по площадкам вместо 877 тыс. вызовов
   `landed`. Это самая дешёвая часть: данные уже считаются один раз на пару
   `(seller, buyer, good)` и не зависят от сделок.
2. `demand` — агрегат `customer_demand_groups` по `(товар, площадка)`, один раз
   на месяц, а не 7 801 раз.
3. `book_price` / `book_offer` — перенос `_market_books` в плоский вид. Делать
   последним: книга уже кэшируется на 95,6%, выигрыш ожидается только от
   скорости обхода (`OfferView.__iter__` — 445 тыс. вызовов, 0.384 s tottime).

## 6. Как воспроизвести замер

```powershell
# базовый прогон, без пробы
.\.venv312\Scripts\python.exe audit\profile_run.py --seed 73129 --months 12 --warmup 2 --detail none --no-cprofile

# тот же прогон со счётчиками рыночной картины
.\.venv312\Scripts\python.exe audit\profile_run.py --seed 73129 --months 12 --warmup 2 --detail none --no-cprofile --market-probe

# только проба, без таймеров фаз
.\.venv312\Scripts\python.exe audit\market_probe.py --seed 73129 --months 12 --warmup 2
```

Проба не меняет расчёт: прогон 10 месяцев с ней и без неё дал одинаковый
отпечаток (счета, предприятия, RNG, сделки) —
`9b52f33a6e6f7c46a6054ad80dcc8a7a96e96584735fcee02be910b6fd47f311`.

## 7. Ограничения замера

- Счётчики добавляют около 20% времени хода; доли между методами сравнимы,
  абсолютные секунды — нет. Доли фаз сняты отдельным прогоном без пробы.
- `offers` и `reachable_demand` не имеют собственного кэша, поэтому попадания
  для них не считаются: это обёртки и чистые расчёты.
- Фаза `other` — вызовы вне обёрнутых фаз (`update_prices` частично,
  `_price_competitors`, `sell_orders`).
- `customer_demand_groups` внутри месяца пересобирается из-за нового
  `id(orders)`; это следствие того, что `_planning_orders` строится заново
  после торгов, а не дефект кэша.
