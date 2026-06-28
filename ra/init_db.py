"""Create, populate, and tear down the openshift_cluster database.

Usage:
    uv run python init_db.py init seed_data.json   # create DB + schema + insert data
    uv run python init_db.py teardown               # drop DB entirely
"""

import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras


PG_HOST = "127.0.0.1"
PG_PORT = 5433
PG_USER = "postgres"
DB_NAME = "openshift_cluster"

ADMIN_DSN = f"host={PG_HOST} port={PG_PORT} dbname=postgres user={PG_USER}"
DB_DSN = f"host={PG_HOST} port={PG_PORT} dbname={DB_NAME} user={PG_USER}"

SCHEMA_PATH = Path(__file__).parent / "world_model_db_schema.sql"


def _get_jsonb_columns(conn, table_name: str) -> set[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND udt_name IN ('json', 'jsonb')",
        [table_name],
    )
    cols = {r[0] for r in cur.fetchall()}
    cur.close()
    return cols


def init_db(seed_path: Path) -> None:
    """Create DB, apply schema, load seed data, advance sequences."""
    # Drop + create DB
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
    cur.execute(f"CREATE DATABASE {DB_NAME}")
    cur.close()
    admin.close()
    print(f"Created database: {DB_NAME}")

    conn = psycopg2.connect(DB_DSN)

    # Apply schema
    schema_sql = SCHEMA_PATH.read_text()
    cur = conn.cursor()
    cur.execute(schema_sql)
    conn.commit()
    cur.close()
    print(f"Applied schema from: {SCHEMA_PATH.name}")

    # Insert seed data (keys are in seeding order)
    seed = json.loads(seed_path.read_text())
    total_rows = 0
    for table_name, rows in seed.items():
        if not rows:
            continue

        jsonb_cols = _get_jsonb_columns(conn, table_name)
        cur = conn.cursor()

        for row in rows:
            cols = list(row.keys())
            values = []
            for col_name in cols:
                v = row[col_name]
                if col_name in jsonb_cols and v is not None:
                    values.append(psycopg2.extras.Json(v))
                else:
                    values.append(v)

            placeholders = ", ".join(["%s"] * len(cols))
            sql = (
                f"INSERT INTO {table_name} ({', '.join(cols)}) "
                f"VALUES ({placeholders})"
            )
            cur.execute(sql, values)

        conn.commit()
        cur.close()
        total_rows += len(rows)
        print(f"  {table_name}: {len(rows)} rows")

    # Advance SERIAL sequences past inserted IDs
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, column_name, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_default LIKE 'nextval%%'"
    )
    for tbl, col, col_default in cur.fetchall():
        seq_name = col_default.split("'")[1]
        cur.execute(
            f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({col}) FROM {tbl}), 1))"
        )
    conn.commit()
    cur.close()
    conn.close()

    print(f"\nDone: {total_rows} total rows inserted into {DB_NAME}")


def teardown_db() -> None:
    """Drop the database entirely."""
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
    cur.close()
    admin.close()
    print(f"Dropped database: {DB_NAME}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  uv run python init_db.py init seed_data.json")
        print("  uv run python init_db.py teardown")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "init":
        seed_file = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "seed_data.json"
        init_db(seed_file)
    elif cmd == "teardown":
        teardown_db()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
