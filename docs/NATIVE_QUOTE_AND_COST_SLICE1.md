# Котировки и себестоимость: перенос в Cython и атрибуция исходных расхождений

Дата: 16.11.2026. Ветка: `kimi/step7-native-project-numeric-ledgers`, база `692af54`.
Срез 1 общего переноса экономики в Cython. Экономические правила и алгоритмы
не менялись: перенесена только арифметика, порядок операций сохранён.

## 1. Исходное состояние тестов

Снято на чистом `692af54` до правок, сборка native из `modules/economy/native`:

```text
tests/test_economy*.py — 17 failed, 1270 passed, 41 error
```

41 error — не код: `pytest` не может создать tmpdir, потому что песочница
запрещает symlink (`WinError 1314`). Это окружение проверки, а не дефект
ветки; в среде без ограничения symlink эти тесты проходят.

17 падений распадаются на две независимые причины.

### 1.1. Четыре падения сравнения outlook-кэша

`tests/test_economy_native_update.py`:

```text
test_recovery_underfunded_owner_partial_transfer_reopen_and_cancellation
test_recovery_outlook_cache_reuse_and_offer_cancellation_match_v3
test_recovery_equal_landed_prices_use_python_lexical_order
test_public_project_stock_claims_releases_orders_and_cash_cap_match_v3
```

Все четыре падают на одном месте — нормализованном сравнении
`_production_outlook_cache` после `run_recovery_pair`:

```python
left = {(None,)+key[1:]: value for key, value in reference._production_outlook_cache.items()}
right = {(None,)+key[1:]: value for key, value in native._production_outlook_cache.items()}
assert left == right
```

Причина: два места пишут ключ кэша в разной форме.

* `production_outlook_boundary.cache_key` (используется Python-прогнозом и
  появился в `d5116c9`) кладёт `VERSION = 1` первым элементом:
  `(VERSION, id(sells), e['id'], ...)`.
* `update_prices_boundary.apply_recovery` (появился раньше, в `bca5aec`)
  собирает ключ вручную и без `VERSION`: `(id(sells), e['id'], ...)`.

Тест срезает первый элемент (`[1:]`), рассчитывая, что там `id(sells)`. Для
Python-ветки на этом месте оказывается `id(sells)`, для native — `e['id']`,
поэтому нормализованные ключи не совпадают никогда.

Диагностика (`tmp/probe_outlook_key_shape.py`) на паре движков:

```text
reference len 14 native len 13
 0 DIFF  reference=1                 native=<id(sells)>
 1 DIFF  reference=<id(sells)>       native='firm-00001'
...
```

Это расхождение формата ключа между границей и тестом, а не расхождение
экономического результата: `assert_same_state` (счета, предприятия, meta,
report, RNG) на всех четырёх тестах проходит, значения записей кэша
совпадают. Ошибка по существу — «мёртвая» запись: native кладёт запись под
ключом, который `cache_key` никогда не переиспользует, поэтому кэш recovery
в native-ветке не работает как кэш.

**Важно.** Простая унификация ключа (взять `cache_key` в `apply_recovery`)
делает эти записи живыми и меняет смысл кэша: после такой правки сравнение
значений вскрывает уже содержательное расхождение `expected_price`
(см. §3.4). Поэтому в этом срезе правка не сделана: она относится к срезу
пак-слоя и требует отдельного разбора, иначе перенос смешается с изменением
поведения кэша.

### 1.2. Тринадцать падений дивидендов

`tests/test_economy_owners.py` (13 падений, включая
`test_dividend_limits_and_idle_working_reserve[...]` и
`test_dividends_follow_owner_id_for_every_owner_kind[...]`):

```text
modules/economy/native/dividend_boundary.py:46: in owner_kinds
    self._owner_kinds = module.OWNER_KINDS
E   AttributeError: module 'test_economy_owners' has no attribute 'OWNER_KINDS'
```

`DividendHooks.owner_kinds` брал `OWNER_KINDS` из модуля класса движка. Для
подкласса, объявленного вне `modules/economy/v3.py` (тестовый
`LegacyOwnerEconomy`, лабораторные сценарии), константы в том модуле нет.
Исправлено поиском по MRO с падением обратно на модуль `v3`; коммит `89f5dba`.

### 1.3. Отдельное расхождение Python/native в deep replay

На `692af54` прямого расхождения не воспроизводится. Сравнение
`tmp/probe_replay_digest.py` (state/report/RNG digest плюс число и порядок
событий и сделок) на seed 17 и 73129, `report_detail='deep'`, 6 месяцев:

```text
seed 17     month 1..6 equal
seed 73129  month 1..6 equal
```

