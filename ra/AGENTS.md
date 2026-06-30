# AGENTS.md — ra/

Mock MCP tool environment + LLM agent loop for evaluating OpenShift troubleshooting agents offline (no live cluster needed). Python 3.11+, managed with `uv`.

## Project structure

```
ra/
  main.py                   # Pipeline orchestrator: seed generation + agent eval (ThreadPoolExecutor)
  run_agent.py              # Single-run entry: user-sim SRE + troubleshooter Socratic conversation
  agent.py                  # Generic LLM agent loop with tool calling (@dataclass)
  mock_tools.py             # 30 PostgreSQL-backed mock MCP tools (openshift-mcp + obs-mcp)
  vshell.py                 # In-memory virtual shell for pods_exec (fs + network sim)
  db.py                     # DB connection/schema/seed/teardown + seeding order (topo sort by FK)
  generate_scenario_based_data.py  # Scenario-driven seed data generation (with JSONB schemas)
  dedup_scenarios.py        # Embed scenarios + cosine dedup via local embedding server
  llm/                      # Provider-agnostic LLM client abstraction
    base.py                 #   ABC: LLMClient.chat() interface
    types.py                #   Pydantic models: LLMResponse, ToolCall, Message, ToolDef, ToolResult
    anthropic_vertex.py     #   Anthropic Vertex AI implementation (streaming + extended thinking)
    openai_client.py        #   OpenAI-compatible implementation (+ custom base_url)
    config/                 #   Pydantic config models per provider
      base.py               #     LLMConfig (max_concurrency)
      anthropic_vertex.py   #     AnthropicVertexConfig
      openai.py             #     OpenAIConfig
  scenarios.txt             # One scenario description per line (input to main.py)
  sdg/v1/                   # Generated artifacts: seed JSON + agent run conversations per scenario
  world_model_db_schema.sql # 20+ tables modeling K8s/OpenShift cluster state
  raw_tool_defs.json        # MCP tool definitions (Anthropic format source)
  seed_data.json            # Example cluster seed data (used by run_agent.py standalone)
  test_data.json            # Seed data for tests (separate from agent seed data)
  test_mock_tool.py         # Custom test runner (not pytest)
  MCP_TOOLS.md              # Full MCP tool schema documentation (30 tools)
```

## Commands

```bash
# Infrastructure: start PostgreSQL via Podman (port 5433, no auth)
podman machine start
podman run -d --name world-model-pg -p 127.0.0.1:5433:5432 \
  -e POSTGRES_DB=openshift_cluster -e POSTGRES_HOST_AUTH_METHOD=trust \
  docker.io/library/postgres:17

# DB lifecycle
uv run python db.py init seed_data.json    # create DB + schema + seed
uv run python db.py teardown                # drop DB

# Pipeline (primary entry point)
uv run python main.py                          # both stages: seed gen + agent runs
uv run python main.py --stage seed             # seed generation only
uv run python main.py --stage run              # agent runs only (seeds must exist)
uv run python main.py --seeds 3 --runs 2       # override counts (default: 5 seeds × 5 runs)

# Single-run (interactive / debugging)
uv run python run_agent.py

# Seed data generation (single scenario)
uv run python generate_scenario_based_data.py "A 3-node cluster with memory pressure"

# Scenario dedup
uv run python dedup_scenarios.py --threshold 0.80

# Tests (custom runner, not pytest)
uv run python test_mock_tool.py

# Type check
uv run mypy .
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `CLOUD_ML_REGION` | Vertex AI region (default: us-east5) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID for Anthropic Vertex |
| `OPENAI_API_KEY` | API key for OpenAIClient (read by SDK if not passed) |

## Architecture

**Pipeline** (`main.py`): Two-stage pipeline. Stage 1 generates N seed data variants per scenario (from `scenarios.txt`) using `ThreadPoolExecutor`. Stage 2 runs the agent loop N times per seed. All results cached to `sdg/v1/<scenario_idx>/`. Concurrency controlled by `AnthropicVertexConfig.max_concurrency` (default 50).

**Data flow** (`run_agent.py`): Creates two Agent instances — a user-simulator (SRE with full DB knowledge, claude-opus-4-6) and a troubleshooter (has MCP tools, no DB access, claude-haiku-4-5). They converse Socratically for up to 5 rounds until the SRE says "DONE".

**Mock tools** (`mock_tools.py`): Each tool function: `def tool_name(conn, *, param=...) -> str`. `TOOLS` dict maps names to functions. `call_tool()` dispatches. `load_tool_defs()` reads `raw_tool_defs.json`.

**Virtual shell** (`vshell.py`): Backs `pods_exec`. Supports cat, ls, grep, curl, nslookup, dig, ~20 commands against in-memory `dict[str, VFile]` filesystem and `VNet` (DNS + HTTP endpoints).

**LLM abstraction** (`llm/`): `LLMClient` ABC with `chat()` method. `AnthropicVertexClient` (streaming + extended thinking) and `OpenAIClient` (custom base_url). Returns normalized `LLMResponse`. Config via pydantic models in `llm/config/`.

## Code style

- Module-level docstrings on every file
- `from __future__ import annotations` in `llm/` package
- Pydantic `BaseModel` for LLM types (`ToolCall`, `LLMResponse`, `Message`, `ToolDef`, `LLMConfig`)
- `@dataclass` for runtime state (`Agent`, `Table`, `VFile`, `VNet`, `VDNSRecord`, `VHTTPEndpoint`)
- ABC for `LLMClient` interface, concrete impl in separate modules
- Type hints throughout: `str | None`, `list[dict]`, `Callable[[str, dict], str]`
- Keyword-only args for tool functions (using `*` separator)
- Mixed indentation: `db.py` and `vshell.py` use 2-space; all other files use 4-space
- `db.connect()` is a context manager (`with db.connect(dsn) as conn:`)
- mypy configured in pyproject.toml: `warn_return_any`, `check_untyped_defs`, `ignore_missing_imports`
- No linter/formatter (no ruff, black, isort)

## Testing

- Custom test runner in `test_mock_tool.py` (not pytest)
- Tests spin up a `test_openshift_cluster` DB, load schema + `test_data.json`, run assertions, then drop DB
- Each test function: `def test_<tool_name>(conn)` — takes a psycopg2 connection
- Run single test by editing `main()` or filtering in the test list
- Run all: `uv run python test_mock_tool.py`

## Database

- PostgreSQL 17 via Podman, port 5433, user `postgres`, no password (trust auth)
- DB name: `openshift_cluster` (production), `test_openshift_cluster` (tests), `ols_run_<idx>` (pipeline per-run)
- Schema: clusters, nodes, namespaces, pods, containers, events, resources, metrics, alerts, silences
- JSONB columns for labels, annotations, manifests, filesystem/network state
- GIN indexes on JSONB columns, trigram index on metric names
