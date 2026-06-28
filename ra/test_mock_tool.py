"""Tests for mock_tools.py.

Spins up a test_openshift_cluster DB, loads schema + test_data.json,
runs tool assertions, then drops the test DB.

Usage:
    uv run test_mock_tool.py
"""

import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

from mock_tools import call_tool

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PG_HOST = "127.0.0.1"
PG_PORT = 5433
PG_USER = "postgres"
TEST_DB = "test_openshift_cluster"

ADMIN_DSN = f"host={PG_HOST} port={PG_PORT} dbname=postgres user={PG_USER}"
TEST_DSN = f"host={PG_HOST} port={PG_PORT} dbname={TEST_DB} user={PG_USER}"

SCHEMA_PATH = Path(__file__).parent / "world_model_db_schema.sql"
SEED_PATH = Path(__file__).parent / "test_data.json"

# ---------------------------------------------------------------------------
# DB setup / teardown
# ---------------------------------------------------------------------------

JSONB_TYPES = {"json", "jsonb"}


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


def setup_test_db() -> psycopg2.extensions.connection:
    """Create test DB, apply schema, load seed data. Returns test connection."""
    # Admin connection (autocommit for CREATE DATABASE)
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    cur.execute(f"CREATE DATABASE {TEST_DB}")
    cur.close()
    admin.close()

    # Connect to test DB
    conn = psycopg2.connect(TEST_DSN)

    # Apply schema
    schema_sql = SCHEMA_PATH.read_text()
    cur = conn.cursor()
    cur.execute(schema_sql)
    conn.commit()
    cur.close()

    # Load seed data (keys are in seeding order)
    seed = json.loads(SEED_PATH.read_text())
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

    # Advance SERIAL sequences past inserted IDs
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, column_name, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_default LIKE 'nextval%%'"
    )
    for table_name, col_name, col_default in cur.fetchall():
        # Extract sequence name from "nextval('foo_id_seq'::regclass)"
        seq_name = col_default.split("'")[1]
        cur.execute(
            f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({col_name}) FROM {table_name}), 1))"
        )
    conn.commit()
    cur.close()

    return conn


def teardown_test_db(conn: psycopg2.extensions.connection) -> None:
    """Close test connection and drop test DB."""
    conn.close()
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    cur.close()
    admin.close()


# ---------------------------------------------------------------------------
# Helper: read known values from test DB
# ---------------------------------------------------------------------------


def _fetch_one(conn, sql: str, params=None) -> dict | None:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params or [])
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def _fetch_all(conn, sql: str, params=None) -> list[dict]:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params or [])
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_events_list(conn):
    result = call_tool(conn, "events_list")
    assert "# The following events (YAML format) were found:" in result or "# No events found" in result

    # With data, should have YAML content
    events = _fetch_all(conn, "SELECT * FROM events LIMIT 1")
    if events:
        assert "Reason" in result
        assert "Type" in result


def test_events_list_field_selector(conn):
    event = _fetch_one(conn, "SELECT event_type FROM events LIMIT 1")
    if event:
        result = call_tool(conn, "events_list", {"fieldSelector": f"type={event['event_type']}"})
        assert event["event_type"] in result


def test_namespaces_list(conn):
    result = call_tool(conn, "namespaces_list")
    ns = _fetch_one(conn, "SELECT name FROM namespaces LIMIT 1")
    if ns:
        assert ns["name"] in result
    assert "kind: Namespace" in result


def test_namespaces_list_field_selector(conn):
    ns = _fetch_one(conn, "SELECT name FROM namespaces LIMIT 1")
    if ns:
        result = call_tool(conn, "namespaces_list", {"fieldSelector": f"metadata.name={ns['name']}"})
        assert ns["name"] in result


def test_projects_list(conn):
    result = call_tool(conn, "projects_list")
    # May return empty list if no openshift projects
    assert isinstance(result, str)


