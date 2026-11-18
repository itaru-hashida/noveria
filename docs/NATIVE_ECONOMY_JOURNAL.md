# Журнал перехода native-ядра экономики

## 2026-11-18 — зафиксирована цель

Текущее ядро считается гибридным: Cython + Python. Полный rewrite на C не
является целью. Cython используется как native-слой и как путь к будущей
замене отдельных стабильных модулей на C.

Зафиксированные проблемы:

- Python callbacks и dictionary-heavy lookup в массовых горячих циклах;
- отсутствие одного явного владельца mutable/durable state;
- смешение чистого расчёта и применения результата;
- передача `engine`, Python dictionaries и helpers внутрь native-кода;
- слишком мелкие переходы Python↔Cython, стоимость которых может съесть выигрыш.

Целевой pipeline:

```text
Python prepare snapshot
→ Cython calculate
→ immutable result
→ Python apply через owner
```

## 2026-11-18 — исходный аудит planning

Проверено: `modules/economy/native/plan_kernel.pyx` получает `engine`,
`enterprise`, `sells` и вызывает `engine.goods`, `engine.cfg`, `engine.meta`,
`engine.depth`, `engine.free_cash`, `engine.recover_enterprises` и
`engine.input_requests`. `input_requests` также использует Python hooks для
`reference_price`, `stock`, `quote`, `plan` и `volume_prices`.

Вывод: текущий модуль является Cython-ускорителем поверх Python, а не строгим
native-контуром. Это baseline, а не повод менять поведение целиком.

## Следующий срез

Первый вертикальный срез: `planning → production outlook → input orders`.

Порядок работ:

1. Подготовить snapshot входов в Python boundary.
2. Убрать `engine` из аргументов расчётной функции Cython.
3. Вернуть явный result без изменения мира из глубины расчёта.
4. Оставить применение cash/orders через прежних владельцев.
5. Затем заменить callbacks `stock` и `quote` подготовленными данными.
6. Проверить parity, RNG, recovery, exception safety и focused-тесты.

Каждый шаг должен быть малым, обратимым и сопровождаться benchmark полного
pipeline. Не принимать изменение только по скорости отдельной функции.

## 2026-11-18 — первый шаг выполнен

В `plan_boundary.input_requests` добавлен явный snapshot с месяцем, полями
прогноза, рецептом, входами, оборудованием, wage и overhead. Сигнатура
`plan_kernel.input_requests` теперь принимает `snapshot, hooks, eps`, а не
`engine, enterprise, sells`. Расчётная часть больше не читает `engine.goods`,
`engine.cfg` и `engine.meta`.

Оставшиеся временные зависимости: Python hooks для `plan`, `stock`, `quote`,
`volume_prices` и операций применения. Это следующий срез, а не завершение
границы.

Проверка окружения: `py_compile` для Python boundary прошёл. Пересборка Cython
и focused pytest пока недоступны в окружении: отсутствуют пакеты `Cython` и
команда `pytest`. После установки зависимостей обязательны сборка расширения и
focused parity-тесты.

## 2026-11-18 — stock callback вынесен из горячего цикла

Snapshot расширен `stock_values`. Остатки по входам и оборудованию читаются один
раз на boundary, после чего `cash_needed`, бисекция и создание input orders
используют snapshot вместо `hooks.stock(...)` на каждой итерации. Порядок товаров
сохранён: сначала входы в порядке рецепта, затем оборудование, которого ещё не
было среди входов.

Оставшийся callback, который нужно убрать следующим: `hooks.quote(good,
quantity)`. Он зависит от quantity и требует нативного контракта подготовленной
книги/контекста котировки, а не простого snapshot. До этого момента горячий цикл
нельзя считать callback-free.

## 2026-11-18 — quote callback заменён подготовленным контекстом

Для каждого входа и оборудования snapshot теперь содержит quote context,
подготовленный через `quote_boundary.prepare_quote_context`. `plan_kernel`
вызывает native `quote_kernel.quoted_cost` по этому контексту для разных
объёмов, поэтому `hooks.quote(good, quantity)` больше не вызывается в
`cash_needed`, бисекции и создании заявок.

