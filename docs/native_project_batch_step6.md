# Шаг 6: последовательный native project_requests

База: `50ad666` (`origin/kimi/step5-native-project-requests`).
Ветка: `kimi/step6-native-project-batch`, отдельный worktree.

## Граница исполнения

`pack_project_requests → serial_step.native_project_requests → apply_project_requests`.
Старый адаптер с временной подменой `engine.project_candidate` удалён.
В Cython передаются packed investment state, справочники ID, read-only таблицы
правил/предприятий и локальные строки журнала; сам engine и Python callbacks
не передаются. Кандидаты вызываются внутри batch через C-entry `cpdef`.

`project_requests.pxi` содержит последовательный цикл, upstream-кэш,
возобновление/добавление очереди, commit резервов, индекс проектов и расчёт
инвестиционной диагностики. Денежные поля и книги предложений за этот участок
не меняются. Поэтому account arrays, distances, history и order books упакованы
один раз. Labor/resource/input/flow reservations и supply revisions имеют
плотные числовые массивы; проектные supplier arrays обновляются при commit,
а не перепаковываются перед каждым инвестором. Dict проектов, очереди и строк
отчёта остаются payload журнала; диагностические trial-ledger остаются локальными
объектами внутри Cython. Это batch с GIL, без параллельных инвесторов.

Список инвесторов переставляется тем же алгоритмом sorted-by-ID + `rng.shuffle`.
RNG профиля и ресурсной площадки используется внутри batch. Поставщики товара
фиксируются лениво при первом запросе, как Python supply cache: возобновлённая
очередь и уже назначенные проекты владельцев учитываются до проверки средств.
ULP-sensitive умножение и деление construction bill не переставлены.

Python apply материализует проекты/профили, очередь, review metadata,
готовые строки отчёта и upstream diagnostics. Повторных `project_candidate`,
`profile`, `upstream_expansion_candidate` и `register_investment_project` нет.
Временные инвестиционные ledger удаляются, как в V3. Для проверки ledger
сохраняется в `engine.native_project_requests['reservations']`; timings имеют
отдельные `pack`, `native`, `apply`.

`project_material_requests` вызывается после apply существующим Python-методом.
Его перенос, 36-месячные проверки и ABBA относятся к следующему этапу.

## Проверки

Сборка: Python 3.12.14, Cython 3.3.1, GCC, без fast-math:

```bash
cd modules/economy/native
CC=gcc python setup.py build_ext --inplace
```

Контрактные тесты проверяют один pack/call/apply, read-only pack и отсутствие
Python callbacks (включая monkeypatch публичного evaluate entry). Отдельно
проверены full/targeted/skipped review, очередь с invalid/duplicate/resumed/waiting
элементами, успешный/неуспешный upstream, повторное использование upstream-кэша
и диагностические поля.

```bash
PYTHONPATH=. python -m pytest -q tests/test_economy_native_project_batch.py \
  tests/test_economy_native_projects.py tests/test_economy_native_skeleton.py \
  tests/test_economy_native_update.py
```

`work/project_batch_compare.py` выполняет replay на входе project_requests
каждого реального месяца. Сравниваются кандидаты и RNG before/after по
`execution_seq`, регистрация и резервы после каждого commit, конечный ledger
(включая flow balances), accounts/stock/stock_reserved/projects, enterprises,
очередь/meta, весь report и offers/orders. Оба движка проходят validate().
Journal execution_seq нумерует candidate и register_project в исходном порядке.
Числовой компаратор допускает только точное равенство int/float представлений;
1 ULP, большие неравные int и bool/int остаются различиями. Digest состояния
не используется как критерий равенства. Ошибка содержит month, investor,
execution_seq, event, field, V3/native values и RNG digests.

```bash
PYTHONPATH=. python work/project_batch_compare.py --seed 17 --months 12
PYTHONPATH=. python work/project_batch_compare.py --seed 73129 --months 12
# Полный native pipeline против прежнего project_requests из 50ad666:
PYTHONPATH=. python work/project_batch_compare.py --seed 17 --months 12 --against-step5
PYTHONPATH=. python work/project_batch_compare.py --seed 73129 --months 12 --against-step5
# Дополнительная, более широкая проверка native pipeline против чистого V3:
PYTHONPATH=. python work/project_batch_compare.py --seed 17 --months 12 --full-step
```

Результаты:

| Проверка | Результат |
|---|---|
| Новый batch, seed 17, 1 и 12 месяцев | exact, один entry call в каждом месяце, RNG/validate совпали |
| Новый batch, seed 73129, 1 и 12 месяцев | exact, один entry call в каждом месяце, RNG/validate совпали |
| Полные native ходы против `50ad666`, seed 17, 12 месяцев | exact accounts/enterprises/meta/report/RNG, validate |
| Полные native ходы против `50ad666`, seed 73129, 12 месяцев | exact accounts/enterprises/meta/report/RNG, validate |
| Native project + skeleton + update + новые batch тесты | 38 passed, 299.94 s |
| Новые контрактные тесты отдельно, включая read-only pack | 7 passed |
| Полный pytest (запущен до добавления последних четырёх новых тестов) | 400 passed, 2 failed, 524.31 s |

Сбои полного pytest:

- `tests/test_economy_owners.py::test_owner_moves_cell_and_buys_from_current_location[cell_id]`:
  пустой trades. При изолированном повторе тест проходит; весь owners-модуль
  отдельно дал 34 passed и на этой ветке, и на чистой базе `50ad666`.
- `tests/test_lab.py::test_reproducible_isolated_runs_and_optional_journal`:
  enterprises двух full/summary checkpoint различаются. Тот же тест падает
  на отдельном чистом worktree `50ad666` без сборки нового native batch.

Эти проверки не объявляются зелёными и не исправлялись изменениями другого
участка экономики.

## Ограничение полного native pipeline

Полный `native_mode='cython'` против V3 уже в месяце 1 имеет числовое расхождение
в `report.provinces[*].prices`, не связанное с новым batch:

| Seed | Поле | V3 | Native |
|---|---|---|---|
| 17 | `provinces[0].prices.tools` | 5.505179997513106 | 5.505179997513107 |
| 73129 | `provinces[6].prices.planks` | 2.2999999999999994 | 2.3 |

Accounts, enterprises, meta и RNG к этому месту совпадают канонически точно.
Для seed 17 тот же первый differing field и значения воспроизведены с адаптером
`run_native_project_requests` из `50ad666`. Это именно числовое расхождение,
его нельзя скрывать нормализацией integer/float или tolerance. Исправления
других native фаз и сводного отчёта в этот шаг не включены. Поэтому общий
критерий exact полного native pipeline против V3 пока не достигнут.
