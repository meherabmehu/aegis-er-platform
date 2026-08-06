#!/usr/bin/env bash
# Convenience launcher for local development. Starts the API + dashboard on :8000.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="libs/aegis:${PYTHONPATH:-}"
export AEGIS_SIMULATOR="${AEGIS_SIMULATOR:-true}"
export AEGIS_TICK_MS="${AEGIS_TICK_MS:-250}"
export AEGIS_REOPTIMIZE_MS="${AEGIS_REOPTIMIZE_MS:-500}"
export AEGIS_SOLVER_BUDGET_MS="${AEGIS_SOLVER_BUDGET_MS:-200}"
export AEGIS_DASHBOARD_DIR="services/dashboard"
exec python services/assignment-solver/app.py
