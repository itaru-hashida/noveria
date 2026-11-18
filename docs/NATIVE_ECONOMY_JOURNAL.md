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

### Повторная проверка границ

Исправлена регрессия малой книги. Пакетная подготовка доставки допустима для
штатного `Economy.landed`, где доставка не зависит от цены продавца.
Переопределённый метод получает исходную цену каждой заявки. Добавлен
регрессионный тест через `Economy._market_book`.

Прежняя запись завысила степень завершённости. Именованный snapshot применяется
на резервной ветке, а основной прогноз получает отдельные наборы входов.
Явная эпоха книги отделила проверку свежести от ядра, но не централизовала
запись и сброс кэшей. Эти задачи остаются открытыми перед settlement.

### Приёмка контракта прогноза и кэшей

Основной и резервный расчёты проходят через подготовку snapshot и возврат
результата. Запись и сброс четырёх кэшей проходят через EconomyServices.
Смена маршрута очищает котировки доставки, книги и прогноз продаж со склада.
Попытка изменить формат recovery-ключа нарушила побитовую parity Cython.
Прежний формат восстановлен. Cython-драйвер пересобран, 142 focused-теста
прошли, включая проверку эталонных траекторий.

## 2026-11-18 — перенос orchestration enterprise settlement в Python

Убран `run_settle` из `enterprise_settlement_kernel.pyx`. Обход предприятий,
порядок чтений и записей, short-circuit после возврата капитала, оплата оброка
и lifecycle-вызовы теперь выполняются в `enterprise_settlement_boundary`.
Cython оставляет только чистые расчёты финансового результата и счётчика
нежизнеспособности; Python-эталон сохранён независимо. Денежные транзакции не
рефакторились.

Пересборка и focused enterprise_settlement/settlement/parity baseline
выполнены после обновления тестов на native-only calculation exports.

## 2026-11-18 — откат расчётов с предприятиями

Граница enterprise settlement восстанавливает изменения при исключении в
обходе предприятий. Балансы и денежные резервы читаются и восстанавливаются
через CashStore. Статус и резерв заявки восстанавливаются через OrderStore.
Состояние строк, ревизии спроса и записи отчёта восстанавливаются на месте.
Транзакция охватывает расчёты с предприятиями до распределения доходов.

Первый вариант со снимком всего движка отклонён на ревью: он заменял живые
словари и не сохранял авторитетные колонки. Точечный откат проверен на сбоях
после оплаты оброка, возврата капитала и закрытия. Прошли 82 focused-теста,
включая побитовое сравнение с эталоном.

### Общая граница settlement

Оба режима хода вызывают `Economy.settle`, который применяет расчёты с
предприятиями и распределение доходов в одной границе отката. При исключении
после перевода восстанавливаются состояние предприятий, балансы получателей
и отправителей, записи отчёта и RNG. Регрессионный тест вызывает сбой после
реального зачисления средств домохозяйству.

## 2026-11-18 — атомарная граница одной market trade

Перед применением сделки зафиксирована карта её реальных записей: CashStore
для балансов и резерва покупателя, StockStore для товара и резерва продавца,
значения склада, две строки OrderStore, доходы предприятия, flows/trades,
ревизии и кэши котировок. Расчёт и обычное применение остались в существующем
trade kernel; `Economy.match` теперь передаёт его через узкую transaction
границу, которая восстанавливает данные через прежних владельцев.

Добавлен focused-регресс: реальный `engine.match` выполняет первый перевод,
затем получает внедрённый сбой на втором и возвращает состояние сделки к
исходному. Другие рыночные фазы и полный pytest этим срезом не затрагивались.

## 2026-11-18 — чистый native calculation для market trade

`trade_kernel` теперь экспортирует только расчёт по подготовленному tuple.
Python-граница владеет подготовкой и применением через прежние CashStore,
StockStore и OrderStore, а `use_stock` остаётся единственным расчётом живой
стоимости склада. Производственный `commit_trade` — Python wrapper, который
вызывает только `_kernel.calculate_trade`; Cython callback с owner удалён.
Независимый `commit_trade_python` сохранён буквальным oracle прежнего порядка.

