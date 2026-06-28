"""Generate dummy seed data for the world model DB using Anthropic Vertex AI.

Usage:
    uv run python generate_seed_data.py

Env vars:
    CLOUD_ML_REGION              - Vertex AI region (default: us-east5)
    ANTHROPIC_VERTEX_PROJECT_ID  - GCP project ID
"""

import json
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
    unique_constraints: dict[str, list[str]]  # constraint_name -> columns


def get_table_metadata(conn, table_name: str, schema: str = "public") -> TableMeta:
    params = {"schema": schema, "table": table_name}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Columns
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

    # Primary key
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

    # Foreign keys
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

    # Unique constraints
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
# Dynamic JSON schema builder
# ---------------------------------------------------------------------------

PG_TO_JSON_TYPE: dict[str, dict] = {
    # integers
    "int2": {"type": "integer"},
    "int4": {"type": "integer"},
    "int8": {"type": "integer"},
    # floats
    "float4": {"type": "number"},
    "float8": {"type": "number"},
    "numeric": {"type": "number"},
    # boolean
    "bool": {"type": "boolean"},
    # strings
    "varchar": {"type": "string"},
    "text": {"type": "string"},
    "char": {"type": "string"},
    "bpchar": {"type": "string"},
    "name": {"type": "string"},
    # uuid
    "uuid": {"type": "string"},
    # timestamps
    "timestamptz": {"type": "string"},
    "timestamp": {"type": "string"},
    "date": {"type": "string"},
    # network
    "inet": {"type": "string"},
    "cidr": {"type": "string"},
    # json
    "json": {},
    "jsonb": {},
    # arrays (pg prefixes with _)
    "_text": {"type": "array", "items": {"type": "string"}},
    "_int4": {"type": "array", "items": {"type": "integer"}},
    "_varchar": {"type": "array", "items": {"type": "string"}},
}


def build_row_schema(meta: TableMeta) -> dict:
    """Build a JSON schema for a single row of the table."""
    properties: dict[str, dict] = {}
    required: list[str] = []

    for col in meta.columns:
        schema = PG_TO_JSON_TYPE.get(col.udt_name, {"type": "string"}).copy()

        # Description with pg type info
        desc = f"pg type: {col.udt_name}"
        if col.max_length:
            desc += f", max_length: {col.max_length}"
        if col.name in meta.primary_key:
            desc += ", PRIMARY KEY"
        if col.column_default:
            desc += f", default: {col.column_default}"
        schema["description"] = desc

        # Nullable: allow null
        if col.is_nullable and "type" in schema:
            t = schema["type"]
            schema["type"] = [t, "null"] if isinstance(t, str) else t + ["null"]

        properties[col.name] = schema

        # Required: non-nullable without defaults, plus SERIAL PKs
        has_serial_default = col.column_default and "nextval" in col.column_default
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
    """Build the full tool input_schema: {rows: [...]}."""
    row_schema = build_row_schema(meta)
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
# LLM call
# ---------------------------------------------------------------------------


def build_prompt(meta: TableMeta, generated: dict[str, list[dict]]) -> str:
    """Build the user prompt with table metadata and referenced data."""
    col_lines = []
    for col in meta.columns:
        parts = [col.name, col.udt_name]
        if not col.is_nullable:
            parts.append("NOT NULL")
        if col.column_default:
            parts.append(f"DEFAULT {col.column_default}")
        col_lines.append(" | ".join(parts))

    pk_str = ", ".join(meta.primary_key) if meta.primary_key else "none"

    fk_lines = []
    for fk in meta.foreign_keys:
        fk_lines.append(
            f"  {fk.column_name} -> {fk.foreign_table}({fk.foreign_column})"
        )
    fk_str = "\n".join(fk_lines) if fk_lines else "  none"

    unique_lines = []
    for _name, cols in meta.unique_constraints.items():
        unique_lines.append(f"  ({', '.join(cols)})")
    unique_str = "\n".join(unique_lines) if unique_lines else "  none"

    # Referenced data
    ref_context = ""
    if meta.foreign_keys:
        seen = set()
        for fk in meta.foreign_keys:
            if fk.foreign_table in generated and fk.foreign_table not in seen:
                ref_context += (
                    f"\nExisting rows in '{fk.foreign_table}':\n"
                    f"{json.dumps(generated[fk.foreign_table], indent=2)}\n"
                )
                seen.add(fk.foreign_table)

    return f"""Generate realistic dummy data for this PostgreSQL table.
This is part of an OpenShift/Kubernetes cluster monitoring system.

Table: {meta.table_name}

Columns:
{chr(10).join(col_lines)}

Primary key: {pk_str}
Foreign keys:
{fk_str}
Unique constraints:
{unique_str}
{ref_context}
Requirements:
- Realistic OpenShift/Kubernetes values (names, IPs, versions, labels)
- SERIAL/BIGSERIAL PKs: sequential integers starting from 1
- UUID fields: valid v4 UUIDs
- TIMESTAMPTZ: recent ISO-8601 timestamps (2025-06 timeframe)
- Foreign keys MUST reference IDs from the referenced table data above
- JSONB fields: realistic JSON matching the column's semantic purpose
- Array types (text[]): realistic arrays
- Data should be internally consistent (e.g., used < capacity)

Call the insert_rows tool with the generated rows."""


