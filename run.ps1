param([switch]$Seed, [switch]$Test, [switch]$Lab)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv312/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Project Python is missing. See README.md: Windows environment.'
}
if ($Seed) { & $projectPython scripts/seed_world.py }
elseif ($Test) { & $projectPython -m pytest -q }
elseif ($Lab) { & $projectPython -m streamlit run lab.py }
else { & $projectPython -m streamlit run main.py }
exit $LASTEXITCODE
