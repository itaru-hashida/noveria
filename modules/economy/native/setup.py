"""Воспроизводимая локальная сборка native-расширений на Windows и Linux."""
from Cython.Build import cythonize
from setuptools import Extension, setup


setup(
    name='noveria-native-serial-step',
    ext_modules=cythonize(
        [Extension('serial_step', ['serial_step.pyx']),
         # Отдельное расширение: stock-операции не должны тянуть serial_step
         # и обязаны собираться независимо от него.
         Extension('stock_kernel', ['stock_kernel.pyx']),
         Extension('stock_store', ['stock_store.pyx']),
         Extension('enterprise_state_kernel', ['enterprise_state_kernel.pyx']),
         # Представление офферов тоже отдельное: оно читает книгу движка и не
         # зависит ни от stock-ядра, ни от участка update_prices.
         Extension('offer_view', ['offer_view.pyx']),
         # Граница мутаций заявок: хранит живой список orders и не зависит ни
         # от представления, ни от stock-ядра.
         Extension('order_store', ['order_store.pyx']),
         # Денежные колонки счетов: хранит живой словарь accounts и не зависит
         # ни от заявок, ни от stock-ядра, ни от проектов.
         Extension('cash_store', ['cash_store.pyx']),
         # Исполнение одной уже выбранной сделки: получает ссылки на движок и
         # его границы, собственных полей не хранит и от прочих ядер не зависит.
         Extension('trade_kernel', ['trade_kernel.pyx']),
         # Внутренний цикл одного buyer order: обходит готовый OfferView и
         # вызывает переданный commit_trade. Порядок покупателей и книга
         # остаются в Python, поэтому ядро не зависит от прочих расширений.
         Extension('match_kernel', ['match_kernel.pyx']),
         # Внешний цикл покупателей: обходит уже перемешанный список заявок и
         # вызывает прежние хуки движка и внутренний цикл по книге. Порядок
         # покупателей, RNG и границы остаются за Python, поэтому ядро не
         # зависит от прочих расширений.
         Extension('match_driver', ['match_driver.pyx']),
         # Резерв строительных материалов: получает уже выбранный проект и его
         # смету, работает с живым складом счёта и вызывает существующую
         # границу заявок. Порядок проектов, отмены и деньги остаются за
         # Python, поэтому ядро не зависит от прочих расширений.
         Extension('material_kernel', ['material_kernel.pyx']),
         # Доставка строительных материалов: считает объём и стоимость
         # перевозки и пишет project['delivery']. Материалы, склад и резерв
         # остаются за material_kernel, деньги и заявки — за Python, поэтому
         # ядро не зависит от прочих расширений.
         Extension('delivery_kernel', ['delivery_kernel.pyx']),
         # Отмена проекта: stall-срез считает прогресс и порог простоя, а
         # срез маржи/спроса — пороги маржи и отсутствия спроса, условие
         # отмены и её причину. Снятие проекта, protected, отчёт и заявки
         # остаются за Python, поэтому ядро не зависит от прочих расширений.
         Extension('cancel_kernel', ['cancel_kernel.pyx']),
         # Release sell orders: снятие закреплённой части оффера и отмена
         # опустевшего. Обход заявок, склад, резерв и деньги остаются за
         # Python, поэтому ядро не зависит от прочих расширений.
         Extension('release_kernel', ['release_kernel.pyx']),
         # Строительные buy orders: цена-потолок из котировки и выставление
         # закупки на недостающий материал через существующий order hook.
         # Обход сметы, склад, резерв и деньги остаются за Python, поэтому
         # ядро не зависит от прочих расширений.
         Extension('construction_buy_kernel', ['construction_buy_kernel.pyx']),
         # Финализация material request: затронутые товары и готовые
         # journal-строки по уже посчитанной смете. Порядок проектов, отчёт и
         # dirty-хуки остаются за Python, поэтому ядро не зависит от прочих
         # расширений.
         Extension('material_finalize_kernel', ['material_finalize_kernel.pyx']),
         # Числовой цикл production: обходит уже перемешанный список
         # предприятий, считает ограничения, выпуск и стоимость. RNG, порядок
         # предприятий, склад и side effects остаются за Python, поэтому ядро
         # не зависит от прочих расширений.
         Extension('production_kernel', ['production_kernel.pyx']),
         # Числовой цикл consumption домохозяйств: обходит готовый список
         # домохозяйств и их needs, считает min(needed, free_stock). Склад,
         # need, отчёт и порядок обхода остаются за Python, поэтому ядро не
         # зависит от прочих расширений.
         Extension('consumption_kernel', ['consumption_kernel.pyx']),
         # Числовой срез owner consumption: обход строк отчёта и книги заявок,
         # отметка consumed_quantity, три накопления и итоги строки. Склад
         # остаётся за Python-хуком через существующую stock boundary, поэтому
         # ядро не зависит от прочих расширений и не заводит второго ledger.
         Extension('owner_consumption_kernel', ['owner_consumption_kernel.pyx']),
         # Числовой цикл net_price: обход готового списка котировок покупателей,
         # сравнение с ценой, расчёт landed/volume, накопления и share. Книга
         # заявок и customer_quotes остаются за Python-хуком, поэтому ядро не
         # зависит от прочих расширений.
         Extension('net_price_kernel', ['net_price_kernel.pyx']),
         # Числовой срез прогноза выпуска: средние продажи, прогноз спроса,
         # остаток склада, постепенное сокращение мощности, fixed_unit, маржа,
         # причина предела и итоговый выпуск. unit_cost, net_price,
         # reachable_demand, expected_stock_sales, оброк и кэш прогноза
         # остаются за Python-хуками, поэтому ядро не зависит от прочих
         # расширений и не заводит второго order/cash ledger.
         Extension('production_outlook_kernel', ['production_outlook_kernel.pyx']),
         # Рыночная книга: достижимые офферы, landed price, потоки площадок и
         # точная сортировка по (landed_price, sell_id). Маршруты, заявки,
         # представление и отчёт остаются за Python-хуками, поэтому ядро не
         # зависит от прочих расширений и не заводит второго order ledger.
         Extension('market_book_kernel', ['market_book_kernel.pyx']),
         # Числовой цикл household_top_up: отбор кандидатов, обход товаров и
         # книги офферов, расчёт missing/quantity. RNG, заявки, сделки, отчёт
         # и dirty-хуки остаются за Python, поэтому ядро не зависит от прочих
         # расширений и не заводит второго cash/stock ledger.
         Extension('topup_kernel', ['topup_kernel.pyx']),
         # Read-only цикл валидации счетов и населения: численность групп,
         # агрегаты переселения, инварианты кассы и резерва товаров. Ядро
         # ничего не мутирует, точные тексты ошибок и отметки инвариантов
         # остаются за Python, поэтому ядро не зависит от прочих расширений.
         Extension('settlement_kernel', ['settlement_kernel.pyx']),
         # Числовой срез дивидендов: рабочий резерв, доступная касса, предел и
         # доли. Выплаты идут прежним transfer, отчёт и события остаются за
         # Python, поэтому ядро не зависит от прочих расширений и не заводит
         # второго cash ledger.
         Extension('dividend_kernel', ['dividend_kernel.pyx']),
         # Зарплатный срез публичных строек: готовность, число работников,
         # wage bill и надбавка за жильё. Выплаты идут прежним distribute,
         # склад, износ, отчёт, завершение стройки и порядок счетов остаются за
         # Python, поэтому ядро не зависит от прочих расширений и не заводит
         # второго cash ledger.
         Extension('wage_kernel', ['wage_kernel.pyx']),
         # Единый контейнер состояния: держит ссылки на существующие
         # OrderStore/CashStore и native update state, собственных данных
         # экономики не заводит и потому не зависит от прочих расширений.
         Extension('world_state', ['world_state.pyx'])],
        compiler_directives={'language_level': 3},
    ),
)
