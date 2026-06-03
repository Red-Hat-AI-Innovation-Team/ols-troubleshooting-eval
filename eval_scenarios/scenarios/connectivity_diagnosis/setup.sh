#!/usr/bin/env bash
set -euo pipefail
FIXTURE_DIR="$(cd "$(dirname "$0")/fixtures" && pwd)"
NS="connectivity-test"

oc apply -f "$FIXTURE_DIR/manifest.yaml"
oc wait --for=condition=available deployment/api-backend -n "$NS" --timeout=60s
oc wait --for=condition=available deployment/web-client -n "$NS" --timeout=60s

# Wait for error logs to appear
ATTEMPT=0
until [ "$ATTEMPT" -ge 30 ]; do
  ATTEMPT=$((ATTEMPT + 1))
  if oc logs -l app=web-client -n "$NS" --tail=5 2>/dev/null | grep -q "ERROR"; then
    echo "Client error logs detected (attempt $ATTEMPT)"
    exit 0
  fi
  sleep 3
done
echo "No error logs found within timeout"
exit 1
