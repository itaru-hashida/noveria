# Рабочие Cython-участки: prices, recovery quotes и sell_orders

Дата: 12.11.2026. Последняя база: `eb656a4`.

## Реализация

Один `pack → Cython → apply` последовательно выполняет:

```text
update_prices → prepare_recovery_quotes → sell_orders
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

Обычный `step()` остаётся V3. В `lagged_batch` native по-прежнему выполняет только
`update_prices`, а исходный отдельный `sell_orders(exclude=...)` не меняется.

## Проверка точности

На независимых копиях сравнивались три исходные Python-фазы и общий native boundary,
затем полный старый V3 против native-режима. Сразу после `sell_orders` проверялись:

- ID, порядок и все поля заявок, включая quantity, price, reserved, backing, status;
- `stock_reserved`, `ask_prices`, состав и порядок `sells`;
- `_market_quote_version`, `_market_quote_versions`, `_market_quotes_epoch` и
  удаление производных market/cost/reference caches;
- accounts, enterprises, meta, report, `_recovery_quotes`, RNG и `validate()`.

Полные ходы дополнительно проверяют сделки, проекты, склады и денежные резервы.

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
строка объединяет `update_prices + prepare_recovery_quotes + sell_orders`.

| Участок | Медиана | Диапазон |
|---|---:|---:|
| старые три фазы | 23,742 мс | 23,090–25,353 мс |
| pack | 10,181 мс | 9,570–10,346 мс |
| Cython call | 0,318 мс | 0,311–0,347 мс |
| apply | 2,101 мс | 2,047–2,168 мс |
| полный V3 step | 464,380 мс | 293,518–488,961 мс |
| полный native step | 457,952 мс | 279,803–473,521 мс |

Локальная разность по медианам: 11,142 мс. Полный ход: 6,428 мс в пользу native,
что меньше локальной разницы. Это результат данного ABBA-замера, а не постоянная
гарантированная экономия.

## Тесты

- native/economy: `158 passed`;
- полный набор: `373 passed, 1 failed`;
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
