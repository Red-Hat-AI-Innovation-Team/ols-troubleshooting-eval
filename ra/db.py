"""DB connection, schema init, and teardown.

Usage:
    uv run python db.py init seed_data.json   # create DB + schema + insert data
    uv run python db.py teardown               # drop DB entirely
"""

import json
import re
import sys
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import psycopg2
import psycopg2.extras


PG_HOST: str = "127.0.0.1"
PG_PORT: int = 5433
PG_USER: str = "postgres"
DB_NAME: str = "openshift_cluster"

ADMIN_DSN: str = f"host={PG_HOST} port={PG_PORT} dbname=postgres user={PG_USER}"
DB_DSN: str = f"host={PG_HOST} port={PG_PORT} dbname={DB_NAME} user={PG_USER}"


def _dsn_for(db_name: str) -> str:
  return f"host={PG_HOST} port={PG_PORT} dbname={db_name} user={PG_USER}"

SCHEMA_PATH: Path = Path(__file__).parent / "world_model_db_schema.sql"

_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> str:
  if not _IDENTIFIER_RE.match(name):
    raise ValueError(f"Invalid SQL identifier: {name!r}")
  return name


@contextmanager
def connect(dsn: str = DB_DSN, autocommit: bool = False) -> Generator[psycopg2.extensions.connection, None, None]:
  conn: psycopg2.extensions.connection = psycopg2.connect(dsn)
  if autocommit:
    conn.autocommit = True
  try:
    yield conn
  finally:
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_jsonb_columns(conn: psycopg2.extensions.connection, table_name: str) -> set[str]:
  with conn.cursor() as cur:
    cur.execute(
      "SELECT column_name FROM information_schema.columns "
      "WHERE table_schema = 'public' AND table_name = %s AND udt_name IN ('json', 'jsonb')",
      [table_name],
    )
    return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------


def _create_database(db_name: str | None = None) -> None:
  name: str = db_name or DB_NAME
  safe_name: str = _validate_identifier(name)
  with connect(ADMIN_DSN, autocommit=True) as admin:
    with admin.cursor() as cur:
      cur.execute(f"DROP DATABASE IF EXISTS {safe_name}")
      cur.execute(f"CREATE DATABASE {safe_name}")
  print(f"Created database: {name}")


def _apply_schema(conn: psycopg2.extensions.connection) -> None:
  schema_sql: str = SCHEMA_PATH.read_text()
  with conn.cursor() as cur:
    cur.execute(schema_sql)
  conn.commit()
  print(f"Applied schema from: {SCHEMA_PATH.name}")


# ---------------------------------------------------------------------------
# Seed data insertion
# ---------------------------------------------------------------------------


def _insert_seed_data(conn: psycopg2.extensions.connection, seed: dict[str, list[dict]]) -> int:
  total_rows: int = 0

  for table_name, rows in seed.items():
    if not rows:
      continue

    safe_table: str = _validate_identifier(table_name)
    jsonb_cols: set[str] = _get_jsonb_columns(conn, table_name)

    with conn.cursor() as cur:
      for row in rows:
        cols: list[str] = list(row.keys())
        safe_cols: list[str] = [_validate_identifier(c) for c in cols]
        values: list = []
        for col_name in cols:
          v = row[col_name]
          if col_name in jsonb_cols and v is not None:
            values.append(psycopg2.extras.Json(v))
          else:
            values.append(v)

        placeholders: str = ", ".join(["%s"] * len(cols))
        col_list: str = ", ".join(safe_cols)
        sql: str = f"INSERT INTO {safe_table} ({col_list}) VALUES ({placeholders})"
        cur.execute(sql, values)

    conn.commit()
    total_rows += len(rows)
    print(f"  {table_name}: {len(rows)} rows")

  return total_rows


