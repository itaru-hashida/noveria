# Требуется C/C++ компилятор для сборки native-расширения serial_step:
#   Windows — "Desktop development with C++" в Visual Studio Build Tools.
#   Linux   — build-essential (gcc, g++).
# Сам `uv sync` компилятор не требует: он ставит только Python-пакеты.
# Сборка расширения запускается отдельно в modules/economy/native.

# Воспроизводимое окружение Noveria через uv (Windows PowerShell).
# Аналог setup.sh для Linux/macOS.

$ErrorActionPreference = 'Stop'

Set-Location -Path $PSScriptRoot

Write-Host '=== Noveria: подготовка окружения через uv ===' -ForegroundColor Cyan

# 1. Проверяем наличие uv.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host ''
    Write-Host 'uv не найден.' -ForegroundColor Yellow
    Write-Host 'Установите uv одной из команд:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  winget install --id=astral-sh.uv -e' -ForegroundColor White
    Write-Host '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' -ForegroundColor White
    Write-Host '  python -m pip install --user uv' -ForegroundColor White
    Write-Host ''
    Write-Host 'После установки перезапустите терминал (нужен обновлённый PATH)' -ForegroundColor Yellow
    Write-Host 'и запустите этот скрипт снова.' -ForegroundColor Yellow
    exit 1
}

Write-Host ("uv найден: {0}" -f $uv.Source) -ForegroundColor Green

# 2. Синхронизируем окружение по uv.lock и .python-version.
Write-Host ''
Write-Host 'Выполняю: uv sync' -ForegroundColor Cyan
& uv sync
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'uv sync завершился с ошибкой. См. вывод выше.' -ForegroundColor Red
    Write-Host 'Если не удалось скачать Python 3.12, выполните: uv python install 3.12' -ForegroundColor Yellow
    exit $LASTEXITCODE
}

# 3. Печатаем команду активации.
Write-Host ''
Write-Host '=== Готово ===' -ForegroundColor Green
Write-Host 'Активация окружения в PowerShell:' -ForegroundColor Cyan
Write-Host ''
Write-Host '  .\.venv\Scripts\Activate.ps1' -ForegroundColor White
Write-Host ''
Write-Host 'Если PowerShell блокирует скрипт активации:' -ForegroundColor Cyan
Write-Host '  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass' -ForegroundColor White
Write-Host ''
Write-Host 'Без активации команды запускаются через префикс uv run:' -ForegroundColor Cyan
Write-Host '  uv run python -m pytest -q tests/test_economy_core_api.py' -ForegroundColor White
Write-Host ''
Write-Host 'Native-расширение serial_step требует установленный C/C++ компилятор:' -ForegroundColor Yellow
Write-Host '  Visual Studio Build Tools с рабочей нагрузкой "Desktop development with C++".' -ForegroundColor Yellow
Write-Host 'Сборка (по желанию, отдельным шагом):' -ForegroundColor Cyan
Write-Host '  uv run python modules/economy/native/setup.py build_ext --inplace' -ForegroundColor White
Write-Host ''
