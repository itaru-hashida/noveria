# Рабочие Cython-участки: prices, recovery, sell и household requests

Дата: 12.11.2026. Последняя база: `4157afc`.

## Реализация

Один `pack → Cython → apply` последовательно выполняет:

```text
update_prices → prepare_recovery_quotes → sell_orders
→ household_requests(basic) → household_requests(extra)
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

Обычный `step()` остаётся V3. В `lagged_batch` native по-прежнему выполняет только
`update_prices`, а исходный отдельный `sell_orders(exclude=...)` не меняется.

## Проверка точности

На независимых копиях сравнивались пять исходных Python-фаз и общий native boundary,
затем полный старый V3 против native-режима. На границе проверялись:

- ID, порядок и все поля заявок, включая quantity, price, reserved, backing, status;
- `stock_reserved`, `ask_prices`, состав и порядок `sells`;
- `_market_quote_version`, `_market_quote_versions`, `_market_quotes_epoch` и
  удаление производных market/cost/reference caches;
- household order, needs, buy quantity/price/reserved и `_demand_revision`;
- accounts, enterprises, meta, report, `_recovery_quotes`, RNG и `validate()`.

Полные ходы дополнительно проверяют сделки, проекты, склады и денежные резервы.
Отдельный маленький deep-режим подтверждает идентичность FoodDiagnostics.

- 1 месяц, seed `73129`: совпало;
- 12 месяцев, seed `73129`: совпало;
- 12 месяцев, seed `17`: совпало;
- первое расхождение после исправлений: отсутствует.

```bash
python audit/native_update_prices.py --seed 73129 --months 12 --warmup 0
python audit/native_update_prices.py --seed 17 --months 12 --warmup 0
```

## Замер без cProfile

Seed `73129`, `detail=none`, два прогретых месяца, четыре пары ABBA на одном
checkpoint. Создание checkpoint не входит в замер; pack/apply входят. Старая
строка объединяет prices, recovery, sell и оба household requests.

| Участок | Медиана | Диапазон |
|---|---:|---:|
| старые пять фаз | 36,887 мс | 35,921–37,790 мс |
| pack | 10,290 мс | 10,127–11,481 мс |
| Cython call | 1,113 мс | 1,088–1,140 мс |
| apply | 4,971 мс | 4,720–5,250 мс |
| полный V3 step | 460,367 мс | 292,186–471,303 мс |
| полный native step | 371,267 мс | 276,900–471,690 мс |

Локальная разность по медианам: 20,513 мс. Парный ABBA показал разность полного
хода 89,101 мс в пользу native; она больше локальной разницы и потому записана
только как результат этого замера, без утверждения об общем ускорении полного хода.

## Тесты

- native/economy: `160 passed`;
- полный набор: `375 passed, 1 failed`;
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
