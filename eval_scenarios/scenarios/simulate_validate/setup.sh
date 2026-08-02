#!/usr/bin/env bash
set -euo pipefail

FIXTURE_DIR="$(cd "$(dirname "$0")/fixtures" && pwd)"
NS="simulate-test"
APP="nginx-app"

oc apply -f "$FIXTURE_DIR/deployment.yaml"

# Wait for the pod to appear
echo "Waiting for $APP pod to be created..."
ATTEMPT=0
until [ "$ATTEMPT" -ge 60 ]; do
  ATTEMPT=$((ATTEMPT + 1))
  POD=$(oc get pods -n "$NS" -l "app=$APP" -o name 2>/dev/null | head -1)
  [ -n "$POD" ] && break
  sleep 5
done
[ -n "${POD:-}" ] || { echo "$APP pod never appeared"; oc get pods -n "$NS"; exit 1; }

# Wait for CrashLoopBackOff (the missing APP_CONFIG_PATH env var causes exit 1)
echo "Pod exists -- waiting for CrashLoopBackOff..."
oc wait --for=jsonpath='{.status.containerStatuses[0].state.waiting.reason}'=CrashLoopBackOff \
  pod -l "app=$APP" -n "$NS" --timeout=300s
