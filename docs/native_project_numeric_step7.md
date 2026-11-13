# Числовые промежуточные расчёты project_requests

База: `311b380`. Ветка: `kimi/step7-native-project-numeric-ledgers`.
Выполнен пункт 1 согласованного списка; очередь, назначения проектов и
выходные журналы (пункты 2–3) не переводились в числовой формат.

## Изменения

- Flow trial: `_FlowLedger` и `_ProjectFlowKernel` используют float64 buffers
  base/trial/best, uint8 presence mask и int32 порядок записей. В 40 итерациях
  поиска нет создания/копирования словарей. Неуспешный trial целиком
  сбрасывается из base; предыдущие принятые резервы сохраняются.
- Порядок рецептов для headroom/reserve — лексикографический, как sorted в V3.
  Вычитания capacity − committed − reserved, умножения ratio и сложения
  резервов выполняются в исходном порядке. Сохраняются нулевые записи,
  порядок первого резервирования и путь ошибки при цикле.
- Upstream-кэш: массивы good/state/province/rounded_shortage и owner index.
  Попадания, включая отрицательные результаты, не запускают оценку повторно.
  Сохраняются Python round(shortage, 8), первый максимум при равенстве
  мощностей и исходный parent_project_id кэшированного проекта. Ёмкость —
  число счетов: в одном обзоре не более одного поиска на инвестора.
- Накопители recipe requirements, affected goods, свободного flow и
  expansion deficits также числовые. У ленивого flow balance cache —
  числовой индекс; у per-account dynamic cache — индексируемая таблица.
- Временные available_inputs — float64 array. Объём предложений суммируется
  Python sum по float в прежнем порядке, чтобы сохранить семантику Python 3.12.
- Добавлена поддержка deepcopy рабочих ledger/cache, сохраняющая ссылки
  payload через memo. Диагностический snapshot flow balances материализуется
  в прежний формат только при экспорте.

Остаются неизменяемые справочники ID и правил, именованные контейнеры packed
arrays, а также payload словарей проектов/очереди/строк отчёта. Они не выдаются
за полностью числовое ядро. В частности, кэш upstream хранит числовые ключи
и индексы владельцев, но сами проекты и диагностика остаются ссылками на
payload будущего выходного журнала. Их перенос — отдельные пункты 2–3.

## Проверки

Сборка: Python 3.12, Cython 3.3.1, GCC -O3 без fast-math.

```bash
cd modules/economy/native
CC=gcc python setup.py build_ext --inplace --force
```

```bash
PYTHONPATH=. python -m pytest -q tests/test_economy_native_project_batch.py \
  tests/test_economy_native_project_numeric.py
PYTHONPATH=. python work/project_batch_compare.py --seed 17 --months 12
PYTHONPATH=. python work/project_batch_compare.py --seed 73129 --months 12
PYTHONPATH=. python -m pytest -q
```

Адресные проверки: 19 passed. Они включают общие предки в рецептах,
rollback частичного trial, повторный solve с собственным best в качестве
входа, explicit zero reservations, EPS-границы, ленивый порядок чтения
балансов, разные государства, 30 детерминированных DAG, глубокий рецепт,
циклы и неиспользуемые циклы, агрегацию повторных родителей, отрицательный
upstream cache, округление ключей и сохранение идентичности payload.

Итоги финальной сборки -O3:

- Seed 17: месяцы 1–12 — exact кандидаты, execution_seq, commit/final ledger,
  accounts, enterprises, очередь, offers/orders, report и RNG; validate пройден.
- Seed 73129: месяцы 1–12 — те же exact-проверки пройдены.
- В каждом месяце replay подтвердил один внешний Cython entry call.
- Полный pytest: **417 passed, 1 failed**, 357.64 s. Единственный сбой —
  `tests/test_lab.py::test_reproducible_isolated_runs_and_optional_journal`
  (сравнение enterprises в full/summary checkpoint). Он уже воспроизведён
  на чистой базе `50ad666`, см. отчёт шага 6. Новых падающих тестов нет.
- `git diff --check` — без ошибок.

Все 50 native-тестов входят в прошедшую часть полного набора. Изменения
проверены без tolerance/округления сравниваемых float; integer/float
представления допускаются только при точном числовом равенстве.

Известное расхождение полного native/V3 в prices на 1 ULP, зафиксированное
в [отчёте шага 6](native_project_batch_step6.md), в этом изменении не исправлялось.
Перенос project_material_requests и ABBA не выполнялись.