Значит, отдельное расхождение либо уже закрыто коммитами после 692af54, либо
проявляется только в сценарии, который эти прогоны не покрывают. Оно не
воспроизведено — и не объявляется исправленным.

## 2. Что перенесено в этом срезе

`modules/economy/native/quote_kernel.pyx` плюс граница
`quote_boundary.py` с эталоном и fallback:

| Функция V3 | Что теперь считает ядро |
| --- | --- |
| `reference_price` | первая живая цена книги либо `market_signal`, затем цена контента |
| `quoted_cost` | обход книги, `value`/`ceiling`, ранний выход по EPS, ориентир recovery |
| `unit_cost` | компенсированные суммы `raw`/`wear` как встроенный `sum` (Neumaier) |
| `volume_input_prices` | средние доставленные цены входов в порядке рецепта |
| `input_bid_ceiling` | прочие затраты плана одним проходом по заявкам |
| `reprice_input_order` | `min(quote, ceiling, budget/remaining)` и доплата |

Осталось в Python и приходит хуками: кэши котировок
(`_reference_price_cache`/`_quoted_cost_cache`/`_unit_cost_cache`),
`_recovery_quotes`, книга заявок, `landed`, склад, касса и мутации заявки и
кассы в `reprice_input_order`. Новых хранилищ нет.

Арифметика намеренно объектная: цена и остаток бывают `int`, и приведение к
`double` изменило бы тип, который виден в `margin` и `expected_profit`.

## 3. Проверки среза

### 3.1. Сборка

```text
cd modules/economy/native && python setup.py build_ext --inplace
quote_boundary.NATIVE_KERNEL is True
```

### 3.2. Focused-тесты

```text
tests/test_economy_native_quote.py — 46 passed
```

Покрыто: значения и типы (включая `int` против `float`), порядок книги и
рецепта, short-circuit (покрытый объём не зовёт `reference_price`),
кэш и его инвалидация, неприкосновенность RNG и живых заявок, fallback без
`.pyd`, отсутствие inline-арифметики в телах V3.

### 3.3. Exact/replay и регрессия

`tmp/snapshot_digests.py` снимает state/report/RNG digest, число и порядок
событий и сделок для seed 17 и 73129 × 3 месяца × standard/deep ×
Python/native. Baseline до переноса и результат после:

```text
compared keys 8 differing []
```

То же для 6 месяцев deep replay: Python и native совпадают на каждом месяце.

Затронутые наборы (`test_economy_native_update`, `_cancel`, `_cancel_margin`,
`_market_book`, `_net_price`, `_production_outlook`, `_production`,
`_update_pack`, `test_economy_recovery`, `test_market_probe`,
`test_economy_native_quote`):

```text
314 passed, 4 failed
```

4 падения — те же четыре падения outlook-кэша из §1.1, без изменений.

Полный набор `tests/test_economy*.py` после среза:

```text
17 failed, 1316 passed, 41 error
```

Список падений совпадает с исходным по составу; 13 падений дивидендов из
§1.2 закрыты (`test_economy_owners.py`: 31 passed), поэтому 13 из 17
исходных падений больше не воспроизводятся, а оставшиеся 4 — те же.
Число passed выросло на 46 — это новые focused-тесты.

### 3.4. Замер

ABBA на одном checkpoint (seed 73129, 2 хода на прогон, 3 пары):

```text
v3     median 1.568 s
native median 1.006 s
```

## 4. Что осталось

* Ключ `_production_outlook_cache` в `apply_recovery` не унифицирован: это
  меняет живость кэша, а не только его форму, и требует отдельного среза
  (§1.1, §4.1).
* Отдельное расхождение deep replay не воспроизведено на 692af54 и не
  объявляется исправленным (§1.3).

### 4.1. Что вскрывает унификация ключа

Если заменить ручной ключ в `apply_recovery` на `cache_key`, сравнение
значений показывает расхождение `expected_price` у товаров с заданным
`production_price`, а также тип `planned_output`:

```text
firm-00005 fish  expected_price 1.3575232143406954 | 1.35
                 planned_output 6 int               | 6.0
firm-00007 bread expected_price 1.9466729811562338 | 1.89
firm-00010 tools expected_price 5.5                | 4.95
```

`unit_cost` и `net_price`, посчитанные отдельно, при этом совпадают, и
запись recovery-кэша согласована со свежим расчётом того же ключа в обеих
ветках (`tmp/probe_outlook_consistency2.py`: inconsistent 0). То есть
расхождение — в том, какой момент месяца запомнила запись, а не в
арифметике. Разбирать это нужно вместе с постоянным состоянием, а не внутри
переноса котировок.
