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
         Extension('offer_view', ['offer_view.pyx'])],
        compiler_directives={'language_level': 3},
    ),
)