GCC build успешен. Focused trade/match/monthly parity: 81 passed. Полный
pytest не запускался.

## 2026-11-18 — Python orchestration matcher и чистый расчёт q

`match_kernel` больше не обходит книгу и не принимает `owner`: он экспортирует
только `calculate_quantity` по явным числовым входам. Python сохраняет
группировку цен, `shuffled`, live-перечитывания `remaining`/`reserved`,
`purchase_quantity`, остановку после полного исполнения и `commit_trade`.
Независимый `run_match_python` оставлен полным oracle.

Старый `match_driver` оставлен импортируемым для совместимости диагностики,
но production bindings `run_buyer`/`run_buyers` намеренно указывают на
Python-реализации. Поэтому monthly path больше не передаёт owner в Cython
driver; callbacks и extensions выполняются в прежнем Python-порядке.

`match_kernel` и изменённые native extensions пересобраны GCC. Focused
matcher/driver/monthly parity: 68 passed. Полный pytest не запускался.

## 2026-11-18 — именованные контракты numeric market calculations

Позиционные tuples заменены на именованные Python records
`TradeCalculationInputs/Result` и `MatchQuantityInputs/Result`. Trade input
содержит только числа/флаг расчёта (касса, цены, резерв, налог, количество и
freight); route path/distance, идентификаторы заявок, month/trade number и
`trade_id` остаются в Python application boundary. Match input содержит только
live-снятые числовые значения; обход и callbacks по-прежнему Python.

Граница сохраняет Python object ABI: Cython получает именованный Python
record, читает его атрибуты и возвращает numeric tuple, который boundary
оборачивает в именованный result. Live objects в kernel не передаются,
числовые типы и порядок арифметики не меняются.

GCC rebuild выполнен. Focused trade/matcher/driver/monthly parity: 108 passed.
Полный pytest не запускался.

## 2026-11-18 — явные контракты planning и owner API payroll

Planning и production outlook получили именованные calculation records:
`PlanCalculationInputs/Result` и `OutlookCalculationInputs/Result`. В native
ядро передаются готовые числовые входы; engine/hooks и применение результата
остаются в Python boundary. Для outlook сохранён единый prepared entrypoint
для расчёта по книгам, а живой склад читается через `StockStore`.

Операционные и payroll-резервы теперь читаются и меняются через `CashStore`
(`release_operating_protected`, `set_payroll_protected`), без прямой записи
этих полей в account dictionary. Исправлен вызов native outlook после
перехода на именованный контракт; parity и rollback сохранены.

Пересборка Cython выполнена GCC. Focused production-outlook/plan/parity:
58 passed. Полный pytest не запускался. Аудит полного ownership stock/orders/
reservations продолжается отдельным следующим срезом.

## 2026-11-18 — аудит writers orders/reservations/stock, узкий phase-срез

От `3a32fd0` проверены native- и phase-пути. Канонические записи склада и
`stock_reserved` уже проходят через `StockStore`, а `remaining`/`reserved`/
`status`/`price`/`quantity` и metadata заявки — через `OrderStore`.

В этом срезе исправлены оставшиеся безопасные записи в `monthly_hooks`,
`match_driver` и `update_prices_boundary`: `reason`, `execution_status`,
`quantity` и `backing` больше не записываются напрямую в словарь заявки.
Внутренние записи самих `StockStore`/`OrderStore` оставлены как реализация
назначенного owner; записи физических агрегатов и valuation `values` не являются
каноническим складом и не смешивались с ownership stock.

Settlement и performance не менялись. Focused ownership/update/match/monthly
проверки и пересборка Cython должны подтвердить parity, порядок и отсутствие
нового writer-а.

## 2026-11-18 — завершение ownership metadata заявок

