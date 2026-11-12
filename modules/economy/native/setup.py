"""Воспроизводимая локальная сборка serial_step на Windows и Linux."""
from Cython.Build import cythonize
from setuptools import Extension, setup


setup(
    name='noveria-native-serial-step',
    ext_modules=cythonize(
        [Extension('serial_step', ['serial_step.pyx'])],
        compiler_directives={'language_level': 3},
    ),
)
