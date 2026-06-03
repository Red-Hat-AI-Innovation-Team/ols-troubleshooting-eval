#!/usr/bin/env bash
set -euo pipefail

# Deploy config_drift_analysis scenario once before running evals.
# Run this BEFORE starting eval. Clean up with cleanup_config_drift.sh AFTER.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCENARIO_DIR="$SCRIPT_DIR/eval_scenarios/scenarios/config_drift_analysis"

oc delete ns ingress-layer --ignore-not-found 2>/dev/null; sleep 5
oc create namespace ingress-layer
oc create secret docker-registry dockerhub \
    --docker-server=docker.io \
    --docker-username="${DOCKERHUB_USER:-}" \
    --docker-password="${DOCKERHUB_TOKEN:-}" \
    -n ingress-layer --dry-run=client -o yaml | oc apply -f -
oc secrets link default dockerhub --for=pull -n ingress-layer
bash "$SCENARIO_DIR/setup.sh" || echo "Setup exited non-zero (expected)"