`OrderStore` получил узкие методы `set_quantity` и `set_metadata`. Записи
`quantity`, `backing`, `reason` и `adaptive_input_budget` в release, matching,
repricing и planning теперь проходят через владельца заявки; колоночные поля
`remaining`, `reserved`, `status`, `price` уже были закрыты ранее. Порядок
расчёта и применение результата не менялись.

## 2026-11-18 — приёмка settlement как фазы экономики

Проверена существующая реализация, повторный перенос не выполнялся. Точка
входа одна: `monthly_boundary`/`monthly_driver` зовут `Economy.settle`, а он —
`enterprise_settlement_boundary.settle_month`, где оба цикла (расчёты с
предприятиями и распределение доходов) идут под одной транзакцией
`_SettlementTransaction`. Расчёт отделён: `enterprise_settlement_kernel`
экспортирует только `calculate_financial_results` и
`calculate_unviable_months`, получает подготовленный tuple и возвращает tuple;
живые dict-ы, engine и helpers в ядро не передаются.

Дефект: `cancel_order` писал причину отмены прямо в словарь заявки
(`order['reason'] = reason`), обходя `OrderStore.set_metadata`. В settlement
этот путь достигается через `close_enterprise`, то есть закрытие предприятия
оставляло metadata-запись мимо назначенного владельца. Исправлено минимально:
запись идёт через `store.set_metadata(order, 'reason', reason)`; порядок
операций, тексты причин, статусы и колонки не менялись.

Добавлены два регресса: собственный AST-страж по телам settlement-функций
(`reason`, `quantity`, `backing`, `execution_status`) и поведенческий тест
реального `close_enterprise`, который требует вызова владельца. Проверено, что
без правки поведенческий тест падает.

Проверки: расширения пересобраны (прежние `.pyd` не содержали
`plan_kernel.calculate_target`, поэтому импорт падал); focused
enterprise_settlement + settlement + order_store + parity baseline — `103
passed`; отдельно enterprise-settlement `28 passed`, order_store+settlement `69
passed`, смежные cancel/order_ownership/owners/v3/monthly — `156 passed` при 7
ошибках setup, вызванных не кодом: `tmp_path` не может создать каталог с
`mode=0o700`, потому что такой каталог получает явный DACL без гранта песочницы.
Ошибка воспроизводится минимальным тестом вне репозитория (`_tmp_probe_test.py`),
к settlement отношения не имеет.

Откат проверен инъекциями: 578 сбоев на каждом `transfer` внутри `settle` на
двухмесячном мире и отдельные сбои после оплаты оброка, возврата капитала и
закрытия предприятия, а также на втором переводе второй фазы; состояние
(предприятия, счета, cash-колонки, склад, заявки, отчёт, RNG, ревизии, кэши)
возвращается побитово, расхождений нет.

Ограничения, вынесенные отдельно: save/load и durable storage в срез не
входили; прямой writer `reason` в `cancel_order` закрыт, но release-путь
(`_claim_public_stock`, `release_kernel`) по-прежнему пишет `quantity`/`backing`
в обход `OrderStore` — это отдельный срез, не settlement. Оптимизация скорости
не выполнялась.

## 2026-11-18 — освобождение офферов через OrderStore

База `46ca0bc`. Закрыты два оставшихся обхода владельца заявок в release-пути:

* `V3._claim_public_stock` (`modules/economy/v3.py`) — единственная реальная
  точка вызова, из `public_project_requests` для материалов, оснащения и
  поддержки городской стройки. Прежнее тело писало
  `offer['quantity'] -= take` и `offer['backing'] -= take` прямо в dict;
  теперь это `store.set_quantity` и `store.set_metadata`, `remaining` идёт
  прежним `set_remaining`, склад — через `stock_store.release_reserved`.
* `release_kernel.detach_offer` и его Python-эталон
  `release_boundary.detach_offer_python` — срез освобождения собственного
  sell-оффера в `project_material_requests`. Обе ветки пишут `quantity`,
  `remaining`, `backing` через владельца; временные переходники
  `_store_quantity`/`_store_metadata`, которые при отсутствии сеттера писали
  в dict, удалены — именно они и были скрытым fallback-обходом.

