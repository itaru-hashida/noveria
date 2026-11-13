# Dependency graph: project_candidate (Шаг 4)

База: `899d022`. Все вызовы — только чтение, кроме явно отмеченных.

## Граф зависимостей

```text
project_candidate(investor, sells, derived_demand, demand_region, parent_project_id,
                  diagnostics, allowed_goods)
│
├─ reads: investor account (kind/state/province/tile/stock/balance/reserved/protected)
│   reads: enterprises[investor.id] (expansion)          # чужая мощность инвестора
│   reads: _investment_index['markets'][state]           # пары (pid, good)
│   reads: _investment_index['resource_tiles']           # плитки ресурса
│   reads: _investment_index['labor'] / ['resource_used']
│   reads: _investment_state['labor_reserved'/'resource_reserved']  # mutable между инвесторами!
│
├─ RNG: shuffled(tiles)[0] для resource good (без expansion)
├─ RNG: profile(investor) → optimism, risk_tolerance     # MUTATION account + RNG
│
├─ investment_customer_limit(quote_firm)
│   └─ _customer_quote_index(tile, good, orders) → landed(tile→order.tile)
│       └─ reads report.orders (buy), route_component, distances
│
├─ market_signal(history, state, pid, good)              # чистая, кэш (history, key)
│
├─ investment_demand(quote_firm, sells)
│   ├─ per firm (enterprise scan): unit_cost(firm), net_price(firm) ×2 цены,
│   │   free_cash(firm), cash_after_commitments(owner, project_cash_reserve(owner)),
│   │   population/workforce sum, resource_limits
│   ├─ _investment_supply_cache[good]                    # кэш на обзор
│   ├─ _investment_index['projects'][good]               # MUTABLE между инвесторами!
│   ├─ customer_demand_groups(candidate) → _customer_demand_rows → _customer_quote_index
│   ├─ offers(buyer, good, sells) → _market_book(tile, good)  # книга (landed price, order)
│   ├─ cheap = {order_id: remaining} по sells[good]
│   └─ landed(supply.tile → group.tile, good, price)     # distances
│
├─ unit_cost(quote_firm, sells)
│   └─ reference_price(tile, input, sells) по inputs и equipment_wear
│       └─ offers(tile, good) → _market_book / market_signal fallback
│
├─ net_price(quote_firm)
│   └─ customer_quotes(tile, good) → _customer_quote_index (искл. own, dead)
│
├─ path(market_tile(investor), loc.tile)                 # distances (стабильны)
├─ construction_bill(1) → cfg.construction / minimum_capacity
├─ quoted_cost(investor, g, q, sells) ×N                 # bill + equipment
│   └─ offers walk + reference_price + _recovery_quotes (landed min)
├─ project_finances(...)                                 # чистая арифметика
├─ recipe_requirements(good, capacity)                   # обход recipe_graph children
└─ max(candidates, key=profit*payback - investment)

upstream_expansion_candidate(good, state, sells, shortage, pid, parent)
├─ кэш (good, state, round(shortage,8), pid)             # MUTABLE кэш на обзор
├─ scan enterprises по good → project_candidate(account, derived_demand={good: shortage},
│   demand_region=firm.province, allowed_goods={good})   # вложенные RNG!
└─ max by capacity

sustainable_candidate_capacity — чистая (бинарный поиск + reserve_flow),
  НЕ входит в project_candidate; вызывается project_requests после candidate.
```

## Вывод о разделимости

`project_candidate` отделяется от commit/mutation **без изменения семантики**:

- единственные мутации — `profile(investor)` (два RNG uniform + запись двух полей
  счёта) и чтение mutable ledger (`_investment_state.labor_reserved/resource_reserved`,
  `_investment_index.projects`), которые в native-варианте передаются как вход;
- RNG-вызовы (resource tile shuffle, profile) воспроизводятся тем же
  `rng.shuffle`/`rng.uniform` в том же порядке;
- sells/orders/цены/книги НЕ меняются в течение всего прохода кандидатов —
  всё рыночное можно предвычислить в pack один раз на месяц.

## Что предвычисляется в pack (read-only, без engine-кэшей)

Книги `_market_book` для (tile, good), reference_price, market_signal,
unit_cost/net_price фирм, suppliers для investment_demand, demand rows,
customer limits, project_cash_reserve, labor по (state, province),
resource_limits, локации провинций, resource tiles — всё реплицируется
чистым Python-кодом в `update_prices_boundary.py` без вызовов методов
engine, пишущих кэши.

## Что считает Cython

Сам цикл кандидатов: фильтры markets, capacity limits, RNG (profile, tile
shuffle), quoted_cost walk, unit_cost/net_price по готовым строкам,
investment_demand (groups + offers walk + suppliers), project_finances,
recipe_requirements, выбор max. Компенсированное сложение Python 3.12
(Neumaier) воспроизводится в `_csum` для sum()-мест.
