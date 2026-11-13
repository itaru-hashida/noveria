# Пункт 3: числовой journal и Python apply

База: `2e4852e`.

`native_project_requests` теперь возвращает `_DecisionBuffer` как основной
результат событий. В нём остаются int32 `execution_seq`, event code, investor
index и project slot; payload проекта хранится в slot и не участвует в
расчёте. Старый список `journal` больше не создаётся внутри Cython batch.

`apply_project_requests(engine, result, names)` один раз вызывает
`decision_buffer.materialize(names)` и собирает совместимый Python journal.
После этого apply продолжает материализовать account project, queue, report и
diagnostics из уже рассчитанного result. Он не вызывает candidate/profile/
upstream-expansion/register methods.

Порядок `register_project` сохранён после reservation commit, поэтому trace
видит те же трудовые/resource/flow значения, что Python V3.

Проверки:

- batch tests: 7 passed;
- seed 17, 12 месяцев: exact journal, decisions, reservations, RNG и state;
- seed 73129, 12 месяцев: exact journal, decisions, reservations, RNG и state;
- существующие native project/skeleton/update tests до этого шага: 31 passed.

Известный 1-ULP дефект полного native/V3 отчёта не входит в этот шаг.
`project_material_requests` не менялся.