Порядок записей прежний (`quantity` → `remaining` → `backing`), арифметика
`offer[...]-take` не менялась, тип int/float сохраняется. Отмена исчерпанного
оффера, причина отмены, `mark_market_quotes_dirty` и порядок RNG/заявок не
затронуты. Заглушки `FakeStore` в focused-тестах material/release/delivery/
cancel/construction_buy дополнены `set_quantity`/`set_metadata` — рабочий код
вместо этого прямых записей не получил.

Новые регрессы: `test_public_stock_release_writes_all_fields_through_owner`
(реальная точка входа `public_project_requests`, частичное освобождение 9 → 3,
проверяет фактические вызовы владельца) и
`test_python_reference_also_writes_through_store` (fallback-ветка). Первый
проверен на падении по прежнему коду: журнал владельца содержал только
`('set_remaining', '0-5', 3)`.

Проверки: расширения пересобраны (`setup.py build_ext --inplace --force`,
`release_kernel` пересобран, импорт грузит свежий `.pyd`). Focused
release/material/cancel/construction_buy/delivery/order_store/order_ownership/
order_columns + `tests/test_native_parity_baseline.py` — `241 passed`; отдельно
order_store/ownership/columns и parity до добавления регрессов — `80 passed`.
Полный pytest не запускался.

Осталось: settlement, save/load и ускорение в срез не входили; артефакты
сборки (`*.pyd`, `build/`, сгенерированные `.c`) в коммит не включались.
Неотслеживаемые `demand_kernel_api.h`, `offer_view_api.h`, `quote_kernel_api.h`
оставлены как есть — они не относятся к задаче.

## 2026-11-18 — продолжение симуляции после save/load

База `811be35`. Проверено, что следующий ход после штатного сохранения и
загрузки совпадает с непрерывным прогоном.

Штатный путь: `WorldService.save_checkpoint` → `EconomyState.save_checkpoint`
(Parquet + `schema.json`) и `WorldService.load_checkpoint` →
`EconomyState.load_checkpoint`. Добавлен сквозной регресс
`test_save_load_continues_next_turn_exactly` в `tests/test_economy_state.py`,
параметризованный по `native_mode`: Python (`off`) и native (`cython`), seed
`73129`, `report_detail=standard`. Каждый режим сравнивается только с
непрерывным прогоном того же режима.

Сценарий теста: после завершённого хода контрольная ветка делает следующий ход
без перезапуска; вторая сохраняет состояние, открывает его штатной загрузкой в
новом экземпляре (новый `WorldDB`, новый `WorldService`) и делает тот же ход.
Свежесть проверяется по тождеству: другой объект состояния, движка, RNG,
`CashStore`, `StockStore`, `OrderStore` и native-контейнера `WorldState`. Для
режима `cython` тест требует загруженных расширений (`NATIVE_MONTHLY_DRIVER`,
`NATIVE_CASH_STORE`), поэтому молчаливый уход в Python-эталон невозможен.

Сравниваются: точное состояние (счета с деньгами, складом, значениями,
резервами и `protected`; предприятия; свойства сущностей; `meta` без
`market_history`; история рынка; `construction_receipts`;
`pending_institutions`; история отчётов; флаг `keep_reports`), состояние RNG и
отчёт следующего хода. В отчёте отдельно сверяются порядок заявок с их ID,
остатками (`remaining`), резервами (`reserved`) и `backing`, порядок сделок с
ID, количеством, ценой, золотом, доставкой, налогом и парой заявок, а также
порядок денежных потоков. Сортировки и округления нет: числа сравниваются через
`repr(float)`, списки — поэлементно в исходном порядке.

Wall-clock тайминги: список исключений задан явно
(`duration`, `wall`, `timings`, `phase_timings`, `elapsed_seconds`, `seconds`).
В отчёте `standard` таких полей сейчас нет — тест это фиксирует, поэтому
появление нового поля таймингов не спрячется. Ничего другого не исключалось.