def test_nodes_log(conn):
    row = _fetch_one(
        conn,
        "SELECT nd.name, nl.source FROM node_logs nl JOIN nodes nd ON nl.node_id = nd.id LIMIT 1",
    )
    if row:
        result = call_tool(conn, "nodes_log", {"name": row["name"], "query": row["source"]})
        assert len(result) > 0
        assert "has not logged" not in result


def test_nodes_log_not_found(conn):
    result = call_tool(conn, "nodes_log", {"name": "nonexistent-node", "query": "kubelet"})
    assert "has not logged" in result or "empty" in result


def test_nodes_stats_summary(conn):
    node = _fetch_one(conn, "SELECT name FROM nodes LIMIT 1")
    if node:
        result = call_tool(conn, "nodes_stats_summary", {"name": node["name"]})
        parsed = json.loads(result)
        assert isinstance(parsed, dict)


def test_nodes_top(conn):
    result = call_tool(conn, "nodes_top")
    assert "NAME" in result or "No metrics" in result
    node = _fetch_one(conn, "SELECT nd.name FROM node_metrics nm JOIN nodes nd ON nm.node_id = nd.id LIMIT 1")
    if node:
        assert node["name"] in result


def test_nodes_top_by_name(conn):
    node = _fetch_one(conn, "SELECT nd.name FROM node_metrics nm JOIN nodes nd ON nm.node_id = nd.id LIMIT 1")
    if node:
        result = call_tool(conn, "nodes_top", {"name": node["name"]})
        assert node["name"] in result


def test_pods_list(conn):
    result = call_tool(conn, "pods_list")
    pod = _fetch_one(conn, "SELECT name FROM pods WHERE deleted_at IS NULL LIMIT 1")
    if pod:
        assert pod["name"] in result
    assert "kind: Pod" in result


def test_pods_list_in_namespace(conn):
    row = _fetch_one(
        conn,
        "SELECT p.name, ns.name AS ns_name FROM pods p JOIN namespaces ns ON p.namespace_id = ns.id WHERE p.deleted_at IS NULL LIMIT 1",
    )
    if row:
        result = call_tool(conn, "pods_list_in_namespace", {"namespace": row["ns_name"]})
        assert row["name"] in result


def test_pods_get(conn):
    pod = _fetch_one(conn, "SELECT name FROM pods WHERE deleted_at IS NULL LIMIT 1")
    if pod:
        result = call_tool(conn, "pods_get", {"name": pod["name"]})
        assert pod["name"] in result
        assert "kind: Pod" in result


def test_pods_get_not_found(conn):
    result = call_tool(conn, "pods_get", {"name": "nonexistent-pod-xyz"})
    assert "not found" in result.lower()


def test_pods_log(conn):
    row = _fetch_one(
        conn,
        "SELECT p.name FROM pod_logs pl JOIN pods p ON pl.pod_id = p.id LIMIT 1",
    )
    if row:
        result = call_tool(conn, "pods_log", {"name": row["name"]})
        assert len(result) > 0
        assert "has not logged" not in result


def test_pods_log_not_found(conn):
    result = call_tool(conn, "pods_log", {"name": "nonexistent-pod-xyz"})
    assert "has not logged" in result


def test_pods_top(conn):
    result = call_tool(conn, "pods_top")
    assert "NAME" in result or "No metrics" in result


def test_pods_exec(conn):
    # Use a real pod from seed data
    pod = _fetch_one(
        conn,
        "SELECT p.name, ns.name AS ns_name FROM pods p JOIN namespaces ns ON p.namespace_id = ns.id WHERE p.deleted_at IS NULL LIMIT 1",
    )
    if pod:
        result = call_tool(conn, "pods_exec", {"name": pod["name"], "namespace": pod["ns_name"], "command": ["ls", "/"]})
        assert isinstance(result, str)
        assert len(result) > 0
    else:
        # No pods — use nonexistent, expect error
        result = call_tool(conn, "pods_exec", {"name": "nonexistent", "command": ["ls"]})
        assert "not found" in result.lower()


def test_pods_run(conn):
    result = call_tool(conn, "pods_run", {"image": "nginx:latest", "name": "test-run"})
    assert "created or updated successfully" in result
    assert "nginx" in result


