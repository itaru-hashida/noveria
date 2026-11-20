# Дорожная карта native-ядра Noveria

## Архитектурная модель

Вычислительное ядро и владение состоянием развиваются отдельно. Выделение
расчёта в Cython может идти до передачи владения складами, резервами и
заявками.

### Граница планирования

- Оркестратор сохраняет порядок предприятий и RNG. Для текущего предприятия он
  выполняет recovery и подготавливает актуальные входы.
- Cython получает snapshot параметров предприятия, данные спроса, котировки,
  книгу предложений и остатки. Ядро возвращает результат по явному контракту и
  не получает `engine` или Python callbacks.
- Слой применения передаёт изменения существующим владельцам состояния до
  перехода к следующему предприятию. При ошибке вычисления исходное состояние
  не должно быть частично изменено.

Книгу и данные спроса следует переиспользовать по действующим ревизиям их
источников. Рабочие остатки для покрытия спроса принадлежат конкретному
прогнозу. Порядок групп, предложений и арифметических операций входит в
контракт.

### Ограничения, подтверждённые экспериментами

Подготовка `OfferView` на каждую группу замедлила прогретое планирование.
Прямой обход живых строк дал около 4% выигрыша при побитовом совпадении
состояния и отчёта. Следующий срез нужно оценивать вместе со стоимостью
подготовки входов, native-расчёта и применения результата.

### Владение состоянием

У каждого изменяемого поля один владелец записи. Адаптеры делегируют записи
владельцу. Кэши и временные буферы восстанавливаются из авторитетного
состояния. Перенос владения требует сохранения и восстановления согласованной
ревизии вместе с RNG, а также проверки rollback при сбое.

## Архитектурная цель

Цель проекта — не механически переписать весь Python на C, а получить изолированное
детерминированное native-ядро, которое можно постепенно реализовывать на Cython и
при необходимости заменять чистым C без изменения игрового API.

Целевое разделение:

```text
Python: сценарии, orchestration, конфигурация, отчёты и скриптовый API
    ↓
тонкий boundary: явные структуры, ID, массивы, числовые значения
    ↓
C-подобное Cython-ядро: расчёты, состояние, RNG и инварианты
    ↓
позже: отдельные модули на C с тем же контрактом
```

Правила для нового кода:

- native-модули не зависят от `engine`, Python dictionaries и Python callbacks в
  горячих циклах;
- границы описываются явными `.pxd`-контрактами и простыми структурами данных;
- состояние, ownership, версии, RNG и cache invalidation имеют одного явного
  владельца;
- порядок предприятий, заявок и случайных чисел является частью контракта;
- Python остаётся фасадом и слоем применения результатов, а не вычислительным
  ядром;
- новый модуль принимается только при parity, focused-тестах и измеримом
  benchmark;
- чистый C вводится только для стабильных и подтверждённых профилированием
  узких мест, а не как цель сама по себе.

Критерий успеха — не максимальная доля строк в Cython, а возможность добавлять
механику без возврата Python callbacks в горячие циклы и возможность заменить
отдельный native-модуль на C без переписывания остальной системы.

## Зафиксированная позиция по Cython и производительности

Текущее ядро является гибридным: оно состоит из Cython-расчётов и Python-частей.
Это не промежуточная ошибка и не требует немедленного полного rewrite на C.
Python остаётся частью ядра там, где он отвечает за orchestration, подготовку
данных, применение результатов и редкие ветви. Cython отвечает за крупные
массовые расчёты, где native-контракт уже стабилен.

Перенос отдельного callback или небольшой функции в Cython не обязан ускорить
полный ход. Если подготовка входов, словари, market helpers и применение
результата остаются в Python, стоимость границ может поглотить выигрыш. Поэтому
15% — это критерий принятия сложного performance-среза, а не требование к
каждому native-модулю и не причина отказываться от Cython как архитектурного
слоя.

Правило принятия изменений:

- откатывать перенос, если он усложняет код и не даёт ни измеримого выигрыша,
  ни полезной изоляции контракта;
- сохранять структурный native-контракт, если он уменьшает связанность и
  готовит модуль к будущей замене на C, даже при нулевом немедленном выигрыше;
