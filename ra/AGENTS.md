# AGENTS.md — ra/

Mock MCP tool environment + LLM agent loop for evaluating OpenShift troubleshooting agents offline (no live cluster needed). Python 3.11+, managed with `uv`.

## Project structure

```
ra/
  agent.py                  # Generic LLM agent loop with tool calling (dataclass)
  run_agent.py              # Entry point: user-sim SRE + troubleshooter conversation loop
  mock_tools.py             # 30 PostgreSQL-backed mock MCP tools (openshift-mcp + obs-mcp)
  vshell.py                 # In-memory virtual shell for pods_exec (fs + network sim)
  db.py                     # DB connection, schema init, seed loading, teardown
  seeding_order.py          # Topological sort of tables by FK dependencies
  generate_seed_data.py     # LLM-based seed data generation (per-table)
  generate_scenario_based_data.py  # Scenario-driven holistic seed data generation
  llm/                      # Provider-agnostic LLM client abstraction
    base.py                 #   ABC: LLMClient.chat() interface
    types.py                #   Dataclasses: LLMResponse, ToolCall
    anthropic_vertex.py     #   Anthropic Vertex AI implementation (streaming)
    openai_client.py        #   OpenAI-compatible implementation (+ custom base_url)
  world_model_db_schema.sql # 20+ tables modeling K8s/OpenShift cluster state
  raw_tool_defs.json        # MCP tool definitions (Anthropic format source)
  seed_data.json            # Generated cluster seed data
  test_data.json            # Seed data for tests
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

# Seed data generation
uv run python generate_seed_data.py              # per-table LLM generation
uv run python generate_scenario_based_data.py "A 3-node cluster with memory pressure"

# Run agent loop
uv run python run_agent.py

# Run tests (custom runner, not pytest)
uv run python test_mock_tool.py
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `CLOUD_ML_REGION` | Vertex AI region (default: us-east5) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID for Anthropic Vertex |
| `OPENAI_API_KEY` | API key for OpenAIClient (read by SDK if not passed) |

## Dependencies

- `psycopg2-binary` — PostgreSQL client
- `anthropic[vertex]` — Anthropic SDK with Vertex AI support
- `pyyaml` — YAML serialization for tool outputs

## Architecture

**Data flow**: `run_agent.py` creates two Agent instances — a user-simulator (SRE with full DB knowledge) and a troubleshooter (has MCP tools, no DB access). They converse for up to 5 rounds until the SRE says "DONE".

**Mock tools** (`mock_tools.py`): Each tool function takes `(conn, *, param=...) -> str`. The `TOOLS` dict maps tool names to functions. `call_tool()` dispatches by name. `load_tool_defs()` reads `raw_tool_defs.json` and converts to Anthropic tool format.

**Virtual shell** (`vshell.py`): Backs `pods_exec`. Supports cat, ls, grep, curl, nslookup, dig, and ~20 other commands against an in-memory `dict[str, VFile]` filesystem and `VNet` (DNS + HTTP endpoints).

**LLM abstraction** (`llm/`): `LLMClient` ABC with single `chat()` method. `AnthropicVertexClient` uses streaming + extended thinking. Returns normalized `LLMResponse` with `ToolCall` list.

## Code style

- Module-level docstrings on every file
- `from __future__ import annotations` in `llm/` package
- Dataclasses for state containers (`Agent`, `ToolCall`, `LLMResponse`, `VFile`, `VNet`)
- ABC for `LLMClient` interface, concrete impl in separate module
- Type hints throughout: `str | None`, `list[dict]`, `Callable[[str, dict], str]`
- Keyword-only args for tool functions (using `*` separator)
- No linter/formatter config in pyproject.toml (no ruff, no mypy configured)

## Testing

- Custom test runner in `test_mock_tool.py` (not pytest)
- Tests spin up a `test_openshift_cluster` DB, load schema + `test_data.json`, run assertions, then drop DB
- Each test function: `def test_<tool_name>(conn)` — takes a psycopg2 connection
- Run single test by editing `main()` or filtering in the test list
- Run all: `uv run python test_mock_tool.py`

## Database

- PostgreSQL 17 via Podman, port 5433, user `postgres`, no password (trust auth)
- DB name: `openshift_cluster` (production), `test_openshift_cluster` (tests)
- Schema covers: clusters, nodes, namespaces, pods, containers, events, resources, metrics, alerts, silences
- JSONB columns for labels, annotations, manifests, filesystem/network state
- GIN indexes on JSONB columns, trigram index on metric names
