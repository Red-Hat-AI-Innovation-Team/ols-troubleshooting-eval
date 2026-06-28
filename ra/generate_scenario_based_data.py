"""Generate scenario-based seed data for the world model DB using Anthropic Vertex AI.

Usage:
    uv run python generate_scenario_based_data.py "A 3-node OpenShift cluster experiencing memory pressure"

Env vars:
    CLOUD_ML_REGION              - Vertex AI region (default: us-east5)
    ANTHROPIC_VERTEX_PROJECT_ID  - GCP project ID
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import psycopg2.extras
from anthropic import AnthropicVertex

from db import get_seeding_order


# ---------------------------------------------------------------------------
# PG metadata extraction
# ---------------------------------------------------------------------------


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    udt_name: str
    max_length: int | None
    is_nullable: bool
    column_default: str | None


@dataclass
class ForeignKey:
    column_name: str
    foreign_table: str
    foreign_column: str


@dataclass
class TableMeta:
    table_name: str
    columns: list[ColumnMeta]
    primary_key: list[str]
    foreign_keys: list[ForeignKey]
    unique_constraints: dict[str, list[str]]


def get_table_metadata(conn, table_name: str, schema: str = "public") -> TableMeta:
    params = {"schema": schema, "table": table_name}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT column_name, data_type, udt_name,
               character_maximum_length, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %(schema)s AND table_name = %(table)s
        ORDER BY ordinal_position
    """,
        params,
    )
    columns = [
        ColumnMeta(
            name=r["column_name"],
            data_type=r["data_type"],
            udt_name=r["udt_name"],
            max_length=r["character_maximum_length"],
            is_nullable=r["is_nullable"] == "YES",
            column_default=r["column_default"],
        )
        for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = %(schema)s
          AND tc.table_name = %(table)s
        ORDER BY kcu.ordinal_position
    """,
        params,
    )
    primary_key = [r["column_name"] for r in cur.fetchall()]

    cur.execute(
        """
        SELECT kcu.column_name,
               ccu.table_name AS foreign_table,
               ccu.column_name AS foreign_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
         AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %(schema)s
          AND tc.table_name = %(table)s
    """,
        params,
    )
    foreign_keys = [
        ForeignKey(
            column_name=r["column_name"],
            foreign_table=r["foreign_table"],
            foreign_column=r["foreign_column"],
        )
        for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT tc.constraint_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'UNIQUE'
          AND tc.table_schema = %(schema)s
          AND tc.table_name = %(table)s
        ORDER BY tc.constraint_name, kcu.ordinal_position
    """,
        params,
    )
    uniques: dict[str, list[str]] = {}
    for r in cur.fetchall():
        uniques.setdefault(r["constraint_name"], []).append(r["column_name"])

    cur.close()
    return TableMeta(
        table_name=table_name,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        unique_constraints=uniques,
    )


# ---------------------------------------------------------------------------
# Schema summary builder
# ---------------------------------------------------------------------------


def format_table_summary(meta: TableMeta) -> str:
    """Format a single table's schema as a compact text block."""
    lines = [f"TABLE: {meta.table_name}"]
    lines.append("  Columns:")
    for col in meta.columns:
        parts = [f"    {col.name} {col.udt_name}"]
        if not col.is_nullable:
            parts.append("NOT NULL")
        if col.column_default:
            parts.append(f"DEFAULT {col.column_default}")
        if col.name in meta.primary_key:
            parts.append("[PK]")
        lines.append(" ".join(parts))

    if meta.foreign_keys:
        lines.append("  Foreign keys:")
        for fk in meta.foreign_keys:
            lines.append(
                f"    {fk.column_name} -> {fk.foreign_table}({fk.foreign_column})"
            )

    if meta.unique_constraints:
        lines.append("  Unique constraints:")
        for _name, cols in meta.unique_constraints.items():
            lines.append(f"    ({', '.join(cols)})")

    return "\n".join(lines)


def build_full_schema_summary(all_metas: dict[str, TableMeta], seeding_order: list[str]) -> str:
    """Build a complete schema summary for all tables in seeding order."""
    sections: list[str] = []
    for table_name in seeding_order:
        if table_name in all_metas:
            sections.append(format_table_summary(all_metas[table_name]))
    return "\n\n".join(sections)


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


def build_row_schema(meta: TableMeta) -> dict:
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


def build_tool_schema(meta: TableMeta, rows_count: int) -> dict:
    row_schema: dict = build_row_schema(meta)
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": row_schema,
                "minItems": rows_count,
                "maxItems": rows_count,
            }
        },
        "required": ["rows"],
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


