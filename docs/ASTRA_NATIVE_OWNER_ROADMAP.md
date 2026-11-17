# Noveria Roadmap: единый владелец native-хранилища

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
