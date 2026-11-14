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
         Extension('trade_kernel', ['trade_kernel.pyx'])],
        compiler_directives={'language_level': 3},
    ),
)
