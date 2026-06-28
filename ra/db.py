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


def _create_database() -> None:
  safe_name: str = _validate_identifier(DB_NAME)
  with connect(ADMIN_DSN, autocommit=True) as admin:
    with admin.cursor() as cur:
      cur.execute(f"DROP DATABASE IF EXISTS {safe_name}")
      cur.execute(f"CREATE DATABASE {safe_name}")
  print(f"Created database: {DB_NAME}")


def _apply_schema(conn: psycopg2.extensions.connection) -> None:
  schema_sql: str = SCHEMA_PATH.read_text()
  with conn.cursor() as cur:
    cur.execute(schema_sql)
  conn.commit()
  print(f"Applied schema from: {SCHEMA_PATH.name}")


# ---------------------------------------------------------------------------
# Seed data insertion
# ---------------------------------------------------------------------------


def _insert_seed_data(conn: psycopg2.extensions.connection, seed_path: Path) -> int:
  seed: dict[str, list[dict]] = json.loads(seed_path.read_text())
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


def init_db(seed_path: Path) -> None:
  """Create DB, apply schema, load seed data, advance sequences."""
  _create_database()

  with connect(DB_DSN) as conn:
    _apply_schema(conn)
    total_rows: int = _insert_seed_data(conn, seed_path)
    _advance_sequences(conn)

  print(f"\nDone: {total_rows} total rows inserted into {DB_NAME}")


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


def teardown_db() -> None:
  """Drop the database entirely."""
  safe_name: str = _validate_identifier(DB_NAME)
  with connect(ADMIN_DSN, autocommit=True) as admin:
    with admin.cursor() as cur:
      cur.execute(f"DROP DATABASE IF EXISTS {safe_name}")
  print(f"Dropped database: {DB_NAME}")


if __name__ == "__main__":
  if len(sys.argv) < 2:
    print("Usage:")
    print("  uv run python db.py init seed_data.json")
    print("  uv run python db.py teardown")
    sys.exit(1)

  cmd: str = sys.argv[1]
  if cmd == "init":
    seed_file: Path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "seed_data.json"
    init_db(seed_file)
  elif cmd == "teardown":
    teardown_db()
  else:
    print(f"Unknown command: {cmd}")
    sys.exit(1)