def generate_rows(
    client: AnthropicVertex,
    meta: TableMeta,
    generated: dict[str, list[dict]],
    rows_count: int,
) -> list[dict]:
    """Generate seed data for one table via structured LLM call."""
    tool_schema = build_tool_schema(meta, rows_count)
    prompt = build_prompt(meta, generated)

    with client.messages.stream(
        model="claude-opus-4-6@default",
        max_tokens=100_000,
        thinking={
            "type": "enabled",
            "budget_tokens": 30_000,
        },
        tools=[
            {
                "name": "insert_rows",
                "description": f"Insert generated seed rows for table '{meta.table_name}'",
                "input_schema": tool_schema,
            }
        ],
        # tool_choice={"type": "tool", "name": "insert_rows"},
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    tool_block = next(b for b in response.content if b.type == "tool_use")
    return tool_block.input["rows"]


# ---------------------------------------------------------------------------
# Insert into PG
# ---------------------------------------------------------------------------

# udt_names that should be wrapped with psycopg2.extras.Json
JSONB_TYPES = {"json", "jsonb"}


def insert_rows(conn, meta: TableMeta, rows: list[dict]) -> None:
    """Insert generated rows into the database."""
    if not rows:
        return

    cur = conn.cursor()
    # Build a set of jsonb column names for this table
    jsonb_cols = {col.name for col in meta.columns if col.udt_name in JSONB_TYPES}

    for row in rows:
        cols = list(row.keys())
        placeholders = []
        values = []
        for col_name in cols:
            v = row[col_name]
            if col_name in jsonb_cols and v is not None:
                placeholders.append("%s")
                values.append(psycopg2.extras.Json(v))
            else:
                placeholders.append("%s")
                values.append(v)

        sql = (
            f"INSERT INTO {meta.table_name} ({', '.join(cols)}) "
            f"VALUES ({', '.join(placeholders)})"
        )
        cur.execute(sql, values)

    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ROWS_PER_TABLE = 3
DB_DSN = "host=127.0.0.1 port=5433 dbname=openshift_cluster user=postgres"


def generate_seed_data(
    db_dsn: str = DB_DSN,
    rows_per_table: int = ROWS_PER_TABLE,
):
    conn = psycopg2.connect(db_dsn)
    tables = get_seeding_order(conn)

    client = AnthropicVertex()

    generated: dict[str, list[dict]] = {}

    for i, table in enumerate(tables):
        print(
            f"[{i + 1}/{len(tables)}] Generating {rows_per_table} rows "
            f"for: {table.table_name} ..."
        )

        meta = get_table_metadata(conn, table.table_name)
        print('Table metadata')
        print(meta)
        print('\n---\n')
        rows = generate_rows(client, meta, generated, rows_per_table)
        generated[table.table_name] = rows

        insert_rows(conn, meta, rows)
        print(f"  -> {len(rows)} rows generated and inserted")

    conn.close()

    out_path = Path(__file__).parent / "seed_data.json"
    out_path.write_text(json.dumps(generated, indent=2) + "\n")
    print(f"\nWrote seed data to {out_path}")


if __name__ == "__main__":
    generate_seed_data()
