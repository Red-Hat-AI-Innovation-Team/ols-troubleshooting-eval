#!/usr/bin/env bash
# Manual validation script for the simulate_fix MCP tool.
#
# Prerequisites:
#   - Docker running (K3d needs Docker)
#   - K3d installed (brew install k3d / https://k3d.io)
#   - Python venv activated with simulate_mcp installed
#   - No existing process on port 8086
#
# Usage:
#   ./scripts/test_simulate.sh

set -euo pipefail

PORT=8086
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Stopping MCP server (PID $SERVER_PID)..."
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "=== Starting simulate MCP server on port $PORT ==="
python -m simulate_mcp &
SERVER_PID=$!

echo "Waiting for server to be ready..."
for i in $(seq 1 30); do
    if curl -sf "http://localhost:$PORT/sse" -o /dev/null 2>/dev/null; then
        echo "Server is ready (attempt $i)"
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "ERROR: Server process exited unexpectedly"
        exit 1
    fi
    sleep 1
done

if ! curl -sf "http://localhost:$PORT/sse" -o /dev/null 2>/dev/null; then
    echo "ERROR: Server did not become ready within 30 seconds"
    exit 1
fi

echo ""
echo "=== Sending simulate_fix request (envvar_missing scenario) ==="
echo "Fix: add DEPLOY_ENV=production to order-fulfillment-daemon in warehouse-ops"
echo ""

RESPONSE=$(curl -sf -X POST "http://localhost:$PORT/messages" \
    -H "Content-Type: application/json" \
    -d '{
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "simulate_fix",
            "arguments": {
                "commands": [
                    "kubectl set env deploy/order-fulfillment-daemon DEPLOY_ENV=production -n warehouse-ops"
                ],
                "manifests": [],
                "fault_description": "DEPLOY_ENV environment variable undefined",
                "target_namespaces": ["warehouse-ops"]
            }
        }
    }' 2>&1) || {
    echo "ERROR: Failed to send request to MCP server"
    echo "Response: $RESPONSE"
    exit 1
}

echo "=== Response ==="
echo "$RESPONSE" | python -m json.tool 2>/dev/null || echo "$RESPONSE"
echo ""
echo "=== Done ==="