def test_pods_delete(conn):
    # Insert a throwaway pod to delete
    pod = _fetch_one(conn, "SELECT name FROM pods WHERE deleted_at IS NULL LIMIT 1")
    if pod:
        # Don't actually delete seed data — test with nonexistent
        result = call_tool(conn, "pods_delete", {"name": "nonexistent-pod-xyz"})
        assert "not found" in result.lower()


def test_resources_list(conn):
    ar = _fetch_one(conn, "SELECT api_version, kind FROM api_resources LIMIT 1")
    if ar:
        result = call_tool(
            conn,
            "resources_list",
            {"apiVersion": ar["api_version"], "kind": ar["kind"]},
        )
        assert isinstance(result, str)


def test_resources_get(conn):
    row = _fetch_one(
        conn,
        """SELECT r.name, ar.api_version, ar.kind
           FROM resources r JOIN api_resources ar ON r.api_resource_id = ar.id
           WHERE r.deleted_at IS NULL LIMIT 1""",
    )
    if row:
        result = call_tool(
            conn,
            "resources_get",
            {"apiVersion": row["api_version"], "kind": row["kind"], "name": row["name"]},
        )
        assert isinstance(result, str)
        assert len(result) > 10  # should have YAML content


def test_resources_get_not_found(conn):
    result = call_tool(
        conn,
        "resources_get",
        {"apiVersion": "v1", "kind": "ConfigMap", "name": "nonexistent-xyz"},
    )
    assert "not found" in result.lower()


def test_resources_delete(conn):
    result = call_tool(
        conn,
        "resources_delete",
        {"apiVersion": "v1", "kind": "ConfigMap", "name": "nonexistent-xyz"},
    )
    assert "not found" in result.lower() or "deleted" in result.lower()


def test_resources_scale(conn):
    row = _fetch_one(
        conn,
        """SELECT r.name, ar.api_version, ar.kind
           FROM resources r JOIN api_resources ar ON r.api_resource_id = ar.id
           WHERE r.deleted_at IS NULL LIMIT 1""",
    )
    if row:
        result = call_tool(
            conn,
            "resources_scale",
            {"apiVersion": row["api_version"], "kind": row["kind"], "name": row["name"]},
        )
        assert "scale" in result.lower() or "not found" in result.lower()


def test_resources_create_or_update(conn):
    resource_yaml = json.dumps({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "test-cm", "namespace": None},
        "data": {"key": "value"},
    })
    result = call_tool(conn, "resources_create_or_update", {"resource": resource_yaml})
    assert "created or updated successfully" in result

    # Clean up
    conn.cursor().execute(
        "DELETE FROM resources WHERE name = 'test-cm'"
    )
    conn.commit()


def test_configuration_contexts_list(conn):
    result = call_tool(conn, "configuration_contexts_list")
    ctx = _fetch_one(conn, "SELECT context_name FROM kubeconfig_contexts LIMIT 1")
    if ctx:
        assert ctx["context_name"] in result
    assert "context" in result.lower()


def test_targets_list(conn):
    result = call_tool(conn, "targets_list")
    assert isinstance(result, str)


def test_list_metrics(conn):
    metric = _fetch_one(conn, "SELECT name FROM metrics LIMIT 1")
    if metric:
        # Use a regex that matches the metric name
        result = call_tool(conn, "list_metrics", {"name_regex": ".*"})
        parsed = json.loads(result)
        assert "metrics" in parsed
        assert metric["name"] in parsed["metrics"]


def test_list_metrics_filtered(conn):
    metric = _fetch_one(conn, "SELECT name FROM metrics LIMIT 1")
    if metric:
        # Use first few chars as regex
        prefix = metric["name"][:5]
        result = call_tool(conn, "list_metrics", {"name_regex": f"{prefix}.*"})
        parsed = json.loads(result)
        assert metric["name"] in parsed["metrics"]


