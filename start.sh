#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh — LedgerFlow Startup Script
# Razorpay AI Buildathon 2026 | Track 4: AI Finance Controller
#
# Usage:
#   chmod +x start.sh
#   ./start.sh
#
# Works with: Git Bash (Windows), WSL, macOS, Linux
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail   # Exit on error, undefined var, or pipe failure

# ── Colours ──────────────────────────────────────────────────────────────────
BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
RESET="\033[0m"

# ── Helpers ───────────────────────────────────────────────────────────────────
step()  { echo -e "\n${CYAN}${BOLD}▶  $*${RESET}"; }
ok()    { echo -e "${GREEN}✔  $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠  $*${RESET}"; }
die()   { echo -e "${RED}✖  $*${RESET}" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
# 0. Banner
# ─────────────────────────────────────────────────────────────────────────────
echo -e "${BOLD}"
echo "  ┌────────────────────────────────────────────────────┐"
echo "  │           LedgerFlow — Startup Script              │"
echo "  │    Razorpay AI Buildathon 2026 | Track 4           │"
echo "  └────────────────────────────────────────────────────┘"
echo -e "${RESET}"

# ─────────────────────────────────────────────────────────────────────────────
# 1. Resolve Python interpreter
# ─────────────────────────────────────────────────────────────────────────────
step "Locating Python interpreter"

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

[[ -z "$PYTHON" ]] && die "Python not found. Please install Python 3.10+ and try again."

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
ok "Using $PYTHON ($PY_VERSION)"

# Minimum version check
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
if [[ "$PY_MAJOR" -lt 3 || ("$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10) ]]; then
    die "Python 3.10+ is required (found $PY_VERSION)."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. Verify script is run from the project root
# ─────────────────────────────────────────────────────────────────────────────
step "Checking project root"

[[ -f "requirements.txt" ]]       || die "requirements.txt not found. Run this script from the LedgerFlow project root."
[[ -f "generate_mock_data.py" ]]  || die "generate_mock_data.py not found."
[[ -f "main.py" ]]                || die "main.py not found."

ok "Project files found"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Install Python dependencies
# ─────────────────────────────────────────────────────────────────────────────
step "Installing dependencies from requirements.txt"

"$PYTHON" -m pip install --upgrade pip --quiet
"$PYTHON" -m pip install -r requirements.txt --quiet

ok "Dependencies installed"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Generate mock data (skip if CSVs already exist)
# ─────────────────────────────────────────────────────────────────────────────
step "Generating mock CSV data"

ERP_CSV="data/mock/erp_ledger.csv"
GW_CSV="data/mock/gateway_settlement.csv"
BANK_CSV="data/mock/bank_statement.csv"

if [[ -f "$ERP_CSV" && -f "$GW_CSV" && -f "$BANK_CSV" ]]; then
    warn "Mock CSVs already exist — skipping generation. Delete data/mock/ to regenerate."
else
    "$PYTHON" generate_mock_data.py
    ok "Mock data written to data/mock/"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 5. Port availability check
# ─────────────────────────────────────────────────────────────────────────────
step "Checking port 8000 availability"

PORT=8000
if command -v lsof &>/dev/null; then
    if lsof -Pi ":$PORT" -sTCP:LISTEN -t &>/dev/null; then
        die "Port $PORT is already in use. Stop the existing process and try again."
    fi
elif command -v ss &>/dev/null; then
    if ss -tlnp | grep -q ":$PORT "; then
        die "Port $PORT is already in use. Stop the existing process and try again."
    fi
else
    warn "Cannot check port availability (lsof/ss not found) — proceeding anyway."
fi

ok "Port $PORT is free"

# ─────────────────────────────────────────────────────────────────────────────
# 6. Start the FastAPI backend
# ─────────────────────────────────────────────────────────────────────────────
step "Starting LedgerFlow API server"

echo ""
echo -e "  ${BOLD}Server:${RESET}    http://localhost:$PORT"
echo -e "  ${BOLD}Swagger:${RESET}   http://localhost:$PORT/docs"
echo -e "  ${BOLD}ReDoc:${RESET}     http://localhost:$PORT/redoc"
echo -e "  ${BOLD}Dashboard:${RESET} Open index.html in your browser"
echo ""
echo -e "  Press ${BOLD}Ctrl+C${RESET} to stop."
echo ""

"$PYTHON" -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --reload \
    --log-level info