- для performance-изменений смотреть на полный pipeline: подготовка → native
  расчёт → применение, а не на скорость отдельной функции;
- считать достаточным ориентиром 5–10% ускорения полного хода, а 15% требовать
  от крупного и усложняющего перенос среза;
- не переносить старый Python ради процента покрытия Cython; новые крупные
  механики проектировать native-first.

## Архитектурные претензии, которые остаются в силе

Претензии относятся не к наличию Python как такового, а к неявным зависимостям
и смешению ответственности:

1. **Python callbacks в горячих циклах.** Их нельзя оставлять внутри массовых
   расчётов planning, market, production и settlement. Callback в редкой ветви
   допустим, callback на каждое предприятие, товар или заявку — нет. Данные
   нужно подготовить пакетно или передавать через компактный native-контракт.
2. **Неявный владелец состояния.** Для каждого durable и mutable datum должен
   быть один owner: кто владеет записью, кто применяет изменения, кто отвечает
   за revision, RNG и invalidation. Остальные компоненты только читают через
   контракт или отправляют команды.
3. **Смешение расчёта и применения.** Native-ядро должно считать результат, а
   отдельный boundary — применить его к миру. Нельзя одновременно менять
   Python-мир из глубины расчёта и считать, что состояние принадлежит ядру.
4. **Неявные структуры данных.** Передача словарей, объектов engine и helpers в
   native-код затрудняет parity, профилирование и будущий перенос на C. Для
   горячих путей нужны ID, массивы, числовые поля, версии и явные результаты.
5. **Дробление на слишком мелкие native-вызовы.** Много маленьких переходов
   Python↔Cython может быть медленнее одного цельного контура. Следующий срез
   должен включать подготовку, расчёт и применение настолько, насколько это
   возможно без нарушения ownership и порядка операций.

   Эти пункты — архитектурные ограничения проекта, а не требование немедленно
   переписать весь существующий код.

## Целевой контракт одного вычислительного контура

Эти четыре требования рассматриваются вместе: native-граница должна быть
строгой, а не только быстрой.

```text
Python orchestration
    ↓ подготовленный snapshot входных данных
Native/Cython calculation
    ↓ immutable result
Python apply phase
    ↓
единственный владелец состояния
```

### Горячие циклы

В циклах по предприятиям, товарам, клиентам и заявкам не допускаются вызовы
`engine`-методов, market helpers, Python callbacks и dictionary-heavy lookup.
Допустимы подготовка входов до цикла, один batch/native-вызов и отдельное
применение результата. Приёмка должна подтверждать инструментальным счётчиком,
что в вычислительной части нет callback на каждую сущность.

### Карта ownership

Для каждого mutable или durable datum заранее фиксируются владелец записи,
читатели и способ изменения:

| Состояние | Владелец записи | Читатели | Изменение |
|---|---|---|---|
| cash | ledger/storage | planning, reports | transaction |
| stock | stock owner | production, market | command/result |
| orders | order owner | market, reports | append/settle |
| RNG | simulation owner | native kernels | explicit state |
| caches | соответствующий owner | расчёты | revision invalidation |

Если два компонента могут напрямую менять одну структуру, ownership считается
неопределённым и контур нельзя объявлять завершённым.

### Разделение расчёта и применения

Расчёт не меняет `engine`, cash, stock, orders или caches:

```text
calculate(snapshot) -> result
apply(result, owner) -> new state
```

Boundary проверяет результат, а назначенный owner применяет его транзакционно.
При исключении не должно оставаться частично применённого состояния.

### Данные native-контракта

В native-контракт передаются integer IDs, `int`/`double`/`bool`, typed arrays,
явные Cython structs, revision numbers и RNG state. Внутрь не передаются
`engine`, Python dictionaries, классы предприятий, bound methods, market
helpers и callbacks. Python может собрать snapshot на внешней границе, но
native-расчёт snapshot не обходит.

Первым эталонным вертикальным срезом считать `planning → production outlook →
input orders`: подготовить snapshot, выполнить Cython-расчёт, получить result и
применить его через владельцев состояния. После parity, RNG/recovery и
exception-тестов этот шаблон распространяется на settlement и market/ledger.

## Порядок работ

### Что уже сделано

