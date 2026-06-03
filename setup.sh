#!/usr/bin/env bash
set -euo pipefail

# One-time setup: clone OLS, install dependencies, configure environment.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OLS_DIR="${OLS_DIR:-$SCRIPT_DIR/lightspeed-service}"

echo "=== OLS Troubleshooting Eval Setup ==="

# 1. Clone lightspeed-service if not present
if [ ! -d "$OLS_DIR" ]; then
    echo "Cloning lightspeed-service..."
    git clone https://github.com/openshift/lightspeed-service.git "$OLS_DIR"
else
    echo "lightspeed-service already present at $OLS_DIR"
fi

# 2. Install OLS dependencies
echo "Installing OLS dependencies..."
cd "$OLS_DIR"
uv sync 2>/dev/null || pip install -e . 2>/dev/null || echo "WARN: Could not install OLS deps. Install manually: cd $OLS_DIR && uv sync"

# 3. Install lightspeed-eval CLI
if ! command -v lightspeed-eval &>/dev/null; then
    echo "Installing lightspeed-eval..."
    pip install lightspeed-evaluation 2>/dev/null || echo "WARN: Could not install lightspeed-eval. Install manually: pip install lightspeed-evaluation"
else
    echo "lightspeed-eval already installed"
fi

# 4. Install MCP server
if ! command -v npx &>/dev/null; then
    echo "WARN: npx not found. Install Node.js for kubernetes-mcp-server"
else
    echo "MCP server available via: npx kubernetes-mcp-server@latest"
fi

# 5. Create .env if not present
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "Created .env from template -- edit with your API keys"
else
    echo ".env already exists"
fi

# 6. Create .openai_key if OPENAI_API_KEY is set
if [ -n "${OPENAI_API_KEY:-}" ] && [ ! -f "$SCRIPT_DIR/.openai_key" ]; then
    echo "$OPENAI_API_KEY" > "$SCRIPT_DIR/.openai_key"
    echo "Created .openai_key from OPENAI_API_KEY env var"
fi

# 7. Patch evals.yaml to comment out config_drift setup/cleanup
EVALS_YAML="$OLS_DIR/eval/troubleshooting/evals.yaml"
if grep -q "^  setup_script: scenarios/config_drift_analysis" "$EVALS_YAML" 2>/dev/null; then
    sed -i.bak 's|^  setup_script: scenarios/config_drift_analysis|  # setup_script: scenarios/config_drift_analysis|' "$EVALS_YAML"
    sed -i.bak 's|^  cleanup_script: scenarios/config_drift_analysis|  # cleanup_script: scenarios/config_drift_analysis|' "$EVALS_YAML"
    rm -f "${EVALS_YAML}.bak"
    echo "Patched evals.yaml: config_drift setup/cleanup commented out"
fi

echo ""
echo "Setup complete. Next steps:"
echo "  1. Edit .env with your OPENAI_API_KEY and DOCKERHUB credentials"
echo "  2. Login to cluster: oc login --token=<token> --server=<server>"
echo "  3. Deploy config_drift: bash setup_config_drift.sh"
echo "  4. Run eval: bash run_eval.sh <label> <model_url> <model_name> 3"
