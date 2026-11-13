#!/usr/bin/env sh
# Требуется C/C++ компилятор для сборки native-расширения serial_step:
#   Linux   — build-essential (gcc, g++).
#   macOS   — Xcode Command Line Tools (xcode-select --install).
# Сам `uv sync` компилятор не требует: он ставит только Python-пакеты.
# Сборка расширения запускается отдельно в modules/economy/native.
#
# Воспроизводимое окружение Noveria через uv (Linux/macOS).
# Аналог setup.ps1 для Windows.

set -eu

cd "$(dirname "$0")"

printf '%s\n' '=== Noveria: подготовка окружения через uv ==='

# 1. Проверяем наличие uv.
if ! command -v uv >/dev/null 2>&1; then
    printf '\n'
    printf '%s\n' 'uv не найден.'
    printf '%s\n' 'Установите uv одной из команд:'
    printf '\n'
    printf '%s\n' '  curl -LsSf https://astral.sh/uv/install.sh | sh'
    printf '%s\n' '  python3 -m pip install --user uv'
    printf '\n'
    printf '%s\n' 'После установки перезапустите терминал (убедитесь, что ~/.local/bin в PATH)'
    printf '%s\n' 'и запустите этот скрипт снова.'
    exit 1
fi

printf '%s\n' "uv найден: $(command -v uv)"

# 2. Синхронизируем окружение по uv.lock и .python-version.
printf '\n'
printf '%s\n' 'Выполняю: uv sync'
if ! uv sync; then
    printf '\n'
    printf '%s\n' 'uv sync завершился с ошибкой. См. вывод выше.'
    printf '%s\n' 'Если не удалось скачать Python 3.12, выполните: uv python install 3.12'
    exit 1
fi

# 3. Печатаем команду активации.
printf '\n'
printf '%s\n' '=== Готово ==='
printf '%s\n' 'Активация окружения в bash/zsh:'
printf '\n'
printf '%s\n' '  source .venv/bin/activate'
printf '\n'
printf '%s\n' 'Без активации команды запускаются через префикс uv run:'
printf '%s\n' '  uv run python -m pytest -q tests/test_economy_core_api.py'
printf '\n'
printf '%s\n' 'Native-расширение serial_step требует установленный C/C++ компилятор:'
printf '%s\n' '  Linux — build-essential; macOS — Xcode Command Line Tools.'
printf '%s\n' 'Сборка (по желанию, отдельным шагом):'
printf '%s\n' '  uv run python modules/economy/native/setup.py build_ext --inplace'
printf '\n'
