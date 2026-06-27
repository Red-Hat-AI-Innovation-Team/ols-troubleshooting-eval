# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Evaluation framework for OpenShift Lightspeed (OLS) troubleshooting agent. Deploys deliberately broken Kubernetes/OpenShift workloads to a live cluster, asks the OLS agent to diagnose them via MCP tools, and scores responses with an LLM judge.

- 11 troubleshooting scenarios (envvar_missing, batch_failure, storage_binding, etc.)
- 6 MCP-based open-ended conversations (payments connection leak demo)
- Supports inference-time scaling (ITS) via its_hub gateway
- LLM-as-judge scoring (default: gpt-5-mini)

## Common Commands

```bash
# One-time setup (clones lightspeed-service, installs lightspeed-eval CLI)
bash setup.sh

# Environment setup (CRC cluster, Docker Hub cache, MCP server)
make env-up

# Run evaluation
OPENAI_API_KEY=$(cat .openai_key) ./run_eval.sh <label> <model_url> <model_name> [iterations]

# Run MCP evaluation (requires Prometheus + payments demo)
MCP_EVALS=1 OPENAI_API_KEY=$(cat .openai_key) ./run_eval.sh <label> <model_url> <model_name> [iterations]

# Run ITS sweep (multiple budgets/voting modes)
./run_its_sweep.sh <model_url> <model_name> [iterations]

# View results
./results.sh <label>

# Run a single scenario
cd eval_scenarios && make envvar_missing

# Teardown
make env-down       # preserve CRC VM
make env-nuke       # full cleanup including CRC delete
```

## Architecture

### Entry Flow

`run_eval.sh` is the orchestrator:
1. Generates OLS config from env vars → `.work/<label>/olsconfig.yaml`
2. Starts services: openshift-mcp-server (8085), optionally ITS gateway (8100), optionally obs-mcp (9100), then OLS (8080)
3. Per iteration: deploys broken scenario → queries OLS → `lightspeed-eval` scores response → cleans up
4. Results land in `eval_scenarios/results/traced_<label>/iter_XX/<scenario>/`

### Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | LLM API access |
| `JUDGE_MODEL` | Judge LLM (default: gpt-5-mini) |
| `ITERATIONS` | Number of eval iterations (default: 3) |
| `ITS_BUDGET` | Inference-time scaling parallel calls (e.g., 4, 8) |
| `ITS_ALGO` | ITS algorithm: self-consistency, best-of-n |
| `ITS_VOTE_MODE` | Voting: tool_hierarchical, tool_flat_all |
| `MCP_EVALS` | Set to 1 for MCP eval mode |
| `TRACING` | Set to "on" for Langfuse tracing |
| `CONTEXT_WINDOW_SIZE` | OLS context window (default: 128000) |
| `TEMPERATURE` | Model temperature |
| `IAAS_TARGET` | Override model endpoint for env cleanup |

### Scenario Structure

Each scenario in `eval_scenarios/scenarios/<tag>/`:
- `setup.sh` — deploys broken workload to cluster
- `cleanup.sh` — tears down workload
- `fixtures/*.yaml` — Kubernetes manifests

`config_drift_analysis` is special: deployed once before eval via `setup_config_drift.sh`, not per-iteration.

### Eval Configs (YAML)

- `eval_scenarios/evals.yaml` — main 11-scenario definitions (queries, expected answers, metrics)
- `eval_scenarios/mcp_evals.yaml` — 6 MCP conversations (11 turns, payments demo)
- `eval_scenarios/system_template.yaml` — OLS config template with `{{PLACEHOLDER}}` substitution
- Variant configs: `evals_hard30.yaml`, `evals_v4.yaml`, `evals_sweep.yaml`

### MCP Server Config

`mcp_config.toml` — Go-based openshift-mcp-server:
- Port 8085, read_write mode (`read_only = false`, `disable_destructive = false`)
- Denies: ServiceAccounts, Secrets, all `rbac.authorization.k8s.io/v1` (ClusterRoles, ClusterRoleBindings, RoleBindings, Roles)
- Disabled tools: configuration_view, helm_install, helm_list, helm_uninstall

### Services (ports)

| Port | Service |
|------|---------|
| 5000 | Docker Hub pull-through cache |
| 8080 | OLS (lightspeed-service) |
| 8085 | openshift-mcp-server |
| 8100 | ITS gateway (its_hub) |
| 9100 | obs-mcp (Prometheus/Alertmanager) |

### Payments Demo (`scenarios/payments/`)

Cross-namespace connection leak scenario for MCP evals:
- shared-services namespace: PostgreSQL + reporting-service
- payments namespace: payments-api
- `scripts/break.sh` rolls out v1.0.2 with connection leak → exhausts pool → 503s

## Known Eval Quirks

- `deepeval:knowledge_retention` always scores 0.0 (creates 95% ceiling on aggregate)
- `scheduled_outage_detection` and `periodic_failure_window`: log evidence unreachable via default tail, all models score 0-20%
- OLS pinned to commit `f600c714` (includes tool output offloading, OLS-2277)
- Results format: `evaluation_*_summary.json` + `evaluation_*_detailed.csv` per scenario per iteration

## Analysis

- `analyze_failures.py` — cross-model failure analysis (clarification-seeking patterns, per-scenario failure rates)
- `results.sh` — parses CSVs and prints pass rates per scenario