Внутри quote kernel пока остаются редкие boundary hooks для reference price и
landed recovery quote при непокрытой книге. Это следующий уровень очистки:
сначала нужно сделать recovery/reference частью подготовленного числового
snapshot, сохранив порядок и cache invalidation.

## 2026-11-18 — reference/landed подготовлены заранее

Добавлен `prepare_native_quote_context`: reference price и landed recovery
prices вычисляются один раз на boundary, а native quote kernel получает только
`offers`, числовой reference и список recovery prices. В planning больше нет
callback к `reference_price` или `landed` во время бисекции и формирования
заявок. Для Python fallback добавлен эквивалент того же контракта.

Открытый риск для обязательной parity-проверки: reference теперь вычисляется
при подготовке контекста даже если конкретная книга покрывает объём. Нужно
проверить, не меняет ли это cache invalidation или ленивую market signal-ветку.

## 2026-11-18 — plan и volume prices вынесены из native-цикла

Boundary теперь выполняет подготовительные операции в прежнем порядке:
`reset_protected → plan → free_cash → begin_orders`, затем вызывает native
`calculate_target`. После target boundary один раз рассчитывает `input_prices`
по snapshot и передаёт их в `input_requests`.

`plan_kernel.input_requests` больше не вызывает `hooks.plan()`,
`hooks.free_cash()`, `hooks.volume_prices()`, `hooks.reset_protected()` или
`hooks.begin_orders()`. Внутри него остались только result construction и
применение через hooks: поля плана, payroll и input orders.

Следующая задача — убрать и эти mutation hooks через явный immutable result и
отдельный apply phase. До этого нельзя считать ownership полностью закрытым.

## 2026-11-18 — calculate/apply разделены

`plan_kernel.input_requests` теперь принимает только snapshot и `eps`, возвращая
пару `(plan, order_specs)`. Он не вызывает cash/order/enterprise mutation
hooks и не меняет Python-мир. Boundary-функция `apply_input_request_result`
применяет результат через существующие владельческие операции в прежнем
порядке, назначает adaptive-флаг и после создания заявок записывает их IDs.

Таким образом, первый planning-срез получил явный pipeline:

```text
prepare snapshot → calculate target/result → apply через owners
```

Осталось подтвердить Cython-синтаксис, parity и exception safety в окружении с
зависимостями. До этой проверки изменение считается структурно завершённым,
но не принятым по поведению.

## Перепроверка planning-среза

Расширения пересобраны с GCC через проектную `.venv`. Добавлены декларации
Cython для прямых вызовов между расширениями. Поля прогноза снимаются после
`plan()`, причина ограничения уточняется после применения лимита, а резервная
цена считается как `max(reference, min(recovery_prices))`. Котировочный
снимок содержит пары цены и остатка без ссылок на живые заявки.

Прошёл 171 focused-тест, включая сравнение с сохранённым baseline. Два новых
случая для порога резервной цены прошли отдельным запуском. Замеры скорости
этого среза ещё не проводились.

Бисекция больше не вызывает прикладные Python-коллбеки. Контракт сохраняет
объектную арифметику. Внешний обход и запись остаются в действующих
компонентах. Подготовка резервных котировок теперь выполняется заранее,
поэтому точный порядок заполнения кэшей требует отдельной проверки.
Подготовка снимает старый резерв и записывает прогноз до расчёта, так что
отсутствие мутаций внутри расчёта само по себе не гарантирует откат операции.

## 2026-11-18 — проверка цели и устранение дублирования

Первый 13-ходовой benchmark на seed `73129`, detail `standard`, показал
регрессию planning после введения native quote context. Причина была в двойной
сборке книги по входам: старый прогрев reference и новая подготовка context.
Прогрев удалён. Reference теперь берётся из уже подготовленной первой цены,
а market lookup выполняется только для пустой книги.

Повторный benchmark после исправления:

