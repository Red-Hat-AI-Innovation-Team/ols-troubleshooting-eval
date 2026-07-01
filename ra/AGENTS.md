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
  cpt/                      # CPT data pipeline scripts + documentation
    build_dataset.py        #   Builds JSONL dataset from doc repos + SO CSVs (global dedup)
    html_to_text.py         #   SO HTML→plain text converter (bs4-based)
    README.md               #   Data sources, methods, output stats
    sede_queries.md          #   All 60 SEDE SQL queries + scraping method
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

## CPT (Continued Pretraining) Data

All CPT data lives on `rh-h100-01:~/rawhad/ols-cpt/`. Managed as a `uv` project (Python 3.11, deps: `tiktoken`, `playwright`, `beautifulsoup4`).

### Purpose

Raw domain knowledge corpus for continued pretraining. The `ra/` pipeline's SFT traces (Socratic conversations in `sdg/v1/`) come after CPT — CPT injects domain knowledge, SFT teaches behavior.

### Directory structure

```
~/rawhad/ols-cpt/
  pyproject.toml              # uv project config
  count_tokens.py             # Token counter across all doc repos
  build_dataset.py            # Builds final JSONL from docs + SO CSVs (global dedup)
  html_to_text.py             # Final SO HTML→text converter (bs4-based)
  process_so_data.py          # Batch processor: all SO CSVs → so_corpus/
  test_html_to_text.py        # Test on 10 random samples
  strategy_regex.py           # Alt parser: regex (rejected — lossy)
  strategy_htmlparser.py      # Alt parser: stdlib HTMLParser (rejected — no inline code)
  strategy_bs4.py             # Alt parser: bs4 with bold/italic (basis for final)
  repos/                      # 18 cloned doc repos (shallow, ~11GB on disk)
  so_data/                    # 60 raw SO CSV files (539MB, HTML bodies)
  so_corpus/                  # 51 processed plain-text files (451MB)
  cpt_dataset.jsonl           # Final CPT dataset (185K docs, 573MB)
```

### Data sources — doc repos (17.5M tokens)

All Apache-2.0 or CC-BY-4.0 licensed. Cloned with `--depth 1`.

| Repo | Dir name | Doc files | Raw MB | Tokens | Notes |
|------|----------|-----------|--------|--------|-------|
| openshift/openshift-docs | openshift-docs | 11,645 | 41.4M | 10,268,283 | AsciiDoc, OCP 3.x-4.22. 59% of total |
| kubernetes/website | kubernetes-website | 1,670 | 14.0M | 3,699,000 | Markdown + Hugo, `content/en/docs/` |
| argoproj/argo-cd | argocd | 452 | 2.8M | 663,637 | MkDocs Markdown |
| istio/istio.io | istio-docs | 407 | 1.8M | 452,354 | Hugo Markdown, `content/en/docs/` |
| tigera/docs | calico-docs | 357 | 1.8M | 422,554 | MDX (Docusaurus), OSS calico/ only |
| ovn-kubernetes/ovn-kubernetes | ovn-kubernetes | 112 | 1.4M | 389,715 | MkDocs, has `troubleshooting/` section |
| tektoncd/pipeline | tekton-pipeline | 66 | 1.2M | 317,027 | Plain Markdown |
| prometheus/docs | prometheus-docs | 128 | 1.1M | 267,153 | Markdown, includes blog posts |
| operator-framework/operator-sdk | operator-sdk | 179 | 0.9M | 214,183 | Hugo Markdown |
| coredns/coredns.io | coredns-io | 223 | 0.6M | 181,013 | Hugo Markdown |
| etcd-io/website | etcd-website | 96 | 0.7M | 178,398 | Hugo/Docsy, v3.6 only |
| helm/helm-www | helm-www | 127 | 0.7M | 165,318 | MDX (Docusaurus) |
| prometheus/prometheus | prometheus-server | 30 | 0.5M | 125,098 | Markdown, config/querying reference |
| coredns/coredns | coredns | 68 | 0.3M | 71,323 | Plugin READMEs |
| operator-framework/olm | olm | 33 | 0.2M | 48,960 | Plain Markdown, v0 (maintenance mode) |
| prometheus/alertmanager | prometheus-alertmanager | 11 | 0.1M | 33,128 | Markdown, config reference |
| cri-o/cri-o | cri-o | 12 | 0.1M | 28,950 | Markdown, docs/ + tutorials/ |
| tektoncd/website | tekton-website | 29 | 0.1M | 26,663 | Hugo Markdown |
| **TOTAL** | | **15,645** | **69.8M** | **17,552,757** | |

Token counts measured with `cl100k_base` tokenizer (GPT-4/Claude approx). Markup stripped before counting (AsciiDoc directives, Hugo frontmatter/shortcodes, MDX JSX tags).

Grafana docs excluded (AGPL-3.0 license risk).

### Data sources — Stack Overflow (451MB processed)

Scraped from SEDE (data.stackexchange.com) via Playwright CDP automation against a local Chrome instance (Cloudflare blocks headless). License: CC BY-SA.

