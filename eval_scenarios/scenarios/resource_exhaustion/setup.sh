#!/usr/bin/env bash
set -euo pipefail
FIXTURE_DIR="$(cd "$(dirname "$0")/fixtures" && pwd)"
NS="ml-serving"

oc apply -f "$FIXTURE_DIR/manifest.yaml"
oc wait --for=condition=available deployment/model-server -n "$NS" --timeout=60s

ATTEMPT=0
until [ "$ATTEMPT" -ge 20 ]; do
  ATTEMPT=$((ATTEMPT + 1))
  if oc logs -l app=model-server -n "$NS" --tail=5 2>/dev/null | grep -q "OutOfMemoryError"; then
    echo "Resource exhaustion logs detected (attempt $ATTEMPT)"
    exit 0
  fi
  sleep 3
done
echo "Expected logs not detected"
exit 1