def plan_row_counts(
    client: AnthropicVertex,
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

    response = client.messages.create(
        model="claude-opus-4-6@default",
        max_tokens=16_000,
        thinking={
            "type": "enabled",
            "budget_tokens": 10_000,
        },
        system=f"""You are planning data generation for an OpenShift monitoring database.

COMPLETE DATABASE SCHEMA:
{schema_summary}""",
        tools=[
            {
                "name": "plan_rows",
                "description": "Specify how many rows to generate for each table",
                "input_schema": {
                    "type": "object",
                    "properties": plan_tool_properties,
                    "required": seeding_order,
                },
            }
        ],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": prompt}],
    )

    tool_block = next(b for b in response.content if b.type == "tool_use")
    return {k: int(v) for k, v in tool_block.input.items()}


# ---------------------------------------------------------------------------
# Row generation
# ---------------------------------------------------------------------------


def build_user_prompt(
    meta: TableMeta,
    rows_count: int,
    generated: dict[str, list[dict]],
) -> str:
    """Build the user prompt for generating a specific table's rows."""
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
    if generated:
        prev_context = "\n\nPREVIOUSLY GENERATED DATA (use these for foreign key references and consistency):\n"
        for table_name, rows in generated.items():
            prev_context += f"\n--- {table_name} ({len(rows)} rows) ---\n"
            prev_context += json.dumps(rows, indent=2) + "\n"

    return f"""Generate exactly {rows_count} rows for table '{meta.table_name}'.

Table: {meta.table_name}

Columns:
{chr(10).join(col_lines)}

Primary key: {pk_str}
Foreign keys:
{fk_str}
Unique constraints:
{unique_str}
{prev_context}

Generate data that fits the scenario. Call the insert_rows tool with the generated rows."""


def generate_rows(
    client: AnthropicVertex,
    meta: TableMeta,
    generated: dict[str, list[dict]],
    rows_count: int,
    system_prompt: str,
) -> list[dict]:
    """Generate seed data for one table via structured LLM call."""
    tool_schema: dict = build_tool_schema(meta, rows_count)
    user_prompt: str = build_user_prompt(meta, rows_count, generated)

    with client.messages.stream(
        model="claude-opus-4-6@default",
        max_tokens=100_000,
        thinking={
            "type": "enabled",
            "budget_tokens": 30_000,
        },
        system=system_prompt,
        tools=[
            {
                "name": "insert_rows",
                "description": f"Insert generated seed rows for table '{meta.table_name}'",
                "input_schema": tool_schema,
            }
        ],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    tool_block = next(b for b in response.content if b.type == "tool_use")
    rows = tool_block.input["rows"]

    # Guard: Anthropic SDK can return a string instead of parsed JSON for
    # large tool outputs.  Detect and attempt recovery.
    if isinstance(rows, str):
        rows = json.loads(rows)
    if not isinstance(rows, list) or (rows and not isinstance(rows[0], dict)):
        raise ValueError(
            f"Table '{meta.table_name}': expected list[dict] from tool output, "
            f"got {type(rows).__name__} "
            f"(first element: {type(rows[0]).__name__ if rows else 'empty'})"
        )

    return rows


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
    scenario: str = DEFAULT_SCENARIO,
    db_dsn: str = DB_DSN,
) -> None:
    conn = psycopg2.connect(db_dsn)
    tables = get_seeding_order(conn)
    seeding_order: list[str] = [t.table_name for t in tables]

    all_metas: dict[str, TableMeta] = {}
    for table_name in seeding_order:
        all_metas[table_name] = get_table_metadata(conn, table_name)

    conn.close()

    schema_summary: str = build_full_schema_summary(all_metas, seeding_order)

    client: AnthropicVertex = AnthropicVertex()

    print(f"Scenario: {scenario}\n")
    print("Planning row counts...")
    row_counts: dict[str, int] = plan_row_counts(client, scenario, schema_summary, seeding_order)
    print("\nPlanned row counts:")
    for table_name, count in row_counts.items():
        print(f"  {table_name}: {count}")
    print()

    system_prompt: str = build_system_prompt(scenario, schema_summary)

    generated: dict[str, list[dict]] = {}

    for i, table_name in enumerate(seeding_order):
        count: int = row_counts.get(table_name, 0)
        if count == 0:
            print(f"[{i + 1}/{len(seeding_order)}] Skipping {table_name} (0 rows)")
            generated[table_name] = []
            continue

        print(
            f"[{i + 1}/{len(seeding_order)}] Generating {count} rows "
            f"for: {table_name} ..."
        )

        meta: TableMeta = all_metas[table_name]
        rows: list[dict] = generate_rows(client, meta, generated, count, system_prompt)
        generated[table_name] = rows

        print(f"  -> {len(rows)} rows generated")

    out_path: Path = Path(__file__).parent / "seed_data.json"
    out_path.write_text(json.dumps(generated, indent=2) + "\n")
    print(f"\nWrote seed data to {out_path}")


if __name__ == "__main__":
    scenario: str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    generate_seed_data(scenario=scenario)

