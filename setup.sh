#!/usr/bin/env bash
set -euo pipefail

# One-time setup: clone OLS, create venv, install dependencies.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OLS_DIR="${OLS_DIR:-$SCRIPT_DIR/lightspeed-service}"
EVAL_REPO="${EVAL_REPO:-$SCRIPT_DIR/lightspeed-evaluation}"

echo "=== OLS Troubleshooting Eval Setup ==="

# 1. Check uv
if ! command -v uv &>/dev/null; then
    echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# 2. Clone lightspeed-service if not present
if [ ! -d "$OLS_DIR" ]; then
    echo "Cloning lightspeed-service..."
    git clone https://github.com/openshift/lightspeed-service.git "$OLS_DIR"
else
    echo "lightspeed-service already present at $OLS_DIR"
fi

# 3. Install OLS dependencies via uv
echo "Installing OLS dependencies..."
cd "$OLS_DIR"
uv sync

# 4. Clone and install lightspeed-evaluation
if [ ! -d "$EVAL_REPO" ]; then
    echo "Cloning lightspeed-evaluation..."
    git clone https://github.com/lightspeed-core/lightspeed-evaluation.git "$EVAL_REPO"
else
    echo "lightspeed-evaluation already present at $EVAL_REPO"
fi
if ! uv run lightspeed-eval --help &>/dev/null 2>&1; then
    echo "Installing lightspeed-eval into OLS venv..."
    uv pip install -e "$EVAL_REPO"
fi
echo "lightspeed-eval: $(uv run lightspeed-eval --version 2>/dev/null || echo 'installed')"

# 5. Check npx (for MCP server)
if ! command -v npx &>/dev/null; then
    echo "WARN: npx not found. Install Node.js for kubernetes-mcp-server"
    echo "  brew install node  OR  curl -fsSL https://fnm.vercel.app/install | bash"
else
    echo "MCP server available via: npx kubernetes-mcp-server@latest"
fi

# 6. Create .env if not present
cd "$SCRIPT_DIR"
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "Created .env from template -- edit with your API keys"
else
    echo ".env already exists"
fi

# 7. Create .openai_key if OPENAI_API_KEY is set
if [ -n "${OPENAI_API_KEY:-}" ] && [ ! -f "$SCRIPT_DIR/.openai_key" ]; then
    echo "$OPENAI_API_KEY" > "$SCRIPT_DIR/.openai_key"
    echo "Created .openai_key from OPENAI_API_KEY env var"
fi

# 8. Patch evals.yaml to comment out config_drift setup/cleanup
EVALS_YAML="$SCRIPT_DIR/eval_scenarios/evals.yaml"
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
echo "  4. Run eval: ./run_eval.sh <label> <model_url> <model_name>"
