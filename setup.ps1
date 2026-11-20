$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $command = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if ($command) {
        $uv = $command.Source
    } else {
        $uv = Join-Path $PSScriptRoot '.noveria-tools\uv.exe'
        if (!(Test-Path -LiteralPath $uv)) {
            $installer = [IO.Path]::GetTempFileName()
            $previous = $env:UV_UNMANAGED_INSTALL
            try {
                $env:UV_UNMANAGED_INSTALL = Split-Path $uv
                Invoke-WebRequest 'https://astral.sh/uv/install.ps1' -UseBasicParsing -OutFile $installer
                & ([scriptblock]::Create([IO.File]::ReadAllText($installer)))
            } finally {
                $env:UV_UNMANAGED_INSTALL = $previous
                Remove-Item -LiteralPath $installer
            }
        }
    }
    $cache = Join-Path $PSScriptRoot '.noveria-tools\uv-cache'
    & $uv sync --locked --cache-dir $cache
    if ($LASTEXITCODE) { throw 'uv sync failed' }
    & $uv pip install --cache-dir $cache --python .venv/Scripts/python.exe -r requirements-ui.txt -r requirements-research.txt
    if ($LASTEXITCODE) { throw 'Dependency installation failed' }
    Write-Host 'Start the map server: .\.venv\Scripts\python.exe scripts\godot_server.py'
} finally {
    Pop-Location
}
