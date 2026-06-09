#!/usr/bin/env bash
set -euo pipefail

FIXTURE_DIR="$(cd "$(dirname "$0")/fixtures" && pwd)"
NS="data-pipeline"
APP="batch-processor"

oc create namespace "$NS" 2>/dev/null || true

oc create secret generic batch-processor-logs-script \
  --from-file=generate_logs.py="$FIXTURE_DIR/generate_logs.py" \
  -n "$NS" --dry-run=client -o yaml | oc apply -f -
oc apply -f "$FIXTURE_DIR/manifest.yaml"

# Wait for pod ready, then verify log sentinels exist.
# The log generator prints all lines immediately on startup,
# so once the pod is ready the sentinels are already there.
oc wait --for=condition=ready pod -l "app=$APP" -n "$NS" --timeout=240s

LOGS=$(oc logs -l "app=$APP" -n "$NS" --tail=10000 2>/dev/null || true)
if echo "$LOGS" | grep -q "Detected repeated failures during 03:00-03:05 window" \
&& echo "$LOGS" | grep -q "System health check passed" \
&& echo "$LOGS" | grep -q "Job executed successfully in 167ms\."; then
  echo "All sentinels found"
  exit 0
fi

echo "ERROR: Pod ready but sentinels not found in logs"
oc logs -l "app=$APP" -n "$NS" --tail=30 2>/dev/null || true
exit 1