Базовая фаза структуризации закрыта, но это не означает, что все callback-и
исчезли. Уже выполнено:

- закреплены владельцы cash, stock, orders и проектных полей;
- прямые записи проектов, офферов и метаданных переведены через владельцев;
- расчёт отделён от применения для производства, потребления и дивидендов;
- добавлены контракты результатов и rollback для завершения строительства;
- save/load продолжает ход с тем же состоянием, RNG и отчётом;
- settlement и release-пути проверены focused-тестами и parity.

Остающаяся проблема — не «Python вообще», а подготовка данных для native
расчёта: некоторые границы создают hooks и делают много отдельных чтений
engine на каждое предприятие. Это следующая отдельная фаза.

### Фаза callback-контрактов

Сделано и сжато: `production_outlook` получил immutable
`OutlookSnapshot`/`OutlookResult`; `plan_kernel` — `PlanPreparation` →
`PlanCalculationInputs` → Cython → `PlanApply`; market-входы получили
`ReachableDemandSnapshot` и `QuoteSnapshot`. Native calculation этих срезов не
получает `engine` и Python callbacks. Parity/rollback проверены focused-тестами.

Остаётся:

1. `quoted_cost` — сделано: `reference_hook` и `landed_hook` убраны из штатного
   native call site. Подготовленный числовой quote snapshot подключён,
   recovery/reference/cache parity подтверждена.
2. `volume_input_prices`: убрать callbacks `free_stock`, `quoted_cost` и
   `reference_price` из native/fallback расчёта через пакетный snapshot.
3. `demand` fallback — сделано: при отсутствии подготовленного native
   `offer_view` используется Python-эталон, а `accounts.__getitem__`/`offers`
   больше не передаются внутрь native fallback.
4. `net_price` — сделано: Python материализует числовой список котировок,
   Cython считает без доступа к order dictionaries и hooks; parity подтверждена.
5. После callback-срезов — полный ownership-аудит и единый benchmark.

### Финальный аудит текущего среза

Проверено на текущей ветке: `CashStore`, `StockStore`, `OrderStore` и
`project_slot` остаются назначенными writers; `PlanApply` и Python apply-фазы
меняют состояние только после native-расчёта. Market audit закрыт: прямые
записи metadata заявок (`project_id`, household budget/top-up flags) переведены
через `OrderStore`. Остальные записи `Economy` в recovery и market — derived
cache, snapshot или последовательное применение сделки; отдельный `MarketStore`
не нужен.

Первый общий замер после callback-срезов: seed `73129`, 13 месяцев,
`standard`, без cProfile — `9.371 s`, в среднем `0.725 s/ход`. Зафиксировано:
`production_outlook` 6302 вызова, `quoted_cost` 51605, `offers` 115845,
`_market_book` 163881, `landed` 919798. Это baseline после структуризации,
не сравнение с прежней веткой; A/B Python/Cython — следующий отдельный замер.

Пункты 1–3 для `production_outlook` выполнены: snapshot и result-контракты
immutable, старый внешний API сохранён через compatibility view, focused
parity пройдена. Внутри native calculation callback-ов нет; оставшиеся
вызовы hooks относятся к Python-подготовке входов и являются следующим
кандидатом на пакетизацию. Для `plan_kernel` расчётный fallback также очищен
от фиктивного параметра `hooks`: native и Python расчёт используют один
контракт `PlanCalculationInputs`. Market-подготовка теперь также имеет явные
`ReachableDemandSnapshot` и `QuoteSnapshot`; перед Cython они приводятся к
ABI-совместимым tuples без изменения порядка данных.
В `plan_boundary` чтение и применение физически разделены на
`PlanPreparation` и `PlanApply`; старое имя `PlanHooks` оставлено только как
совместимый alias для apply-фазы. В `production_outlook` подготовка также
названа отдельно — `OutlookPreparation`; `OutlookHooks` оставлен только как
совместимый alias, а не как часть native API.

## Критерии достижения целей

Planning-срез считается завершённым после выполнения всех условий ниже:

1. Для непокрытой книги сохранены порядок чтений, recovery, cache invalidation,
   RNG и порядок заявок. Это подтверждается focused parity-тестами.
