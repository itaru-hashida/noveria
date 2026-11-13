# Пункт 2: числовые проекты, queue keys и решения

База: `edfa524`. Commit этого шага создаётся после проверок.

Внутри одного `native_project_requests` добавлены три числовые структуры:

- `_ProjectTable`: int32 good/state/province/tile и float64 capacity/price для
  живого индекса проектов. При commit таблица обновляется до оценки следующего
  инвестора и напрямую формирует `pj_*` arrays кандидата.
- `_QueueTable`: int32 investor/good/province/created. Невалидные строки
  очереди отбрасываются тем же условием, что Python loop; project dict остаётся
  только payload slot для последующей материализации.
- `_DecisionBuffer`: int32 execution sequence, event code, investor и project
  slot. Candidate/register/resume events записываются в числовом порядке.

Словари проектов, очередь и report rows по-прежнему существуют как payload:
это следующий пункт materialization/output journal. Здесь числовые таблицы уже
являются рабочим состоянием принятия решения; Python apply получает их через
native result и не пересчитывает candidate/queue semantics.

Проверки:

- `tests/test_economy_native_project_batch.py` +
  `tests/test_economy_native_project_numeric.py`: 19 passed;
- `tests/test_economy_native_projects.py` + skeleton + update: 31 passed;
- seed 17, 12 месяцев: exact, один Cython call на месяц, RNG/ledger/validate;
- seed 73129, 12 месяцев: exact, один Cython call на месяц, RNG/ledger/validate.

Известный 1-ULP defect полного native/V3 отчёта не входит в этот шаг.
`project_material_requests` также не менялся.
