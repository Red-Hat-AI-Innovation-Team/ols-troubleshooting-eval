#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OLS_DIR="${OLS_DIR:-$(cd "$SCRIPT_DIR/../lightspeed-service" 2>/dev/null && pwd)}"
bash "$OLS_DIR/eval/troubleshooting/scenarios/config_drift_analysis/cleanup.sh" 2>/dev/null || true
echo "config_drift cleaned up"
