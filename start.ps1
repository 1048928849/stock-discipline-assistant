param(
    [switch]$SkipInstall
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath ".venv")) {
    python -m venv .venv
}
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not $SkipInstall) {
    & $PythonExe -m pip install -e ".[dev]"
}
& $PythonExe -m alembic upgrade head
& $PythonExe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

