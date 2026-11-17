# Архитектурное ревью Астры

## Рекомендация

Делать постоянное native-состояние стоит; переносить весь проект в Cython ради отсутствия упаковки — нет.

Упаковка исчезает, когда производители и потребители используют одно авторитетное представление. Простое помещение большего числа объектов или orchestration в `.pyx` само по себе этого не делает.

## Что есть сейчас

- `WorldState` связывает существующие хранилища, но не является авторитетным числовым миром.
- `CashStore` и `OrderStore` используют Python-объекты и совместимость с внешними dictionary writes.
- `StockStore` ссылается на словари складов.
- `monthly_driver.pyx` остаётся оркестратором: сортирует словари, сбрасывает report и вызывает Python boundaries.
- Native buffers переиспользуются частично, но динамические значения всё ещё заново заполняются.
- Инвестиции работают по схеме `pack → calculate → apply`.

## Целевая архитектура

Native core должен владеть балансами, запасами, оценкой, резервами, заявками, состоянием предприятий, проектами и инвестиционными очередями. Python должен отвечать за API, authored content, структурные операции БД, отчёты, протоколы, checkpoints и orchestration загрузки/сохранения.

Между ними нужны команды, batch snapshots и события. Нужны стабильные entity handles, отдельные массивы детерминированного порядка, versioned derived indexes и повторно используемые scratch buffers. Compatibility adapters должны направлять записи владельцу и постепенно становиться read-only.

## Риски

- порядок инвесторов, RNG, tie-breaking и порядок арифметики должны остаться детерминированными;
- native arrays должны полностью участвовать в save/load и rollback;
- нужны правила slot allocation, deletion, reuse и lifetime memoryviews;
- dense account×goods и tile×tile структуры могут увеличить память;
- persistent state не делает последовательные economic commits безопасными для параллельного изменения.

## Этапы

1. Инвентаризировать всех writers каждого поля и разделить timing на conversion, derived-data construction, numeric execution, apply, reporting и rollback.
2. Зафиксировать layout, ownership APIs, structural/content revisions и reusable buffers.
3. Завершить один market/ledger slice: cash, stock, reservations, orders, trade application, valuation и protected cash.
4. Подключить investment и construction к тем же stores, включая material requests и project completion.
5. Переносить production, settlement, demography и planning только там, где измерения показывают отдачу.

## Критерий принятия

Крупный перенос принимается при повторяемом end-to-end улучшении хотя бы на 10% либо при доказанном улучшении peak memory или tail latency. Нужны exact state/decision/RNG replay, money/goods/reservation invariants, save/load continuation и injected-failure rollback.

Это ревью Астры по текущему состоянию репозитория; отдельный roadmap с decision trees будет сохранён рядом.
