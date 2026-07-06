"""Generate scenario-based seed data for the world model DB using Anthropic Vertex AI.

Usage:
    uv run python generate_scenario_based_data.py "A 3-node OpenShift cluster experiencing memory pressure"

Env vars:
    CLOUD_ML_REGION              - Vertex AI region (default: us-east5)
    ANTHROPIC_VERTEX_PROJECT_ID  - GCP project ID
"""

import json
import sys
from pathlib import Path

import psycopg2

import db
from llm import AnthropicVertexClient, LLMResponse, Message, ToolDef
from llm.base import LLMClient
from llm.config.base import LLMConfig


# ---------------------------------------------------------------------------
# JSON schema builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Explicit JSON schemas for JSONB columns consumed by mock_tools / vshell.
# Keyed by (table_name, column_name).  Only columns with a fixed contract
# need entries — free-form JSONB columns (manifest, stats_summary_json, …)
# keep the default empty schema so the LLM has creative freedom.
# ---------------------------------------------------------------------------

_LABEL_MAP_SCHEMA: dict = {
    "type": "object",
    "description": "Kubernetes label map: string keys to string values",
    "additionalProperties": {"type": "string"},
}

_ANNOTATION_MAP_SCHEMA: dict = {
    "type": "object",
    "description": "Kubernetes annotation map: string keys to string values",
    "additionalProperties": {"type": "string"},
}

JSONB_COLUMN_SCHEMAS: dict[tuple[str, str], dict] = {
    # --- labels / annotations (all tables) ---
    ("nodes", "labels"): _LABEL_MAP_SCHEMA,
    ("nodes", "annotations"): _ANNOTATION_MAP_SCHEMA,
    ("namespaces", "labels"): _LABEL_MAP_SCHEMA,
    ("namespaces", "annotations"): _ANNOTATION_MAP_SCHEMA,
    ("pods", "labels"): _LABEL_MAP_SCHEMA,
    ("pods", "annotations"): _ANNOTATION_MAP_SCHEMA,
    ("resources", "labels"): _LABEL_MAP_SCHEMA,
    ("resources", "annotations"): _ANNOTATION_MAP_SCHEMA,

    # --- nodes.taints ---
    ("nodes", "taints"): {
        "type": "array",
        "description": "Kubernetes taints array",
        "items": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "effect": {"type": "string", "enum": ["NoSchedule", "PreferNoSchedule", "NoExecute"]},
            },
            "required": ["key", "effect"],
        },
    },

    # --- pods.conditions ---
    ("pods", "conditions"): {
        "type": "array",
        "description": "Pod condition array (same shape as status.conditions in K8s API)",
        "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "status": {"type": "string", "enum": ["True", "False", "Unknown"]},
                "lastTransitionTime": {"type": "string"},
                "reason": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["type", "status"],
        },
    },

    # --- containers.ports ---
    ("containers", "ports"): {
        "type": "array",
        "description": "Container port list",
        "items": {
            "type": "object",
            "properties": {
                "containerPort": {"type": "integer"},
                "protocol": {"type": "string", "enum": ["TCP", "UDP"]},
                "name": {"type": "string"},
            },
            "required": ["containerPort"],
        },
    },

    # --- containers.filesystem_json  (consumed by vshell.vfile_from_dict) ---
    ("containers", "filesystem_json"): {
        "type": "object",
        "description": (
            "Virtual filesystem for pods_exec. Keys are absolute POSIX paths. "
            "Values are VFile objects. Example: "
            '{ "/etc/hostname": {"content": "web-0", "is_directory": false, '
            '"permissions": "-rw-r--r--", "owner": "root", "size_bytes": 5}, '
            '"/var/log": {"is_directory": true} }'
        ),
        "additionalProperties": {
            "type": "object",
            "properties": {
                "content": {"type": ["string", "null"], "description": "File text content (null for dirs)"},
                "is_directory": {"type": "boolean", "description": "True for directories"},
                "permissions": {"type": "string", "description": "Unix permission string, e.g. -rw-r--r--"},
                "owner": {"type": "string", "description": "File owner, e.g. root"},
                "size_bytes": {"type": "integer", "description": "File size in bytes"},
            },
        },
    },

    # --- containers.network_json  (consumed by vshell.vnet_from_dict) ---
    ("containers", "network_json"): {
        "type": "object",
        "description": (
            "Virtual network for pods_exec curl/nslookup. Has 'dns' and 'http' keys. "
            "dns maps hostnames to {ip}. http maps URLs to {status_code, body, headers, error}."
        ),
        "properties": {
            "dns": {
                "type": "object",
                "description": "DNS records: hostname -> {ip: string}",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"ip": {"type": "string"}},
                    "required": ["ip"],
                },
            },
            "http": {
                "type": "object",
                "description": "HTTP endpoint stubs: URL -> response",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "status_code": {"type": "integer"},
                        "body": {"type": "string"},
                        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                        "error": {"type": ["string", "null"]},
                    },
                },
            },
        },
    },

    # --- metrics.labels  (used as Prometheus label set) ---
    ("metrics", "labels"): {
        "type": "object",
        "description": "Prometheus metric label set: string keys to string values (e.g. instance, job, namespace)",
        "additionalProperties": {"type": "string"},
    },
}


