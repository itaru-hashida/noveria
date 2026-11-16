# Эталонная матрица native/Python parity

`native_parity_baseline_5887c10.json` снят на ревизии
`5887c1059471fd70e839ed172212a18458af0821` **до** переноса блоков 1–3.
С ним сверяется `scripts/verify_native_parity.py --compare-baseline` и
`tests/test_native_parity_baseline.py`.

Матрица: seed `17` и `73129` × 3 месяца × `standard`/`deep` × Python (`off`) и
native (`cython`). На каждый месяц записаны digest'ы `state`, `report`, `rng`,
число и порядок сделок и событий.

SHA-256 канонического содержимого (JSON с сортированными ключами, без
пробелов, UTF-8):
`24d5950a631f5ace4d2c5d310033dee6c99ea4dad7ef5032b88cfc2f054e0a25`.

Хеш берётся от содержимого, а не от байтов файла: Git отдаёт LF, а рабочая
копия на Windows может быть CRLF, поэтому байтовый хеш зависел бы от платформы
и настроек checkout.

## Процедура воспроизведения

Из корня репозитория на ревизии, где скрипт уже существует:

```bash
git worktree add -f ../noveria-baseline 5887c10
cp scripts/verify_native_parity.py ../noveria-baseline/scripts/
cd ../noveria-baseline/modules/economy/native
python setup.py build_ext --inplace
cd ../../..
python scripts/verify_native_parity.py --save ../baseline.json
```

Скрипт копируется в worktree, потому что на `5887c10` его ещё нет: он добавлен
позже и от ревизии не зависит (только прогоняет шаги и считает digest'ы).

Проверено: процедура даёт ту же матрицу, что и закоммиченный снимок
(канонический хеш совпадает). На Windows `build_ext` может упереться в
недоступный `%TEMP%`; помогает `$env:TMP`/`$env:TEMP` внутри рабочего каталога.

## Зачем отдельный снимок

Расхождение native и Python **внутри одной ревизии** — свойство базовой линии
`5887c10`, а не следствие переноса:

* расходятся только типы (`int` против `float`) в
  `accounts[...].project.input_evidence.*`, значения равны;
* `rng`, число и порядок сделок и событий в обеих ветках совпадают;
* числовых расхождений — 0 для обоих seed.

Поэтому перенос проверяется по неизменности траекторий относительно снимка, а
не по совпадению двух веток между собой.
