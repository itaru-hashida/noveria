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
    Write-Host 'uv не найден. Пытаюсь установить автоматически...' -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    } else {
        $python = Get-Command py -ErrorAction SilentlyContinue
        if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
        if (-not $python) {
            throw 'Не найден ни winget, ни Python. Установите Python 3.12+ с python.org и запустите setup.ps1 снова.'
        }
        & $python.Source -m pip install --user uv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Автоматическая установка uv завершилась ошибкой.' }
    # winget/pip обновляют PATH только для новых процессов; добавляем типичные каталоги
    # в текущий процесс, чтобы не требовать перезапуск PowerShell.
    $userBin = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
    $userBin2 = Join-Path $env:APPDATA 'Python\Python312\Scripts'
    $env:Path = "$userBin;$userBin2;$env:Path"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) { throw 'uv установлен, но не найден в PATH. Закройте PowerShell, откройте заново и запустите setup.ps1.' }
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
