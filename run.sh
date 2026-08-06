#!/usr/bin/env bash
# AEGIS-ER quick-start script for macOS / Linux
set -euo pipefail
cd "$(dirname "$0")"

GREEN='\033[0;32m'; YEL='\033[0;33m'; CYN='\033[0;36m'; RST='\033[0m'

echo -e "${CYN}============================================"
echo "  AEGIS-ER — Emergency Response Platform"
echo -e "============================================${RST}"
echo

command -v python3 >/dev/null || { echo -e "${RED}python3 not found. Install Python 3.10+ first.${RST}"; exit 1; }

echo -e "${YEL}[1/4] Creating virtual environment...${RST}"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo -e "${YEL}[2/4] Installing dependencies...${RST}"
pip install -q --upgrade pip
pip install -q -e 'libs/aegis[server]'
pip install -q -r services/assignment-solver/requirements.txt

echo -e "${YEL}[3/4] Starting server on http://localhost:8000 ...${RST}"
export PYTHONPATH=libs/aegis
export AEGIS_SIMULATOR=false
export AEGIS_DASHBOARD_DIR=services/dashboard

echo
echo -e "${GREEN}[4/4] Ready. Dashboard will open at http://localhost:8000/${RST}"
echo -e "         API docs: http://localhost:8000/docs"
echo -e "         Press Ctrl+C to stop."
echo

( sleep 5; xdg-open "http://localhost:8000/" >/dev/null 2>&1 || open "http://localhost:8000/" >/dev/null 2>&1 || true ) &

exec python services/assignment-solver/app.py