| Category | Tags | Posts | Raw CSV |
|----------|------|-------|---------|
| K8s core | kubernetes (3 date batches), kubectl | 59,865 | 175MB |
| Docker | docker (4 date batches, score>=1) | 77,409 | 224MB |
| OpenShift | openshift, openshift-4 | 7,555 | 18MB |
| Monitoring | prometheus, promql, grafana, alertmanager, grafana-alerting | 13,678 | 26MB |
| Networking | kubernetes-ingress, kubernetes-networking, kube-proxy, calico, istio, coredns, ovn, kubernetes-dns, nfs | 8,427 | 24MB |
| Workloads | kubernetes-pod, kubernetes-deployment, kubernetes-statefulset, kubernetes-cronjob, kubernetes-jobs, kubernetes-daemonset, kubernetes-hpa, kubernetes-health-check | 3,127 | 8MB |
| Config/Security | kubernetes-configmap, kubernetes-secrets, kubernetes-rbac, kubernetes-security, cert-manager, kubernetes-pvc | 1,547 | 4MB |
| Runtime/Infra | containers, container-runtime, containerd, cri-o, kubelet, etcd | 11,957 | 28MB |
| CI/CD | tekton, argocd, argo-cd, operator-sdk, operator-lifecycle-manager | 890 | 2MB |
| Cross-domain | kubernetes+postgresql/redis/elasticsearch/mongodb/cassandra/mysql/java/grpc/nginx-ingress | 5,993 | 21MB |
| **TOTAL** | | **190,541** | **539MB → 451MB text** |

Large tags (>45k questions) were date-partitioned to stay under SEDE's 50k row limit. Docker also filtered to `score >= 1`.

Each CSV row contains: QuestionId, Title, Tags, QScore, ViewCount, CreationDate, QuestionBody (HTML), AnswerId, AScore, AnswerBody (HTML). The `OUTER APPLY` pattern selects the accepted answer or highest-scored answer per question.

### HTML-to-text conversion

Final converter: `html_to_text.py` (BeautifulSoup4-based). Design choices:

| Feature | Decision | Rationale |
|---------|----------|-----------|
| Code blocks (`<pre><code>`) | Triple backticks + language hint | Preserve code patterns for CPT |
| Inline `<code>` | Backtick wrapping | Common in technical Q&A |
| `<h1>`-`<h6>` | Markdown `#` headings | Structural signal |
| `<ul>/<li>` | `- ` bullets | Readable lists |
| `<a>` links | Text only, href dropped | URLs are noise for CPT |
| `<strong>/<em>` | Stripped, text only | Bold/italic adds no CPT value |
| `<img>` | Stripped entirely | No alt-text noise |
| HTML entities | Decoded via bs4 | Clean text |
| Blank lines | Collapsed to max 2 | No excessive whitespace |

Conversion ratio: ~89% (HTML markup was ~11% of content). Three strategies were evaluated:
- `strategy_regex.py` — regex-only, stdlib. Rejected: strips inline code backticks and code block indentation.
- `strategy_htmlparser.py` — stdlib HTMLParser. Rejected: preserves indentation but drops inline code formatting.
- `strategy_bs4.py` — bs4 with full markdown (bold/italic/images). Basis for final, simplified.

### Output format (so_corpus/)

Each `.txt` file contains concatenated documents, one per SO question:

```
## <Title>

<question text>

### Answer

<answer text>

---

## <Next Title>
...
```

### Final dataset (`cpt_dataset.jsonl`)

Built by `build_dataset.py`. One JSON object per line:

```jsonl
{"text": "...", "source": "docs", "repo": "coredns", "path": "coredns/plugin.md"}
{"text": "...", "source": "stackoverflow", "tag": "kubernetes__2023_2027", "qid": "12345"}
```

| Metric | Value |
|--------|-------|
| Total documents | 185,631 |
| Doc files | 28,402 |
| SO posts (after global dedup) | 157,229 |
| SO dupes removed | 33,311 (17% cross-tag overlap) |
| File size | 572.7 MB |

### Total CPT corpus

| Source | Text size | Est. tokens |
|--------|-----------|-------------|
| Doc repos | 70 MB | ~17.5M |
| SO corpus | 451 MB | ~110M (est.) |
| **Total** | **~521 MB** | **~127M** |

Sufficient for a pilot CPT run. For full-scale CPT (100M+ tokens), the SO corpus alone covers it.

### SEDE query automation

Queries automated via `sede_cdp.py` (local Mac) connecting to Chrome with `--remote-debugging-port=9222` over CDP. Playwright is the CDP client library (no separate browser launched). Key details:

- Cloudflare blocks headless browsers on data.stackexchange.com
- Chrome Remote Debugging bypasses this (real browser session)
- Retry logic: exponential backoff with jitter, max 10 retries, max 60s wait
- Skip logic: already-downloaded CSVs are skipped on restart (idempotent)
- Download timeout: 300s for large CSVs