2. Ошибка на каждом шаге расчёта и применения оставляет состояние предприятия,
   cash store, order store и revisions прежним.
3. A/B benchmark на 13 ходах с несколькими seed показывает поведение полного
   хода и planning до и после. Срез принимается при отсутствии ухудшения полного
   хода более чем на 5% или при подтверждённой архитектурной пользе без
   усложнения пути.
4. Focused-набор, Cython-сборка и `git diff --check` проходят.

После этого planning-срез можно объявить достигшим цели. Затем тем же контрактом
переносится settlement, после него market/ledger. Весь переход будет завершён,
когда для этих срезов выполнены те же parity, rollback, ownership и benchmark
gates.

1. После callback-фазы принять `production_outlook` по parity, rollback и
   benchmark-gates.
2. Подключить бюджет закупок к тому же представлению, сохранив порядок
   котировок, предприятий, резервирования и ID заявок.
3. Продолжить market/ledger отдельными срезами: сначала карта writers для
   склада, резервов и заявок, затем применение сделок и оценка запасов.

Первый этап можно выполнять до завершения этапов владения ниже. Граница расчёта описана в [архитектуре ядра](NATIVE_ECONOMY_ARCHITECTURE.md).

## Приёмка вычислительного ядра

- Проверить, что ядро прогноза вызывается через штатную точку входа и выполняет расчёт без `engine` и Python-коллбеков. Проверить инвалидацию входных данных, порядок recovery, предприятий и заявок, состояние и RNG, включая поведение при исключении.
- Запустить focused-тесты и сверить точное воспроизведение. Снять A/B без профилировщика на seed `73129`, `report_detail=standard`, по три последовательных месяца в режимах `off` и `cython`. Раздельно измерить подготовку, расчёт и применение прогноза, `planning_execution` и полное время хода. Сравнить первый ход и два прогретых по нескольким повторам.
- Принимать структурное выделение при совпадении поведения и отсутствии подтверждённого замедления полного хода более чем на 5%. Для задачи на ускорение планирования оставить отдельный порог в 15% на прогретом Cython.

## Владельцы

- **Владелец данных — Native Storage.** Он владеет committed state, схемой, версиями, миграциями, валидацией, transaction boundaries и recovery.
- **Владелец процесса — Application Lifecycle Coordinator.** Он управляет initialization, migration scheduling, save requests, shutdown и recovery orchestration. Он вызывает Native Storage, но не хранит второго источника истины.
- **Потребители — UI, domain logic, adapters и caches.** Они читают через контракт и отправляют команды. Кэш — derived state.

Native Storage решает, что committed. Lifecycle Coordinator решает, когда операция запрошена.

## Фаза 1. Граница владения

Инвентаризировать всех writers durable state, форматы, migration hooks, caches, save triggers, RNG, background tasks и shutdown.

```text
если компонент уже имеет исключительное durable ownership:
    сохранить его и направить всех callers через его контракт
иначе:
    назначить Native Storage и ввести compatibility boundary

если caller пишет durable state напрямую:
    перенести запись за Native Storage
иначе:
    оставить caller consumer'ом

если cache восстанавливается:
    считать его disposable derived state
иначе:
    классифицировать его как durable data до миграции
```

Gate: каждому durable datum назначен владелец, каждому write path — destination.

## Фаза 2. Миграция и recovery

Разделить migration формата и transfer write authority. Определить поддерживаемые версии, conversion rules, validation, restart behavior и сохранение recoverable source.

```text
если store отсутствует:
    инициализировать native schema
иначе если версия поддерживается:
    сохранить recoverable source и подготовить migration
иначе:
    остановить запуск с явным compatibility outcome

если migration output проходит integrity и semantic validation:
    пометить его готовым к authority transfer
иначе:
    сохранить source и сообщить об ошибке

если запуск обнаружил interrupted migration:
    продолжить только безопасно повторяемую фазу
иначе:
    удалить incomplete output и восстановиться из source
```

Gate: restart, failure и recovery определены однозначно; authority не переключается преждевременно.

## Фаза 3. Передача authority

Перед cutover погасить mutations и дождаться in-flight operations. После cutover legacy code может только делегировать Native Storage.