Первое расхождение найдено на границе загрузки и исправлено минимально:
`schema.json` не содержал режим хранения истории отчётов, поэтому
`EconomyState.load_checkpoint` создавал состояние с `keep_reports`, вычисленным
из `report_detail` (`True` для `standard`), тогда как непрерывный прогон жил с
`keep_reports=False`. Симптом в тесте — первое расхождение
`('/reports/длина', 0, 1)` сразу после загрузки. Дальше разница росла: следующий
ход после загрузки копил отчёты, а сохранение записало бы в `reports.parquet` и
`trades.parquet` историю, которой у контрольной ветки нет. Исправлено в
`core/economy_state.py`: `save_checkpoint` пишет `keep_reports` в
`schema.json`, `load_checkpoint` восстанавливает флаг и держит историю только
при нём, а `last_report` берёт из checkpoint всегда — он нужен интерфейсу и
штатному сохранению следующего хода. Порядок фаз, RNG, формат таблиц и состав
полей не менялись; версия схемы не поднималась, потому что старые checkpoint'ы
читаются прежним путём (отсутствующий ключ даёт `None`, то есть прежнее
поведение по `report_detail`).

Проверено падение регресса на старом коде: без правки оба режима падают с
`AssertionError: состояние после загрузки: первое расхождение
('/reports/длина', 0, 1)`.

Следствие правки закрыто в `core/lab.py`: сохранение пишет историю как
`reports or [last_report]`, поэтому read-only просмотр (`read_live`,
`live_reports`) обязан показывать тот же последний отчёт, а не пустой список.
Без этой парной правки `tests/test_lab.py` терял месяц просмотра. Правка
касается только чтения checkpoint, не хода и не формата.

Проверки: расширения актуальны (все 40 `.pyx` старше своих `.pyd`), Cython
действительно загружен в сценарии `cython`. Focused
`tests/test_economy_state.py` + `tests/test_economy_native_cash_ownership.py` +
`tests/test_native_parity_baseline.py` — `48 passed`; новый сквозной тест
отдельно — `2 passed`. Смежные `tests/test_economy.py`,
`test_lab_export.py`, `test_stock_planning_game.py`, `test_parallel_planning.py`
проходят; в `tests/test_lab.py` остаётся одно падение
`test_reproducible_isolated_runs_and_optional_journal` (расхождение
предприятий между прогонами `full` и `summary`), которое воспроизводится и на
чистой базе `811be35` без правок этой задачи. Полный pytest не запускался.

Осталось: миграция формата, durable storage, settlement и ускорение в срез не
входили. Артефакты сборки (`*.pyd`, `build/`, сгенерированные `.c`) в коммит не
включались; неотслеживаемые `demand_kernel_api.h`, `offer_view_api.h`,
`quote_kernel_api.h` оставлены на месте.
# Восстановление при сбое публикации checkpoint

Предыдущий checkpoint сохраняется рядом до публикации нового. При ошибке
переименования он возвращается на штатный путь. Если публикация прервана
после перемещения старого каталога, загрузчик читает предыдущую версию,
а последующее сохранение восстанавливает штатный путь.

Три focused-проверки прошли. Они покрывают штатный round trip, ошибку
публикации и прерывание между переименованиями. После открытия проверены
состояние, RNG, повтор следующего хода и новое сохранение.

## 2026-11-18 — граница фактического производства

Граница production обходит предприятия в прежнем порядке и готовит
ограничения из актуального состояния перед расчётом каждого выпуска.
Cython возвращает объём, причину ограничения и признак нехватки сырья.
Подготовка наблюдения, выплата зарплаты, списание сырья и износа,
оприходование выпуска и обновление отчёта идут через Python.
Python-расчёт служит эталоном выбора ограничения и fallback.

Ядро пересобрано GCC. Focused-тесты производства и parity baseline прошли
(40 тестов).
