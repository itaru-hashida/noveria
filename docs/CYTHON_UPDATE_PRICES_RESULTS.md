# Рабочие Cython-участки: от prices до public_project_requests

Дата: 12.11.2026. Предыдущий опубликованный boundary: `bca5aec`.

## Реализация

Один `pack → Cython → apply` последовательно выполняет:

```text
update_prices → prepare_recovery_quotes → sell_orders
→ household_requests(basic) → household_requests(extra)
→ owner_requests → recover_enterprises → public_project_requests
```

Все фазы используют одно изменяемое flat state. В вычислительных циклах нет
engine, объектов фирм, dict или callback. Маршрутные расстояния получены исходным
Python `path()` во время pack. Inputs и equipment wear упаковываются в исходном
порядке ключей рецепта, чтобы сохранить побитовое сложение float64.

`prepare_recovery_quotes()` сохраняет порядок `(depth(good), enterprise.id)`,
очистку `production_price`, фильтры suspended/resource, landed-котировки уже
обработанных производителей, inputs, equipment wear, tax/markup и maximum price.
Apply восстанавливает обязательный `_recovery_quotes`.

`sell_orders()` перенесён целиком:

- полная инвалидация market books находится перед RNG;
- исходный `random.Random.shuffle` вызывается один раз с GIL над канонически
  отсортированными числовыми account index; дальше цикл работает без GIL;
- household и suspended пропускаются, товары обходятся в `sorted(stock)`;
- free stock вычитает stock reserved и private/public locked materials;
- input buffer и equipment reserve используют исходные planned/recent/capacity;
- складские ask prices видят результат уже выполненного `update_prices`;
- native создаёт готовые quantity/price/order index/execution sequence и сразу
  меняет flat `stock_reserved`;
- apply материализует реальные заявки и `sells[good]` строго по журналу, без
  повторного `order()` или пересчёта quantity, price, reserve и порядка;
- начальная и каждая последующая точечная инвалидация котировок сохраняются.

`household_requests()` использует второй исходный `shuffle` над household index.
Native сначала выполняет базовые заявки всех households, затем дополнительные всех
households. Перенесены offers по `(landed_price, order ID)`, quoted cost ceiling,
market/recovery fallback, стабильная сортировка basic budget revision, общий бюджет
extra, needs, денежные резервы, buy ID и demand revisions. Python sum заменён
эквивалентным компенсированным накоплением. Для deep report FoodDiagnostics
создаётся в apply строго между sell и household events.

`owner_requests()` использует третий исходный `shuffle`, сохраняет project reserve,
owner budget, basket order и food cap. Созданные заявки сразу входят в общий native
demand state. `recover_enterprises()` выполняет четвёртый `shuffle` по отсортированным
ID и в том же state рассчитывает outlook, reachable demand, stock sales, required cash,
transfers, reopen, cancellations и operating reserve. Apply материализует готовые events
и восстанавливает `_stock_sales_cache` и `_production_outlook_cache` из journal, включая
snapshot quote versions; повторный `production_outlook()` в apply не вызывается.

`public_project_requests()` выполняется в том же state после recovery. Native последовательно
закрепляет собственные materials, equipment и support stock, при необходимости сокращает
quantity/remaining/backing существующих sell offers и уменьшает `stock_reserved`. Он сразу
обновляет market versions, отменяет исчерпанные offers, ограничивает покупки доступными
деньгами, увеличивает денежный reserve и добавляет готовые buy orders с `project_id` в общий
demand state. Apply только материализует bucket values, release/cancel events и заявки по
журналу; quantity, price, reserve и порядок повторно не вычисляются.

Offers упорядочиваются по `(landed_price, order ID)`. Decimal suffix сравнивается
лексикографически по цифрам, включая `2 < 268` и `11 < 9`; числовая сортировка здесь
недопустима. История продаж и Python 3.12 `sum()` воспроизводятся компенсированным
сложением в исходном порядке.

Обычный `step()` остаётся V3. В `lagged_batch` native по-прежнему выполняет только
`update_prices`, а исходный отдельный `sell_orders(exclude=...)` не меняется.

## Проверка точности

На независимых копиях сравнивались восемь исходных Python-фаз и общий native boundary,
затем полный старый V3 против native-режима. На границе проверялись:

- ID, порядок и все поля заявок, включая quantity, price, reserved, backing, status;
- `stock_reserved`, `ask_prices`, состав и порядок `sells`;
- `_market_quote_version`, `_market_quote_versions`, `_market_quotes_epoch` и
  удаление производных market/cost/reference caches;
- household order, needs, buy quantity/price/reserved и `_demand_revision`;
- owner budgets/reserves, recovery transfers, lifecycle, cancellations и operating holds;
- public stock claims, сокращение и отмена sell offers, buy orders, project IDs и cash cap;
- точные `_stock_sales_cache` и `_production_outlook_cache`, включая ключи и quote versions;
- accounts, enterprises, meta, report, `_recovery_quotes`, RNG и `validate()`.

Полные ходы дополнительно проверяют сделки, проекты, склады и денежные резервы.
Отдельный маленький deep-режим подтверждает идентичность FoodDiagnostics.

- 1 месяц, seed `73129`: совпало;
- 12 месяцев, seed `73129`: совпало;
- 12 месяцев, seed `17`: совпало;
- первое расхождение после исправлений: отсутствует.

```bash
PYTHONPATH=. python audit/native_update_prices.py --seed 73129 --months 12
PYTHONPATH=. python audit/native_update_prices.py --seed 17 --months 12
```

## Замер без cProfile

Seed `73129`, `detail=none`, два прогретых месяца, четыре пары ABBA на одном
checkpoint. Создание checkpoint не входит в замер; pack/apply входят. Старая
строка объединяет prices, recovery quotes, sell, оба household requests, owner requests,
начальный recover_enterprises и public_project_requests. Повторные точечные recovery из
plan_enterprises не относятся к измеряемому boundary.

| Участок | Медиана | Диапазон |
|---|---:|---:|
| старые восемь фаз | 81,993 мс | 80,675–263,049 мс |
| pack | 11,654 мс | 11,474–11,877 мс |
| Cython call | 4,004 мс | 3,931–4,281 мс |
| apply | 6,268 мс | 5,998–7,063 мс |
| полный V3 step | 468,899 мс | 286,800–480,955 мс |
| полный native step | 361,488 мс | 262,605–444,284 мс |

Локальная разность по медианам: 60,067 мс (21,926 мс native boundary вместо
81,993 мс). Разность полного хода 107,411 мс больше локальной экономии и потому
записана только как результат замера, без утверждения об общем ускорении полного хода.

## Тесты

Все тесты запускались параллельно через `PYTHONPATH=. pytest -n auto -q`.

- focused native boundary: `10 passed`;
- полный набор: `379 passed, 1 failed`;
- единственный сбой: предсуществующий
  `tests/test_lab.py::test_reproducible_isolated_runs_and_optional_journal`;
  он воспроизводился отдельно и на неизменённом HEAD до native-изменений.

## Сборка

Linux-проверка: Python 3.12.12, Cython 3.3.1, GCC 12.2.0.

```bash
python -m pip install -r requirements-native.txt
cd modules/economy/native
python setup.py build_ext --inplace
```

Windows/MSVC в доступной среде отсутствуют, поэтому Windows-сборка не выполнялась
и не считается проверенной. Платформенно специфичные GCC-флаги отсутствуют.
