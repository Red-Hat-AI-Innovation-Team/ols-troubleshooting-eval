# OLS Troubleshooting Agent Eval

Evaluate any model on the OpenShift Lightspeed troubleshooting benchmark. Point to a model endpoint, run one command, get results.

11 scenarios, 20 eval points per iteration, LLM-as-judge scoring.

## Quick Start

```bash
# 1. Setup (one-time)
bash setup.sh
# Edit .env with your API keys

# 2. Login to cluster
oc login --token=<token> --server=<server>

# 3. Deploy config_drift scenario (once, before evals)
bash setup_config_drift.sh

# 4. Run eval
./run_eval.sh <label> <model_url> <model_name>

# 5. Clean up (after all evals)
bash cleanup_config_drift.sh
```

## Arguments

```
./run_eval.sh <model_label> <model_url> <model_name>
```

| Arg | Description | Example |
|-----|-------------|---------|
| `model_label` | Short name for results directory | `qwen35_base` |
| `model_url` | OpenAI-compatible API base URL | `http://localhost:8234/v1` |
| `model_name` | Model name in API requests | `openshift-expert` |

## Options (env vars)

| Env var | Default | Description |
|---------|---------|-------------|
| `ITERATIONS` | `3` | Number of eval iterations |
| `ITER_OFFSET` | `0` | Starting iteration number |
| `JUDGE_MODEL` | `gpt-5-mini` | Judge LLM for scoring |
| `TRACING` | `off` | Langfuse tracing: `on` or `off` |
| `OLS_DIR` | `./lightspeed-service` | Path to OLS checkout |

## Examples

```bash
# vLLM model, 3 iterations (default)
./run_eval.sh qwen35_base http://localhost:8234/v1 openshift-expert

# OpenAI model, 5 iterations
ITERATIONS=5 ./run_eval.sh gpt5mini https://api.openai.com/v1 gpt-5-mini

# With Langfuse tracing enabled
TRACING=on ./run_eval.sh nemotron_sft http://localhost:8250/v1 nemotron-gpt55-sft

# Custom judge model
JUDGE_MODEL=gpt-4.1 ./run_eval.sh mymodel http://localhost:8234/v1 openshift-expert

# Run in tmux (recommended for long evals)
tmux new-session -d -s eval "./run_eval.sh mymodel http://localhost:8234/v1 openshift-expert"

# Add 2 more iterations to an existing run
ITERATIONS=2 ITER_OFFSET=3 ./run_eval.sh mymodel http://localhost:8234/v1 openshift-expert
```

## Setup Details

### OLS (OpenShift Lightspeed)

`setup.sh` clones and installs `lightspeed-service`. OLS is the agent framework — it takes a user query, calls the model with MCP tools, and produces a diagnosis. The eval runner starts/stops OLS automatically.

### MCP Server

The `kubernetes-mcp-server` exposes the OpenShift/Kubernetes API as tool calls (pods_list, pods_get, pods_log, events_list, etc.). Installed via npm, started automatically by the eval runner on port 8089.

### Cluster

The eval deploys broken workloads on the cluster, asks the model to diagnose them, and cleans up after. Requires `oc` login with permissions to create/delete namespaces. Works with:
- Shared OpenShift cluster (pawshift)
- CRC / microshift
- Any OpenShift cluster you have admin access to

### Langfuse (optional)

Set `TRACING=on` and configure Langfuse keys in `.env` to capture per-round traces of every model interaction. Useful for debugging failure modes.

## Credentials (.env)

```bash
cp .env.example .env
# Edit with your values:
```

| Key | Required | Purpose |
|-----|----------|---------|
| `OPENAI_API_KEY` | Yes | For the judge model (gpt-5-mini) |
| `DOCKERHUB_USER` | Recommended | Pull secrets for scenario pod images |
| `DOCKERHUB_TOKEN` | Recommended | Pull secrets for scenario pod images |
| `LANGFUSE_SECRET_KEY` | If TRACING=on | Langfuse tracing |
| `LANGFUSE_PUBLIC_KEY` | If TRACING=on | Langfuse tracing |
| `LANGFUSE_HOST` | If TRACING=on | Langfuse host URL |

## Output

Results go to `lightspeed-service/eval/troubleshooting/results/traced_<label>/`.

```
traced_mymodel/
  iter_01/
    envvar_missing/
      evaluation_YYYYMMDD_HHMMSS_detailed.csv
    batch_failure/
      ...
  iter_02/
    ...
```

The script prints per-iteration and total pass rates at the end.

## Analysis

```bash
# Cross-model failure mode analysis
python3 analyze_failures.py

# Single model
python3 analyze_failures.py lightspeed-service/eval/troubleshooting/results/traced_mymodel
```

## Scenarios

| Scenario | Points | Description |
|----------|--------|-------------|
| envvar_missing | 1 | Pod crashing due to undefined env var |
| batch_failure | 1 | Job failing to connect to database |
| storage_binding | 1 | PVC stuck pending, wrong StorageClass |
| namespace_pod_count | 1 | Count pods across namespaces |
| scheduled_outage_detection | 1 | Detect maintenance window in logs |
| periodic_failure_window | 1 | Find recurring error pattern in logs |
| readiness_probe_diagnosis | 1 | Pod not ready, failing readiness probe |
| ingress_rule_mismatch | 1 | NetworkPolicy blocking traffic |
| oom | 1 | Pod OOMKilled from memory leak |
| wrong_networkpolicy | 10 | Multi-turn: frontend can't reach backend (3 turns, 6 turn-level + 4 conversation-level metrics) |
| config_drift_analysis | 1 | Config reload causing connection errors |

**Total: 20 points per iteration. Practical ceiling: 95%** (knowledge_retention always scores 0).

## Known Eval Quirks

- **95% ceiling**: `deepeval:knowledge_retention` always scores 0.0 for every model
- **config_drift_analysis**: Deploy once before eval, not per-iteration (setup script is flaky)
- **scheduled_outage_detection**: Log evidence unreachable via default tail. All models 0-20%
- **periodic_failure_window**: Same timing issue. Scores depend on model's log retrieval strategy