def test_execute_instant_query(conn):
    metric = _fetch_one(conn, "SELECT name FROM metrics LIMIT 1")
    if metric:
        result = call_tool(conn, "execute_instant_query", {"query": metric["name"]})
        parsed = json.loads(result)
        assert parsed["resultType"] == "vector"
        assert "result" in parsed


def test_execute_range_query(conn):
    metric = _fetch_one(conn, "SELECT name FROM metrics LIMIT 1")
    if metric:
        result = call_tool(
            conn,
            "execute_range_query",
            {"query": metric["name"], "step": "1m"},
        )
        parsed = json.loads(result)
        assert parsed["resultType"] == "matrix"
        assert "result" in parsed


def test_show_timeseries(conn):
    result = call_tool(
        conn,
        "show_timeseries",
        {"query": "up", "step": "1m"},
    )
    assert result == "{}"


def test_get_label_names(conn):
    result = call_tool(conn, "get_label_names")
    parsed = json.loads(result)
    assert "labels" in parsed
    assert isinstance(parsed["labels"], list)


def test_get_label_names_for_metric(conn):
    metric = _fetch_one(conn, "SELECT name FROM metrics LIMIT 1")
    if metric:
        result = call_tool(conn, "get_label_names", {"metric": metric["name"]})
        parsed = json.loads(result)
        assert "labels" in parsed


def test_get_label_values(conn):
    label = _fetch_one(conn, "SELECT label_name FROM metric_labels LIMIT 1")
    if label:
        result = call_tool(conn, "get_label_values", {"label": label["label_name"]})
        parsed = json.loads(result)
        assert "values" in parsed
        assert isinstance(parsed["values"], list)


def test_get_series(conn):
    metric = _fetch_one(conn, "SELECT name FROM metrics LIMIT 1")
    if metric:
        result = call_tool(conn, "get_series", {"matches": metric["name"]})
        parsed = json.loads(result)
        assert "series" in parsed
        assert "cardinality" in parsed
        assert isinstance(parsed["cardinality"], int)


def test_get_alerts(conn):
    result = call_tool(conn, "get_alerts")
    parsed = json.loads(result)
    assert "alerts" in parsed
    assert isinstance(parsed["alerts"], list)

    alerts = _fetch_all(conn, "SELECT * FROM alerts")
    if alerts:
        assert len(parsed["alerts"]) == len(alerts)


def test_get_alerts_filtered(conn):
    label = _fetch_one(conn, "SELECT label_key, label_value FROM alert_labels LIMIT 1")
    if label:
        result = call_tool(
            conn,
            "get_alerts",
            {"filter": f"{label['label_key']}={label['label_value']}"},
        )
        parsed = json.loads(result)
        assert "alerts" in parsed
        # Filtered result should be <= total
        total = len(_fetch_all(conn, "SELECT * FROM alerts"))
        assert len(parsed["alerts"]) <= total


def test_get_silences(conn):
    result = call_tool(conn, "get_silences")
    parsed = json.loads(result)
    assert "silences" in parsed
    assert isinstance(parsed["silences"], list)

    silences = _fetch_all(conn, "SELECT * FROM silences")
    if silences:
        assert len(parsed["silences"]) == len(silences)


def test_call_tool_unknown(conn):
    result = call_tool(conn, "nonexistent_tool")
    assert "unknown tool" in result.lower()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print("Setting up test database...")
    conn = setup_test_db()
    print(f"Test DB '{TEST_DB}' ready.\n")

    # Collect all test functions
    tests = [
        (name, func)
        for name, func in globals().items()
        if name.startswith("test_") and callable(func)
    ]
    tests.sort(key=lambda t: t[0])

    passed = 0
    failed = 0
    errors: list[tuple[str, str]] = []

    for name, func in tests:
        try:
            func(conn)
            conn.rollback()  # reset transaction state
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            conn.rollback()
            failed += 1
            errors.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            conn.rollback()
            failed += 1
            errors.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")

    if errors:
        print(f"\nFailures:")
        for name, msg in errors:
            print(f"  {name}: {msg}")

    print("\nTearing down test database...")
    teardown_test_db(conn)
    print("Done.")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
