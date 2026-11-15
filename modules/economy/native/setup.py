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
         # Единый контейнер состояния: держит ссылки на существующие
         # OrderStore/CashStore и native update state, собственных данных
         # экономики не заводит и потому не зависит от прочих расширений.
         Extension('world_state', ['world_state.pyx'])],
        compiler_directives={'language_level': 3},
    ),
)
