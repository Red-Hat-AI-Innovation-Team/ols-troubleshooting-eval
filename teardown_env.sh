#!/bin/bash
set -euo pipefail

# teardown_env.sh — Tear down the eval environment
#
# Usage:
#   bash teardown_env.sh          # Stop CRC (preserves VM, fast restart)
#   bash teardown_env.sh --force  # Delete CRC VM + stop cache (full cleanup)

FORCE="${1:-}"

log() { echo "=== $1 ==="; }
ok()  { echo "  ✓ $1"; }

# ---------- Kill eval services ----------

log "Stopping eval services"

pkill -f "runner.py" 2>/dev/null && ok "OLS stopped" || ok "OLS not running"
pkill -f "openshift-mcp-server" 2>/dev/null && ok "MCP server stopped" || ok "MCP server not running"
pkill -f "its-iaas" 2>/dev/null && ok "ITS gateway stopped" || ok "ITS gateway not running"

# ---------- Stop/delete CRC ----------

if [ "$FORCE" = "--force" ] || [ "$FORCE" = "-f" ]; then
    log "Deleting CRC (full cleanup)"
    crc delete -f 2>/dev/null && ok "CRC deleted" || ok "CRC not running"

    log "Stopping Docker Hub cache"
    podman stop dockerhub-cache 2>/dev/null && ok "Cache stopped" || ok "Cache not running"
else
    log "Stopping CRC (preserving VM)"
    crc stop 2>/dev/null && ok "CRC stopped" || ok "CRC not running"
    echo ""
    echo "  CRC VM preserved. Use 'crc start' to restart quickly."
    echo "  Use 'bash teardown_env.sh --force' for full cleanup."
fi

echo ""
echo "Done."