def _advance_sequences(conn: psycopg2.extensions.connection) -> None:
  with conn.cursor() as cur:
    cur.execute(
      "SELECT table_name, column_name, column_default "
      "FROM information_schema.columns "
      "WHERE table_schema = 'public' AND column_default LIKE 'nextval%%'"
    )
    seq_rows: list[tuple[str, str, str]] = cur.fetchall()

    for tbl, col, col_default in seq_rows:
      safe_tbl: str = _validate_identifier(tbl)
      safe_col: str = _validate_identifier(col)
      seq_name: str = col_default.split("'")[1]
      _validate_identifier(seq_name.split(".")[-1])
      cur.execute(
        "SELECT setval(%s, COALESCE((SELECT MAX({col}) FROM {tbl}), 1))".format(
          col=safe_col, tbl=safe_tbl
        ),
        [seq_name],
      )
  conn.commit()


# ---------------------------------------------------------------------------
# Init / Teardown
# ---------------------------------------------------------------------------


def init_db(seed: dict[str, list[dict]], db_name: str | None = None) -> None:
  """Create DB, apply schema, load seed data, advance sequences."""
  name: str = db_name or DB_NAME
  _create_database(name)

  with connect(_dsn_for(name)) as conn:
    _apply_schema(conn)
    total_rows: int = _insert_seed_data(conn, seed)
    _advance_sequences(conn)

  print(f"\nDone: {total_rows} total rows inserted into {name}")


# ---------------------------------------------------------------------------
# Schema introspection
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


def get_table_metadata(conn: psycopg2.extensions.connection, table_name: str, schema: str = "public") -> TableMeta:
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
# Seeding order (topological sort by FK dependencies)
# ---------------------------------------------------------------------------


@dataclass
class Table:
  table_name: str
  references: list[str] | None = None


def get_seeding_order(conn: psycopg2.extensions.connection) -> list[Table]:
  with conn.cursor() as cur:
    cur.execute("""
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    """)
    all_tables: set[str] = {row[0] for row in cur.fetchall()}

    cur.execute("""
      SELECT
        tc.table_name AS child_table,
        ccu.table_name AS parent_table
      FROM information_schema.table_constraints tc
      JOIN information_schema.referential_constraints rc
        ON tc.constraint_name = rc.constraint_name
        AND tc.constraint_schema = rc.constraint_schema
      JOIN information_schema.constraint_column_usage ccu
        ON rc.unique_constraint_name = ccu.constraint_name
        AND rc.unique_constraint_schema = ccu.constraint_schema
      WHERE tc.constraint_type = 'FOREIGN KEY'
        AND tc.table_schema = 'public'
    """)

    dependencies: defaultdict[str, set[str]] = defaultdict(set)
    for child, parent in cur.fetchall():
      dependencies[child].add(parent)

  graph: defaultdict[str, list[str]] = defaultdict(list)
  in_degree: dict[str, int] = {t: 0 for t in all_tables}

  for child, parents in dependencies.items():
    for parent in parents:
      graph[parent].append(child)
      in_degree[child] += 1

  queue: deque[str] = deque(sorted(t for t in all_tables if in_degree[t] == 0))
  order: list[str] = []

  while queue:
    table: str = queue.popleft()
    order.append(table)
    for child in sorted(graph[table]):
      in_degree[child] -= 1
      if in_degree[child] == 0:
        queue.append(child)

  if len(order) != len(all_tables):
    raise RuntimeError("Cycle detected in FK dependencies")

  return [
    Table(
      table_name=t,
      references=sorted(dependencies[t]) if dependencies[t] else None,
    )
    for t in order
  ]


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def teardown_db(db_name: str | None = None) -> None:
  """Drop the database entirely."""
  name: str = db_name or DB_NAME
  safe_name: str = _validate_identifier(name)
  with connect(ADMIN_DSN, autocommit=True) as admin:
    with admin.cursor() as cur:
      cur.execute(f"DROP DATABASE IF EXISTS {safe_name}")
  print(f"Dropped database: {name}")


if __name__ == "__main__":
  if len(sys.argv) < 2:
    print("Usage:")
    print("  uv run python db.py init seed_data.json")
    print("  uv run python db.py teardown")
    sys.exit(1)

  cmd: str = sys.argv[1]
  if cmd == "init":
    seed_file: Path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "seed_data.json"
    seed: dict[str, list[dict]] = json.loads(seed_file.read_text())
    init_db(seed)
  elif cmd == "teardown":
    teardown_db()
  else:
    print(f"Unknown command: {cmd}")
    sys.exit(1)

