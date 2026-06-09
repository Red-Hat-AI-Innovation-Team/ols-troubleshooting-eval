#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$SCRIPT_DIR/eval_scenarios/scenarios/config_drift_analysis/cleanup.sh" 2>/dev/null || true
echo "config_drift cleaned up"