PG_TO_JSON_TYPE: dict[str, dict] = {
    "int2": {"type": "integer"},
    "int4": {"type": "integer"},
    "int8": {"type": "integer"},
    "float4": {"type": "number"},
    "float8": {"type": "number"},
    "numeric": {"type": "number"},
    "bool": {"type": "boolean"},
    "varchar": {"type": "string"},
    "text": {"type": "string"},
    "char": {"type": "string"},
    "bpchar": {"type": "string"},
    "name": {"type": "string"},
    "uuid": {"type": "string"},
    "timestamptz": {"type": "string"},
    "timestamp": {"type": "string"},
    "date": {"type": "string"},
    "inet": {"type": "string"},
    "cidr": {"type": "string"},
    "json": {},
    "jsonb": {},
    "_text": {"type": "array", "items": {"type": "string"}},
    "_int4": {"type": "array", "items": {"type": "integer"}},
    "_varchar": {"type": "array", "items": {"type": "string"}},
}


def build_row_schema(meta: db.TableMeta) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []

    for col in meta.columns:
        # Use explicit JSONB schema if one exists, else fall back to PG type map
        override = JSONB_COLUMN_SCHEMAS.get((meta.table_name, col.name))
        if override is not None:
            schema = override.copy()
        else:
            schema = PG_TO_JSON_TYPE.get(col.udt_name, {"type": "string"}).copy()

        desc: str = f"pg type: {col.udt_name}"
        if col.max_length:
            desc += f", max_length: {col.max_length}"
        if col.name in meta.primary_key:
            desc += ", PRIMARY KEY"
        if col.column_default:
            desc += f", default: {col.column_default}"
        # Preserve any description from the override, append PG metadata
        if "description" in schema:
            schema["description"] = schema["description"] + f" ({desc})"
        else:
            schema["description"] = desc

        if col.is_nullable and "type" in schema:
            t = schema["type"]
            schema["type"] = [t, "null"] if isinstance(t, str) else t + ["null"]

        properties[col.name] = schema

        has_serial_default: bool = col.column_default is not None and "nextval" in col.column_default
        if not col.is_nullable and not col.column_default:
            required.append(col.name)
        elif has_serial_default:
            required.append(col.name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def build_system_prompt(scenario: str, schema_summary: str) -> str:
    return f"""You are a data generation engine for an OpenShift/Kubernetes cluster monitoring database.

SCENARIO:
{scenario}

All data you generate must be consistent with this scenario. Every table's data should
tell a coherent story that matches the scenario description.

COMPLETE DATABASE SCHEMA (in dependency/seeding order):
{schema_summary}

General rules:
- SERIAL/BIGSERIAL PKs: sequential integers starting from 1
- UUID fields: valid v4 UUIDs
- TIMESTAMPTZ: recent ISO-8601 timestamps (2025-06 timeframe)
- Foreign keys MUST reference IDs that exist in previously generated data
- JSONB fields: realistic JSON matching the column's semantic purpose
- Array types (text[]): realistic arrays
- Data should be internally consistent (e.g., used < capacity)
- Use realistic OpenShift/Kubernetes values (names, IPs, versions, labels)
- All data must serve the scenario narrative"""


# ---------------------------------------------------------------------------
# Planning step
# ---------------------------------------------------------------------------


MODEL: str = "claude-opus-4-6@default"


def plan_row_counts(
    client: LLMClient,
    scenario: str,
    schema_summary: str,
    seeding_order: list[str],
) -> dict[str, int]:
    """Ask the LLM to decide how many rows each table needs for the scenario."""
    table_list: str = "\n".join(f"  - {t}" for t in seeding_order)

    plan_tool_properties: dict[str, dict] = {}
    for t in seeding_order:
        plan_tool_properties[t] = {
            "type": "integer",
            "description": f"Number of rows to generate for table '{t}'",
            "minimum": 0,
        }

    prompt: str = f"""Given this scenario:
{scenario}

And these tables (in seeding order):
{table_list}

Decide how many rows each table needs to realistically represent this scenario.
Consider the relationships between tables. For example, if the scenario mentions
3 nodes, the nodes table should have 3 rows, and dependent tables should have
proportional amounts.

Some tables might need 0 rows if they're not relevant to the scenario.
Be reasonable — enough data to tell the story, but not excessive.

Call the plan_rows tool with your decisions."""

    response: LLMResponse = client.chat(
        model=MODEL,
        messages=[Message(role="user", content=prompt)],
        tools=[
            ToolDef(
                name="plan_rows",
                description="Specify how many rows to generate for each table",
                parameters={
                    "type": "object",
                    "properties": plan_tool_properties,
                    "required": seeding_order,
                },
            )
        ],
        max_tokens=16_000,
        system=f"You are planning data generation for an OpenShift monitoring database.\n\nCOMPLETE DATABASE SCHEMA:\n{schema_summary}",
        thinking_budget=10_000,
    )

    tc = response.tool_calls[0]
    return {k: int(v) for k, v in tc.arguments.items()}


# ---------------------------------------------------------------------------
# Row generation
# ---------------------------------------------------------------------------


def build_user_prompt(
    meta: db.TableMeta,
    rows_count: int,
    prev_table_rows: dict[str, list[dict]],
    generated: list[dict] | None = None,
) -> str:
    """Build the user prompt for generating a specific table's rows.

    Args:
        generated: rows already generated for THIS table on a prior attempt.
                   When present, the prompt asks for only the remaining rows.
    """
    col_lines: list[str] = []
    for col in meta.columns:
        parts: list[str] = [col.name, col.udt_name]
        if not col.is_nullable:
            parts.append("NOT NULL")
        if col.column_default:
            parts.append(f"DEFAULT {col.column_default}")
        col_lines.append(" | ".join(parts))

    pk_str: str = ", ".join(meta.primary_key) if meta.primary_key else "none"

    fk_lines: list[str] = []
    for fk in meta.foreign_keys:
        fk_lines.append(
            f"  {fk.column_name} -> {fk.foreign_table}({fk.foreign_column})"
        )
    fk_str: str = "\n".join(fk_lines) if fk_lines else "  none"

    unique_lines: list[str] = []
    for _name, cols in meta.unique_constraints.items():
        unique_lines.append(f"  ({', '.join(cols)})")
    unique_str: str = "\n".join(unique_lines) if unique_lines else "  none"

    prev_context: str = ""
    if prev_table_rows:
        prev_context = "\n\nPREVIOUSLY GENERATED DATA FOR OTHER TABLES (use these for foreign key references and consistency):\n"
        for table_name, rows in prev_table_rows.items():
            prev_context += f"\n--- {table_name} ({len(rows)} rows) ---\n"
            prev_context += json.dumps(rows, indent=2) + "\n"

    already_generated: str = ""
    if generated:
        remaining: int = rows_count - len(generated)
        already_generated = (
            f"\n\nALREADY GENERATED ROWS FOR THIS TABLE ({len(generated)}/{rows_count}):\n"
            f"{json.dumps(generated, indent=2)}\n\n"
            f"Generate {remaining} more row(s). Do NOT repeat any row above."
        )

    target: int = rows_count - len(generated) if generated else rows_count
    return f"""Generate exactly {target} rows for table '{meta.table_name}'.

Table: {meta.table_name}

Columns:
{chr(10).join(col_lines)}

Primary key: {pk_str}
Foreign keys:
{fk_str}
Unique constraints:
{unique_str}
{prev_context}{already_generated}

Generate data that fits the scenario. Call the insert_row tool once per row, using parallel tool calls. You must produce exactly {target} insert_row calls."""

def generate_rows(
    client: LLMClient,
    meta: db.TableMeta,
    prev_table_rows: dict[str, list[dict]],
    rows_count: int,
    system_prompt: str,
) -> list[dict]:
    """Generate seed data for one table via parallel tool calls (one per row).

    If the LLM returns fewer rows than requested, rebuild a fresh user message
    with the already-generated rows baked in and retry.
    """
    tool_schema: dict = build_row_schema(meta)
    tool = ToolDef(
        name="insert_row",
        description=f"Insert one generated seed row for table '{meta.table_name}'",
        parameters=tool_schema,
    )

    rows: list[dict] = []

    while len(rows) < rows_count:
        messages: list[Message] = [
            Message(role="user", content=build_user_prompt(meta, rows_count, prev_table_rows, rows or None)),
        ]

        response: LLMResponse = client.chat(
            model=MODEL,
            messages=messages,
            tools=[tool],
            max_tokens=100_000,
            system=system_prompt,
            thinking_budget=30_000,
        )

        new_rows: list[dict] = [tc.arguments for tc in response.tool_calls]
        rows.extend(new_rows)

    return rows[:rows_count]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DB_DSN: str = "host=127.0.0.1 port=5433 dbname=openshift_cluster user=postgres"

DEFAULT_SCENARIO: str = (
    "A 3-node OpenShift 4.14 cluster running a mix of web application and "
    "database workloads. One node is experiencing memory pressure with several "
    "pods in CrashLoopBackOff state. There are active alerts for high memory "
    "usage and pod restart counts."
)


def generate_seed_data(
    scenario: str,
    llm_client: LLMClient,
    db_dsn: str = DB_DSN,
) -> dict[str, list[dict]]:
    conn = psycopg2.connect(db_dsn)
    tables = db.get_seeding_order(conn)
    seeding_order: list[str] = [t.table_name for t in tables]

    all_metas: dict[str, db.TableMeta] = {}
    for table_name in seeding_order:
        all_metas[table_name] = db.get_table_metadata(conn, table_name)

    conn.close()

    schema_summary: str = db.build_full_schema_summary(all_metas, seeding_order)

    print(f"Scenario: {scenario}\n")
    print("Planning row counts...")
    row_counts: dict[str, int] = plan_row_counts(llm_client, scenario, schema_summary, seeding_order)
    print("\nPlanned row counts:")
    for table_name, count in row_counts.items():
        print(f"  {table_name}: {count}")
    print()

    system_prompt: str = build_system_prompt(scenario, schema_summary)

    generated: dict[str, list[dict]] = {}

    for i, table_name in enumerate(seeding_order):
        row_count: int = row_counts.get(table_name, 0)
        if row_count == 0:
            print(f"[{i + 1}/{len(seeding_order)}] Skipping {table_name} (0 rows)")
            generated[table_name] = []
            continue

        print(
            f"[{i + 1}/{len(seeding_order)}] Generating {row_count} rows "
            f"for: {table_name} ..."
        )

        meta: db.TableMeta = all_metas[table_name]
        rows: list[dict] = generate_rows(llm_client, meta, generated, row_count, system_prompt)
        generated[table_name] = rows

        print(f"  -> {len(rows)} rows generated")

    return generated


if __name__ == "__main__":
    scenario: str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    client: LLMClient = AnthropicVertexClient()
    seed_data: dict[str, list[dict]] = generate_seed_data(scenario=scenario, llm_client=client)

    out_path: Path = Path(__file__).parent / "seed_data.json"
    out_path.write_text(json.dumps(seed_data, indent=2) + "\n")
    print(f"\nWrote seed data to {out_path}")