```text
off:    total 5.7111 s, warm median 0.4302 s, planning 0.5217 s
cython: total 5.7570 s, warm median 0.4299 s, planning 2.1799 s
```

Полный ход не ухудшился более чем на 5%, но ускорения planning не подтверждено.
Разница planning между режимами также зависит от native monthly driver и
счётчиков фаз, поэтому следующий замер нужно выполнить отдельным режимным
профилем с одинаковой оркестрацией фаз. Срез не объявляется performance-целью
до этого измерения.

## Реестр дефектов planning

| Дефект | Статус |
|---|---|
| Прогноз читался до `plan()` | Исправлено. Снимок читает обновлённые поля. |
| В плане оставалась старая причина ограничения | Исправлено. Причина обновляется после применения лимита. |
| Резервная цена могла оказаться ниже ориентира | Исправлено. Два регрессионных случая прошли. |
| Отсутствовали декларации связей Cython | Исправлено. Сборка GCC прошла. |
| Сбой расчёта оставлял новый прогноз и снятый резерв | Исправлено до начала применения. Тест с внедрённым сбоем подтверждает восстановление предприятия, резерва и списка заявок. |
| Покрытая книга всё равно запускала подготовку резервных цен | Исправлено. Регрессионный тест проверяет покрытую закупку оборудования. Для непокрытой книги момент заполнения кэша всё ещё может отличаться. |
| Сбой во время применения может оставить частично записанные заявки и резервы | Исправлено для planning-среза. Созданные заявки отменяются и удаляются, cash/order revisions и состояние предприятия восстанавливаются. Добавлен регрессионный тест. |

Следующие задачи по порядку:

1. Проверить порядок чтений и инвалидацию на непокрытой книге, включая recovery между предприятиями.
2. Снять A/B полного хода и planning по прежней методике, включая холодный и прогретые ходы.
3. Перенести тот же transaction/apply-контракт на settlement и market/ledger.

## Критерии, по которым объявляем цели достигнутыми

Planning закрыт после parity на непокрытой книге и recovery, проверки cache
invalidation, injected-failure rollback, Cython-сборки, focused-тестов и A/B
замера 13 ходов. Полный перенос экономики закрыт после повторения этих условий
для settlement и market/ledger.

Текущий статус целей: структурный planning-контур выполнен, а окончательная
приёмка ждёт проверки непокрытой книги, замера полного хода и подтверждения
rollback во всех владельцах. Точный срок зависит от результатов тестов. По
текущему объёму это три последовательных контрольных этапа, после которых
можно считать первый срез завершённым.

## 2026-11-18 — контрольный прогон

Добавлены критерии завершения в roadmap. Повторный focused-набор прошёл без
ошибок: `176 passed in 57.61s`. Проверены planning, production, production
outlook, quote, demand, project batch и parity baseline. `git diff --check`
чистый.

До достижения planning-gate осталось проверить непокрытую книгу с recovery и
инвалидацией после изменения рынка, выполнить A/B benchmark полного хода и
зафиксировать результат. После этого можно переходить к settlement и
market/ledger.
## 2026-11-18 — профиль причины замедления

Профиль Cython на трёх ходах показал основные расходы в `landed`, route path
и `_market_book`. Наблюдалось около 196 тысяч вызовов `landed`, 0.538 секунды
собственного времени, около 0.599 секунды в `_market_book` и 0.476 секунды в
route path. Дробление оставшихся planning callbacks не является следующим
performance-срезом. Для ускорения потребуется единый native-контракт доставки
и маршрута.

После устранения дублирования focused-набор снова прошёл: `176 passed in
54.87s`. `git diff --check` чистый.

Planning-gate частично закрыт. Parity и rollback закрыты, обычные quote cache
paths покрыты тестами, полный 13-ходовой A/B показал ухудшение полного хода на
0.8% и отсутствие ускорения planning. Performance-цель пока не закрыта.

## 2026-11-18 — recovery quote cache

