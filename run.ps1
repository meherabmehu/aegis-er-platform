# AEGIS-ER quick-start for Windows PowerShell
# Usage: right-click this file -> "Run with PowerShell", OR cd to this folder and run: .\run.ps1
# Encoding: UTF-8

# Auto-navigate to the directory this script lives in, regardless of where you invoke it from
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
Write-Host ""
Write-Host "Working directory: $ScriptDir" -ForegroundColor DarkGray

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  AEGIS-ER - Emergency Response Platform" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Find Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "[ERROR] Python not found. Install from https://python.org (check 'Add Python to PATH') and re-run." -ForegroundColor Red
    Write-Host "Press any key to exit..." -ForegroundColor DarkGray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

Write-Host "[1/5] Checking Python... $($py.Source)" -ForegroundColor Yellow
& $py.Source --version

Write-Host "[2/5] Creating virtual environment (.venv)..." -ForegroundColor Yellow
if (-not (Test-Path ".venv")) {
    & $py.Source -m venv .venv
}
# Activate venv
$activate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $activate) {
    & $activate
} else {
    Write-Host "[WARN] .venv activation script not found, continuing with system pip" -ForegroundColor DarkYellow
}

Write-Host "[3/5] Installing dependencies..." -ForegroundColor Yellow
python -m pip install -q --upgrade pip
python -m pip install -q -e libs\aegis[server]
python -m pip install -q -r services\assignment-solver\requirements.txt

Write-Host "[4/5] Setting environment variables..." -ForegroundColor Yellow
$env:PYTHONPATH = "libs/aegis"
$env:AEGIS_SIMULATOR = "false"
$env:AEGIS_DASHBOARD_DIR = "services/dashboard"

Write-Host ""
Write-Host "[5/5] Ready. Dashboard: http://localhost:8000/   (click 'Start Disaster')" -ForegroundColor Green
Write-Host "      API docs: http://localhost:8000/docs" -ForegroundColor White
Write-Host "      Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""
Write-Host "Opening browser in 5 seconds..." -ForegroundColor DarkGray

Start-Job -ScriptBlock {
    param($dir)
    Start-Sleep -Seconds 5
    Start-Process "http://localhost:8000/"
} -ArgumentList $ScriptDir | Out-Null

python services\assignment-solver\app.py

Write-Host ""
Write-Host "Server stopped. Press any key to exit..." -ForegroundColor DarkGray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