```text
если legacy writers или in-flight mutations ещё есть:
    отложить cutover или явно отклонить их
иначе:
    завершить migration и записать native authority

если native authority durably установлена:
    направить все reads и writes через Native Storage
иначе:
    оставить source authoritative

если старым callers нужна совместимость:
    оставить delegating adapter
иначе:
    удалить старые entry points
```

Gate: ровно один компонент принимает writes, включая после restart и interruption на cutover.

## Save и rollback

Save должен включать coherent state revision, RNG state и metadata для продолжения.

```text
если save относится к current revision:
    validate и commit через Native Storage
иначе:
    отклонить stale request или применить явную conflict policy

если commit достиг требуемой durability boundary:
    подтвердить success и увеличить committed revision
иначе если failure произошёл до commit:
    сохранить предыдущую committed revision
иначе:
    сначала определить outcome transaction, потом retry или acknowledge

если rollback нужен до commit:
    отбросить staged changes
иначе если rollback после commit:
    выполнить compensating transaction или restore operation
```

Если старая версия не читает новую schema, rollback release требует reverse migration или explicit snapshot restoration. Нельзя молча активировать устаревший legacy store.

## RNG и concurrency

```text
если нужна deterministic continuation:
    атомарно сохранять RNG state вместе с изменениями, которые он породил
иначе:
    явно документировать изменение последовательности после restart

если concurrent operations потребляют randomness:
    сериализовать consumption или назначить deterministic streams
иначе:
    оставить один ordered stream

если background result относится к obsolete revision:
    отклонить или пересчитать на current revision
иначе:
    применить внутри storage transaction boundary
```

Native Storage отвечает за write ordering и conflict handling. Persistent state не делает sequential economic commits безопасными для параллельного применения.

## Финальные gates

```text
если остался независимый legacy writer:
    consolidation incomplete
иначе если migration/recovery может выбрать двух owners:
    consolidation incomplete
иначе если save, rollback, RNG или conflicts неоднозначны:
    consolidation incomplete
иначе если shutdown может потерять accepted write без outcome:
    consolidation incomplete
иначе:
    объявить Native Storage sole authoritative owner
```

Для Noveria мигрировать по slices: сначала cash/stock/orders/reservations, затем investment/construction, затем остальные phase stores. Каждый slice принимается только после exact state/decision/RNG replay, money-goods-reservation invariants, save/load continuation и injected-failure rollback.

## Уточнение цели по ownership

Ownership нужен для крупных доменов, которые действительно поддерживают
инварианты и rollback. Не следует создавать отдельного owner для каждого поля
или прятать обычное присваивание в тонкую функцию без дополнительной гарантии.

Текущие крупные владельцы:

- `CashStore` — баланс, резервы и защищённые деньги;
- `StockStore` — склад, физические остатки и резервы склада;
- `OrderStore` — заявки, остатки, статусы и metadata заявок;
- `project_slot` пока остаётся переходной границей проектного состояния;
- `EconomyState` — durable state, save/load и продолжение после загрузки.

Предприятия и проекты не получают новые микроскопические stores до тех пор, пока
аудит не покажет отдельный инвариант, несколько независимых writers или
необходимость атомарного rollback. Для фаз достаточно явного контура
`calculate → result → apply`, где apply меняет связанные поля одной операцией.

Перед добавлением новой механики нужно сначала определить домен-владелец,
изменяемые инварианты и точку rollback. Если для этого достаточно существующего
владельца, новый класс не создаётся.

## Статус текущей фазы

Фаза структуризации текущих границ завершена на уровне принятого гибридного
ядра. Подтверждены владельцы cash, stock и orders, проектные записи проходят
через переходную границу, а production, consumption и dividends используют
отдельные apply-фазы. Завершение проекта защищено транзакционным snapshot с
восстановлением связанных владельцев.

Save/load continuation и parity проверяются focused-набором. Последняя проверка
текущего среза дала `207 passed`.

Оставшиеся Python callbacks в planning, production outlook, market helpers и
части settlement относятся к следующей performance-фазе. Их перенос требует
пакетных snapshots и числовых результатов, а не новых микроскопических owners.
До начала новых механик этот срез считается завершённым, а callback-аудит
остаётся отдельной задачей с собственными benchmark и parity gates.