Профиль показал, что подготовка одинаковых recovery quotes повторно вызывала
`landed` для разных предприятий. Добавлен cache подготовленного recovery-среза
с ключом buyer tile, good, identity списка recovery rows и market quote
version. Кэш не становится владельцем состояния и сбрасывается сменой identity
списка или версии рынка.

Новый 13-ходовой замер на `73129`, `standard`:

```text
off:    total 6.0066 s, warm median 0.4595 s
cython: total 5.8959 s, warm median 0.4348 s
```

Cython быстрее полного прогона на 1.8%, прогретого хода на 5.4%. Focused-набор
прошёл: `176 passed in 55.63s`. Это первый срез, который даёт измеримое
ускорение без нарушения parity. Требование 15% для planning пока не достигнуто.

## 2026-11-18 — route path audit

Повторный профиль после recovery quote cache показал за три хода 78 построений
деревьев Dijkstra, около 0.166 секунды в native `route_boundary` и около 0.504
секунды накопительно в `support.path`. Основной расход остаётся шире route
kernel: около 196 тысяч вызовов `landed` и 24 тысячи построений `_market_book`.

Дополнительный точечный перенос route path сейчас не принимается как отдельный
срез. Деревья уже кэшируются владельцем маршрутов, а изменение их lifetime
затронет route invalidation и parity. Для следующего ускорения нужен batch
контракт рыночной книги, который выдаёт несколько landed values за один
boundary call и сохраняет текущего owner кэша.

## 2026-11-18 — batch market-book contract

В `market_book_boundary` добавлен узкий batch-контракт для малых книг: один
`landed` вызывается на уникальную площадку продавца, после чего Cython
собирает и сортирует options по готовым числовым freight-значениям. Большая
книга и старый fallback сохранены; порядок продавцов, сортировка и
unreachable semantics не менялись. Вызов из `v3.py` переведён на этот
контракт.

Проверки: `130 passed` в focused-наборе market book, plan, quote и parity.
Замер 13 ходов на seed `73129`, `standard`:

```text
off:    total 5.8940 s, warm median 0.4411 s
cython: total 5.7673 s, warm median 0.4275 s
```

Cython быстрее полного прогона примерно на 2.2%, прогретого хода примерно
на 3.1%. Цель planning `-15%` этим срезом не достигнута, поэтому это не
финальная performance-приёмка. Структурно batch-контракт оставлен как
положительный узкий срез; дальнейший выигрыш потребует более широкого native
контракта маршрута и доставки, а не дробления отдельных callback-ов.

## 2026-11-18 — отделение orchestration от native plan kernel

Убран `plan_enterprises` из `plan_kernel.pyx`. Он принимал `engine` и вызывал
`depth`, `free_cash`, `recover_enterprises` и `input_requests`, то есть был
оркестратором, а не расчётным ядром. Теперь перемешивание, порядок
предприятий, recovery и переход к следующему предприятию выполняются только
в `plan_boundary`; Cython получает snapshot одного предприятия через
`input_requests` и возвращает расчётный результат.

Проверки: Cython успешно сгенерировал `plan_kernel.pyx`, focused plan/parity
набор прошёл `26 passed`, `git diff --check` чистый. Устранена одна граница;
production outlook hooks, cache ownership и market-book legacy fallback ещё
остаются следующими архитектурными задачами.

## 2026-11-18 — закрытие границ production outlook и market book

Production outlook теперь сначала собирает полный `snapshot` всех чтений
движка (`demand`, stock, trades, cost, overhead, revisions и параметры
снижения), после чего native calculation вызывается отдельной функцией
`_compute_snapshot`. Hooks больше не перемешаны с вычислительной частью.

Market-book kernel очищен от `engine`, route helpers, cache mutation и
freshness lookup. В нём осталась только сборка options из готовых заявок и
числовых freight-значений. `landed`, route lookup, cache и invalidation
остаются в `market_book_boundary`; freshness получает эпоху явно.

Пересборка Cython успешна. Focused-набор прошёл `231 passed`, включая parity,
market book и production outlook. Это закрывает структурные пункты 1–3 в
текущем planning/market-book срезе; общий ownership settlement/ledger ещё не
закрыт.
