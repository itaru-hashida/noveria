# Рабочие Cython-участки: update_prices и prepare_recovery_quotes

Дата: 12.11.2026. База переноса recovery: `766e25a`.

## Реализация

В Cython последовательно исполняются все ветви `update_prices()`:

- спрос из `meta.public_orders` и пределы цен покупателей;
- повышение и снижение по прошлым продажам;
- обзор конкурентов с учётом уже обновлённых цен ранних фирм;
- рыночный сигнал с компенсированным суммированием Python 3.12;
- ветвь `price_feedback`;
- ограничения minimum/maximum price.

Сразу после этого в том же изменяемом flat state выполняется
`prepare_recovery_quotes()`: порядок `(depth(good), enterprise.id)`, очистка
старой `production_price`, фильтры suspended/resource, landed-котировки уже
обработанных производителей, inputs, equipment wear, consumption tax, markup и
maximum price. Apply записывает готовые цены и восстанавливает обязательный
`_recovery_quotes`; повторных решений и RNG в apply нет.

Native-вход состоит из плоских массивов. В вычислительных циклах нет engine,
объектов фирм, dict, callback или RNG. Маршрутные расстояния получены исходным
Python `path()` во время pack. Inputs и equipment wear упаковываются отдельными
упорядоченными списками: порядок сложения `cost` соответствует ключам рецепта V3,
а не общему индексу товара. Это необходимо для побитового равенства float64.

Версии и игровые кэши участки не меняют; производные Python-кэши не имитируются
и при последующем обращении перестраиваются старым кодом. В `lagged_batch`
native по-прежнему выполняет только `update_prices`, как и V3.

## Проверка точности

На одинаковых независимых копиях сравнивались Python
`update_prices() + prepare_recovery_quotes()` и `pack → Cython → apply`, затем
полный старый V3 против native-режима. После каждого месяца проверялись accounts,
enterprises, meta, report, `_recovery_quotes`, RNG и `validate()`, включая заявки,
сделки, проекты, склады и резервы.

- 1 месяц, seed `73129`: совпало;
- 12 месяцев, seed `73129`: совпало;
- 12 месяцев, seed `17`: совпало;
- первое расхождение после исправления порядка сложения: отсутствует.

До исправления месяц 10 seed `73129` выявил один ULP у `firm-00172`: native
сложил wood до iron и получил `cost=4.014116`, тогда как порядок рецепта V3 даёт
`cost=4.0141160000000005`. Упорядоченная упаковка устранила причину без допусков
и изменения формул.

Команды повторной проверки:

```bash
python audit/native_update_prices.py --seed 73129 --months 12 --warmup 0
python audit/native_update_prices.py --seed 17 --months 12 --warmup 0
```

## Замер без cProfile

Seed `73129`, `detail=none`, два прогретых месяца, четыре пары ABBA на одном
checkpoint. Создание checkpoint не входит в замер; pack/apply входят. Старая
строка объединяет `update_prices + prepare_recovery_quotes`.

| Участок | Медиана | Диапазон |
|---|---:|---:|
| старые обе фазы | 20,993 мс | 19,718–21,331 мс |
| pack | 7,640 мс | 7,309–8,462 мс |
| Cython call | 0,171 мс | 0,158–0,195 мс |
| apply | 0,479 мс | 0,447–0,519 мс |
| полный V3 step | 456,196 мс | 299,209–467,916 мс |
| полный native step | 370,811 мс | 284,758–462,358 мс |

Разность медиан полного хода: 85,386 мс в пользу native. Диапазоны полного хода
широкие, поэтому это результат именно данного ABBA-замера, а не гарантированная
постоянная экономия. Основная цена границы остаётся pack/apply, а не вычисление.

## Тесты

- native/economy: `157 passed`;
- полный набор: `372 passed, 1 failed`;
- единственный сбой: предсуществующий
  `tests/test_lab.py::test_reproducible_isolated_runs_and_optional_journal`;
  он воспроизводился отдельно и на неизменённом HEAD до этих изменений.

## Сборка

Linux-проверка: Python 3.12.12, Cython 3.3.1, GCC 12.2.0.

```bash
python -m pip install -r requirements-native.txt
cd modules/economy/native
python setup.py build_ext --inplace
```

Импорт и вызовы обеих фаз с typed arrays прошли. Windows/MSVC в доступной среде
отсутствуют, поэтому Windows-сборка не выполнялась и не считается проверенной.
Платформенно специфичные GCC-флаги в setup.py отсутствуют.
