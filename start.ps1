# ─────────────────────────────────────────────────────────────────────────────
# start.ps1 — LedgerFlow Startup Script (Windows PowerShell)
# Razorpay AI Buildathon 2026 | Track 4: AI Finance Controller
#
# Usage (from the LedgerFlow project root):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\start.ps1
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

# ── Colours ───────────────────────────────────────────────────────────────────
function Write-Step  { param($msg) Write-Host "`n▶  $msg" -ForegroundColor Cyan }
function Write-Ok    { param($msg) Write-Host "✔  $msg"  -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "⚠  $msg"  -ForegroundColor Yellow }
function Write-Die   { param($msg) Write-Host "✖  $msg"  -ForegroundColor Red; exit 1 }

# ─────────────────────────────────────────────────────────────────────────────
# 0. Banner
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ┌────────────────────────────────────────────────────┐" -ForegroundColor White
Write-Host "  │           LedgerFlow — Startup Script              │" -ForegroundColor White
Write-Host "  │    Razorpay AI Buildathon 2026 | Track 4           │" -ForegroundColor White
Write-Host "  └────────────────────────────────────────────────────┘" -ForegroundColor White
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Resolve Python interpreter
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Locating Python interpreter"

$Python = $null
foreach ($cmd in @("python", "python3")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        $Python = $cmd
        break
    }
}

if (-not $Python) {
    Write-Die "Python not found. Install Python 3.10+ and ensure it is on PATH."
}

$pyVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Ok "Using $Python ($pyVersion)"

# Minimum version guard
$pyMajor = [int](& $Python -c "import sys; print(sys.version_info.major)")
$pyMinor = [int](& $Python -c "import sys; print(sys.version_info.minor)")
if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 10)) {
    Write-Die "Python 3.10+ is required (found $pyVersion)."
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. Verify project root
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Checking project root"

if (-not (Test-Path "requirements.txt"))     { Write-Die "requirements.txt not found. Run from the LedgerFlow project root." }
if (-not (Test-Path "generate_mock_data.py")){ Write-Die "generate_mock_data.py not found." }
if (-not (Test-Path "main.py"))              { Write-Die "main.py not found." }

Write-Ok "Project files found"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Install Python dependencies
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Installing dependencies from requirements.txt"

& $Python -m pip install --upgrade pip --quiet
& $Python -m pip install -r requirements.txt --quiet

Write-Ok "Dependencies installed"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Generate mock data (skip if CSVs already exist)
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Generating mock CSV data"

$erpCsv  = "data\mock\erp_ledger.csv"
$gwCsv   = "data\mock\gateway_settlement.csv"
$bankCsv = "data\mock\bank_statement.csv"

if ((Test-Path $erpCsv) -and (Test-Path $gwCsv) -and (Test-Path $bankCsv)) {
    Write-Warn "Mock CSVs already exist — skipping generation. Delete data\mock\ to regenerate."
} else {
    & $Python generate_mock_data.py
    Write-Ok "Mock data written to data\mock\"
}

# ─────────────────────────────────────────────────────────────────────────────
# 5. Port availability check
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Checking port 8000 availability"

$port = 8000
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Die "Port $port is already in use. Stop the existing process and try again."
}
Write-Ok "Port $port is free"

# ─────────────────────────────────────────────────────────────────────────────
# 6. Start the FastAPI backend
# ─────────────────────────────────────────────────────────────────────────────
Write-Step "Starting LedgerFlow API server"

Write-Host ""
Write-Host "  Server:    http://localhost:$port"     -ForegroundColor White
Write-Host "  Swagger:   http://localhost:$port/docs" -ForegroundColor White
Write-Host "  ReDoc:     http://localhost:$port/redoc" -ForegroundColor White
Write-Host "  Dashboard: Open index.html in your browser" -ForegroundColor White
Write-Host ""
Write-Host "  Press Ctrl+C to stop." -ForegroundColor Gray
Write-Host ""

& $Python -m uvicorn main:app `
    --host 0.0.0.0 `
    --port $port `
    --reload `
    --log-level info
