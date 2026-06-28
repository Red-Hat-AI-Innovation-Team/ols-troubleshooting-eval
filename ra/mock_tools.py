"""Mock MCP tools backed by PostgreSQL.

Each tool function: def tool_name(conn, *, param=...) -> str
Output formats match MCP_TOOLS.md spec (YAML, JSON, or plain text).
"""

import json
import re
import shlex
from pathlib import Path
from typing import Any, Callable

import psycopg2.extras
import yaml

from vshell import VFile, VNet, VShell, vfile_from_dict, vnet_from_dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dict_cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _yaml_out(data: Any) -> str:
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


def _parse_field_selector(field_selector: str) -> list[tuple[str, str]]:
    """Parse 'key=value,key2=value2' into [(key, value), ...]."""
    pairs = []
    if not field_selector:
        return pairs
    for part in field_selector.split(","):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            pairs.append((k.strip(), v.strip()))
    return pairs


# ---------------------------------------------------------------------------
# Core: Events
# ---------------------------------------------------------------------------


def events_list(
    conn,
    *,
    namespace: str | None = None,
    fieldSelector: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    query = """
        SELECT e.*, n.name AS namespace_name
        FROM events e
        LEFT JOIN namespaces n ON e.namespace_id = n.id
        WHERE 1=1
    """
    params: list = []

    if namespace:
        query += " AND n.name = %s"
        params.append(namespace)

    for key, val in _parse_field_selector(fieldSelector):
        col_map = {
            "type": "e.event_type",
            "reason": "e.reason",
            "involvedObject.name": "e.involved_object_name",
            "involvedObject.kind": "e.involved_object_kind",
            "involvedObject.namespace": "e.involved_object_namespace",
            "involvedObject.uid": "e.involved_object_uid",
            "source": "e.source_component",
            "reportingComponent": "e.reporting_component",
        }
        if key in col_map:
            query += f" AND {col_map[key]} = %s"
            params.append(val)

    query += " ORDER BY e.resolved_timestamp DESC"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return "# No events found"

    events = []
    for r in rows:
        events.append(
            {
                "Namespace": r.get("namespace_name")
                or r.get("involved_object_namespace", ""),
                "Timestamp": str(r["resolved_timestamp"]),
                "Type": r["event_type"],
                "Reason": r["reason"],
                "InvolvedObject": {
                    "apiVersion": r.get("involved_object_api_version", ""),
                    "Kind": r.get("involved_object_kind", ""),
                    "Name": r.get("involved_object_name", ""),
                },
                "Message": (r["message"] or "").strip(),
            }
        )

    return (
        "# The following events (YAML format) were found:\n" + _yaml_out(events)
    )


# ---------------------------------------------------------------------------
# Core: Namespaces / Projects
# ---------------------------------------------------------------------------


def namespaces_list(
    conn,
    *,
    fieldSelector: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    query = "SELECT * FROM namespaces WHERE 1=1"
    params: list = []

    for key, val in _parse_field_selector(fieldSelector):
        if key == "metadata.name":
            query += " AND name = %s"
            params.append(val)
        elif key == "status.phase":
            query += " AND phase = %s"
            params.append(val)

    query += " ORDER BY name"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    ns_list = []
    for r in rows:
        ns_list.append(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": r["name"],
                    "uid": str(r["uid"]),
                    "labels": r.get("labels", {}),
                    "annotations": r.get("annotations", {}),
                    "creationTimestamp": str(r["created_at"]),
                },
                "status": {"phase": r["phase"]},
            }
        )
    return _yaml_out(ns_list)


def projects_list(conn) -> str:
    cur = _dict_cur(conn)
    cur.execute(
        "SELECT * FROM namespaces WHERE is_openshift_project = TRUE ORDER BY name"
    )
    rows = cur.fetchall()
    cur.close()

    projects = []
    for r in rows:
        projects.append(
            {
                "apiVersion": "project.openshift.io/v1",
                "kind": "Project",
                "metadata": {
                    "name": r["name"],
                    "uid": str(r["uid"]),
                    "labels": r.get("labels", {}),
                    "annotations": r.get("annotations", {}),
                    "creationTimestamp": str(r["created_at"]),
                },
                "status": {"phase": r["phase"]},
            }
        )
    return _yaml_out(projects)


# ---------------------------------------------------------------------------
# Core: Nodes
# ---------------------------------------------------------------------------


def nodes_log(
    conn,
    *,
    name: str,
    query: str,
    tailLines: int | None = None,
) -> str:
    cur = _dict_cur(conn)
    sql = """
        SELECT nl.line_number, nl.timestamp, nl.message
        FROM node_logs nl
        JOIN nodes nd ON nl.node_id = nd.id
        WHERE nd.name = %s AND nl.source = %s
        ORDER BY nl.line_number DESC
    """
    cur.execute(sql, [name, query])
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return (
            f"The node {name} has not logged any message yet "
            "or the log file is empty"
        )

    tail = tailLines if tailLines is not None else 100
    if tail > 0:
        rows = rows[:tail]
    rows.reverse()

    lines = []
    for r in rows:
        ts = str(r["timestamp"]) + " " if r["timestamp"] else ""
        lines.append(f"{ts}{r['message']}")
    return "\n".join(lines)


def nodes_stats_summary(conn, *, name: str) -> str:
    cur = _dict_cur(conn)
    cur.execute(
        """
        SELECT nm.stats_summary_json
        FROM node_metrics nm
        JOIN nodes nd ON nm.node_id = nd.id
        WHERE nd.name = %s
        ORDER BY nm.timestamp DESC LIMIT 1
    """,
        [name],
    )
    row = cur.fetchone()
    cur.close()

    if row and row["stats_summary_json"]:
        return json.dumps(row["stats_summary_json"])

    # Fallback: build minimal summary from metrics columns
    cur2 = _dict_cur(conn)
    cur2.execute(
        """
        SELECT nm.*, nd.allocatable_cpu_millicores, nd.allocatable_memory_bytes
        FROM node_metrics nm
        JOIN nodes nd ON nm.node_id = nd.id
        WHERE nd.name = %s
        ORDER BY nm.timestamp DESC LIMIT 1
    """,
        [name],
    )
    row2 = cur2.fetchone()
    cur2.close()
    if not row2:
        return "{}"
    return json.dumps(
        {
            "node": {
                "cpu": {"usageNanoCores": (row2["cpu_usage_millicores"] or 0) * 1_000_000},
                "memory": {"usageBytes": row2["memory_usage_bytes"] or 0},
            }
        }
    )


def nodes_top(
    conn,
    *,
    name: str | None = None,
    label_selector: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    query = """
        SELECT DISTINCT ON (nd.id)
            nd.name,
            nm.cpu_usage_millicores,
            nm.memory_usage_bytes,
            nd.allocatable_cpu_millicores,
            nd.allocatable_memory_bytes
        FROM node_metrics nm
        JOIN nodes nd ON nm.node_id = nd.id
        WHERE 1=1
    """
    params: list = []

    if name:
        query += " AND nd.name = %s"
        params.append(name)

    if label_selector:
        for part in label_selector.split(","):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                query += " AND nd.labels->>%s = %s"
                params.extend([k.strip(), v.strip()])

    query += " ORDER BY nd.id, nm.timestamp DESC"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return "No metrics available"

    lines = [f"{'NAME':<40} {'CPU(cores)':<14} {'CPU(%)':<10} {'MEMORY(bytes)':<18} {'MEMORY(%)'}"]
    for r in rows:
        cpu_m = r["cpu_usage_millicores"] or 0
        mem_b = r["memory_usage_bytes"] or 0
        alloc_cpu = r["allocatable_cpu_millicores"] or 1
        alloc_mem = r["allocatable_memory_bytes"] or 1
        cpu_pct = int(cpu_m * 100 / alloc_cpu) if alloc_cpu else 0
        mem_pct = int(mem_b * 100 / alloc_mem) if alloc_mem else 0
        lines.append(
            f"{r['name']:<40} {cpu_m}m{'':<10} {cpu_pct}%{'':<8} {mem_b}{'':<10} {mem_pct}%"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core: Pods
# ---------------------------------------------------------------------------


def _pods_query(
    conn,
    *,
    namespace: str | None = None,
    labelSelector: str | None = None,
    fieldSelector: str | None = None,
) -> list[dict]:
    cur = _dict_cur(conn)
    query = """
        SELECT p.*, ns.name AS namespace_name
        FROM pods p
        JOIN namespaces ns ON p.namespace_id = ns.id
        WHERE p.deleted_at IS NULL
    """
    params: list = []

    if namespace:
        query += " AND ns.name = %s"
        params.append(namespace)

    if labelSelector:
        for part in labelSelector.split(","):
            part = part.strip()
            if " in " in part.lower():
                # label in (v1,v2) — simplified
                m = re.match(r"(\S+)\s+in\s*\((.+)\)", part, re.IGNORECASE)
                if m:
                    key = m.group(1)
                    vals = [v.strip() for v in m.group(2).split(",")]
                    query += " AND p.labels->>%s = ANY(%s)"
                    params.extend([key, vals])
            elif "=" in part:
                k, _, v = part.partition("=")
                query += " AND p.labels->>%s = %s"
                params.extend([k.strip(), v.strip()])

    for key, val in _parse_field_selector(fieldSelector):
        col_map = {
            "status.phase": "p.phase",
            "spec.nodeName": "p.node_id",  # approximate
            "metadata.name": "p.name",
            "metadata.namespace": "ns.name",
            "spec.restartPolicy": "p.restart_policy",
            "status.podIP": "p.pod_ip",
        }
        if key in col_map:
            query += f" AND {col_map[key]} = %s"
            params.append(val)

    query += " ORDER BY ns.name, p.name"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def _pod_to_yaml_obj(r: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": r["name"],
            "namespace": r.get("namespace_name", ""),
            "uid": str(r["uid"]),
            "labels": r.get("labels", {}),
            "annotations": r.get("annotations", {}),
            "creationTimestamp": str(r["created_at"]),
        },
        "spec": r.get("spec_json") or {
            "restartPolicy": r.get("restart_policy", "Always"),
            "schedulerName": r.get("scheduler_name", "default-scheduler"),
            "serviceAccountName": r.get("service_account_name", ""),
        },
        "status": r.get("status_json") or {
            "phase": r["phase"],
            "podIP": str(r["pod_ip"]) if r.get("pod_ip") else None,
            "hostIP": str(r["host_ip"]) if r.get("host_ip") else None,
            "qosClass": r.get("qos_class", ""),
            "startTime": str(r["start_time"]) if r.get("start_time") else None,
            "conditions": r.get("conditions", []),
        },
    }


def pods_list(
    conn,
    *,
    labelSelector: str | None = None,
    fieldSelector: str | None = None,
) -> str:
    rows = _pods_query(conn, labelSelector=labelSelector, fieldSelector=fieldSelector)
    return _yaml_out([_pod_to_yaml_obj(r) for r in rows])


def pods_list_in_namespace(
    conn,
    *,
    namespace: str,
    labelSelector: str | None = None,
    fieldSelector: str | None = None,
) -> str:
    rows = _pods_query(
        conn,
        namespace=namespace,
        labelSelector=labelSelector,
        fieldSelector=fieldSelector,
    )
    return _yaml_out([_pod_to_yaml_obj(r) for r in rows])


def pods_get(conn, *, name: str, namespace: str | None = None) -> str:
    cur = _dict_cur(conn)
    query = """
        SELECT p.*, ns.name AS namespace_name
        FROM pods p
        JOIN namespaces ns ON p.namespace_id = ns.id
        WHERE p.name = %s AND p.deleted_at IS NULL
    """
    params: list = [name]
    if namespace:
        query += " AND ns.name = %s"
        params.append(namespace)
    query += " LIMIT 1"
    cur.execute(query, params)
    row = cur.fetchone()
    cur.close()

    if not row:
        return f"Error: pod '{name}' not found"

    pod = _pod_to_yaml_obj(dict(row))

    # Attach containers
    cur2 = _dict_cur(conn)
    cur2.execute("SELECT * FROM containers WHERE pod_id = %s", [row["id"]])
    containers = cur2.fetchall()
    cur2.close()

    if containers:
        pod["spec"]["containers"] = [
            {
                "name": c["name"],
                "image": c["image"],
                "ports": c.get("ports", []),
                "resources": {
                    "requests": {
                        "cpu": f"{c['request_cpu_millicores']}m" if c.get("request_cpu_millicores") else None,
                        "memory": str(c["request_memory_bytes"]) if c.get("request_memory_bytes") else None,
                    },
                    "limits": {
                        "cpu": f"{c['limit_cpu_millicores']}m" if c.get("limit_cpu_millicores") else None,
                        "memory": str(c["limit_memory_bytes"]) if c.get("limit_memory_bytes") else None,
                    },
                },
            }
            for c in containers
        ]
        pod["status"]["containerStatuses"] = [
            {
                "name": c["name"],
                "state": c["state"] or "waiting",
                "reason": c.get("state_reason", ""),
                "ready": c["ready"],
                "restartCount": c["restart_count"],
            }
            for c in containers
        ]

    return _yaml_out(pod)


def pods_delete(conn, *, name: str, namespace: str | None = None) -> str:
    cur = conn.cursor()
    query = """
        UPDATE pods SET deleted_at = NOW()
        WHERE name = %s AND deleted_at IS NULL
    """
    params: list = [name]
    if namespace:
        query += """
            AND namespace_id = (
                SELECT id FROM namespaces WHERE name = %s LIMIT 1
            )
        """
        params.append(namespace)
    cur.execute(query, params)
    conn.commit()
    affected = cur.rowcount
    cur.close()
    if affected:
        return f'pod "{name}" deleted'
    return f"Error: pod '{name}' not found"


def pods_top(
    conn,
    *,
    all_namespaces: bool = True,
    namespace: str | None = None,
    name: str | None = None,
    label_selector: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    query = """
        SELECT DISTINCT ON (p.id, pm.container_name)
            ns.name AS namespace_name,
            p.name AS pod_name,
            pm.container_name,
            pm.cpu_usage_millicores,
            pm.memory_usage_bytes
        FROM pod_metrics pm
        JOIN pods p ON pm.pod_id = p.id
        JOIN namespaces ns ON p.namespace_id = ns.id
        WHERE p.deleted_at IS NULL
    """
    params: list = []

    if not all_namespaces and namespace:
        query += " AND ns.name = %s"
        params.append(namespace)
    if name:
        query += " AND p.name = %s"
        params.append(name)

    query += " ORDER BY p.id, pm.container_name, pm.timestamp DESC"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return "No metrics available"

    lines = [
        f"{'NAMESPACE':<25} {'NAME':<35} {'CONTAINER':<20} {'CPU(cores)':<14} {'MEMORY(bytes)'}"
    ]
    for r in rows:
        lines.append(
            f"{r['namespace_name']:<25} {r['pod_name']:<35} "
            f"{r['container_name']:<20} {r['cpu_usage_millicores'] or 0}m{'':<10} "
            f"{r['memory_usage_bytes'] or 0}"
        )
    return "\n".join(lines)


def pods_exec(
    conn,
    *,
    name: str,
    command: list[str],
    namespace: str | None = None,
    container: str | None = None,
) -> str:
    ns = namespace or "default"

    cur = _dict_cur(conn)

    query = """
        SELECT p.id, p.name
        FROM pods p
        JOIN namespaces ns ON p.namespace_id = ns.id
        WHERE p.name = %s AND p.deleted_at IS NULL AND ns.name = %s
        LIMIT 1
    """
    cur.execute(query, [name, ns])
    pod_row = cur.fetchone()
    if not pod_row:
        return f"Error: pod '{name}' not found in namespace '{ns}'"

    container_query = """
        SELECT id, name, filesystem_json, network_json
        FROM containers WHERE pod_id = %s
    """
    container_params: list = [pod_row["id"]]
    if container:
        container_query += " AND name = %s"
        container_params.append(container)
    container_query += " ORDER BY id LIMIT 1"
    cur.execute(container_query, container_params)
    container_row = cur.fetchone()
    cur.close()

    fs: dict[str, VFile] = {}
    net: VNet | None = None

    if container_row:
        if container_row.get("filesystem_json"):
            fs = {k: vfile_from_dict(v) for k, v in container_row["filesystem_json"].items()}
        if container_row.get("network_json"):
            net = vnet_from_dict(container_row["network_json"])

    shell = VShell(fs=fs, hostname=name, net=net)
    cmd_str = shlex.join(command)
    output = shell.run([cmd_str])

    if output is None or output.strip() == "":
        return (
            f"The executed command in pod {name} in namespace {ns} "
            "has not produced any output"
        )

    return output


def pods_log(
    conn,
    *,
    name: str,
    namespace: str | None = None,
    container: str | None = None,
    tail: int | None = None,
    previous: bool = False,
) -> str:
    cur = _dict_cur(conn)
    query = """
        SELECT pl.line_number, pl.timestamp, pl.message
        FROM pod_logs pl
        JOIN pods p ON pl.pod_id = p.id
        WHERE p.name = %s AND pl.is_previous = %s
    """
    params: list[Any] = [name, previous]

    if namespace:
        query += " AND p.namespace_id = (SELECT id FROM namespaces WHERE name = %s LIMIT 1)"
        params.append(namespace)
    if container:
        query += " AND pl.container_name = %s"
        params.append(container)

    query += " ORDER BY pl.line_number DESC"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    if not rows:
        ns = namespace or "default"
        return f"The pod {name} in namespace {ns} has not logged any message yet"

    limit = tail if tail is not None else 100
    if limit > 0:
        rows = rows[:limit]
    rows.reverse()

    return "\n".join(r["message"] for r in rows)


def pods_run(
    conn,
    *,
    image: str,
    namespace: str | None = None,
    name: str | None = None,
    port: int | None = None,
) -> str:
    import uuid

    ns_name = namespace or "default"
    pod_name = name or f"run-{uuid.uuid4().hex[:8]}"

    cur = _dict_cur(conn)

    cur.execute("SELECT id FROM namespaces WHERE name = %s LIMIT 1", [ns_name])
    ns_row = cur.fetchone()
    if not ns_row:
        cur.execute(
            "INSERT INTO namespaces (cluster_id, name, uid, phase) "
            "VALUES ((SELECT id FROM clusters LIMIT 1), %s, %s, 'Active') RETURNING id",
            [ns_name, str(uuid.uuid4())],
        )
        ns_row = cur.fetchone()
    ns_id = ns_row["id"]

    pod_uid = str(uuid.uuid4())
    spec_json = {"containers": [{"name": "main", "image": image}]}
    if port:
        spec_json["containers"][0]["ports"] = [{"containerPort": port}]

    cur.execute(
        """
        INSERT INTO pods (cluster_id, namespace_id, name, uid, phase, restart_policy, spec_json, created_at, start_time)
        VALUES ((SELECT id FROM clusters LIMIT 1), %s, %s, %s, 'Running', 'Always', %s, NOW(), NOW())
        RETURNING id
        """,
        [ns_id, pod_name, pod_uid, psycopg2.extras.Json(spec_json)],
    )
    pod_row = cur.fetchone()
    pod_id = pod_row["id"]

    cur.execute(
        """
        INSERT INTO containers (pod_id, name, image, ready, restart_count, state)
        VALUES (%s, 'main', %s, TRUE, 0, 'running')
        """,
        [pod_id, image],
    )

    conn.commit()
    cur.close()

    pod_obj = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": ns_name, "uid": pod_uid},
        "spec": spec_json,
    }
    return (
        "# The following resources (YAML) have been created or updated successfully\n"
        + _yaml_out(pod_obj)
    )


# ---------------------------------------------------------------------------
# Core: Resources
# ---------------------------------------------------------------------------


def resources_list(
    conn,
    *,
    apiVersion: str,
    kind: str,
    namespace: str | None = None,
    labelSelector: str | None = None,
    fieldSelector: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    query = """
        SELECT r.*, ar.api_version, ar.kind, ns.name AS namespace_name
        FROM resources r
        JOIN api_resources ar ON r.api_resource_id = ar.id
        LEFT JOIN namespaces ns ON r.namespace_id = ns.id
        WHERE ar.api_version = %s AND ar.kind = %s AND r.deleted_at IS NULL
    """
    params: list = [apiVersion, kind]

    if namespace:
        query += " AND ns.name = %s"
        params.append(namespace)

    if labelSelector:
        # Join resource_labels for filtering
        for part in labelSelector.split(","):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                query += """
                    AND r.id IN (
                        SELECT resource_id FROM resource_labels
                        WHERE label_key = %s AND label_value = %s
                    )
                """
                params.extend([k.strip(), v.strip()])

    query += " ORDER BY r.name"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    resources = []
    for r in rows:
        if r.get("manifest"):
            resources.append(r["manifest"])
        else:
            resources.append(
                {
                    "apiVersion": r["api_version"],
                    "kind": r["kind"],
                    "metadata": {
                        "name": r["name"],
                        "namespace": r.get("namespace_name"),
                        "uid": str(r["uid"]),
                    },
                }
            )
    return _yaml_out(resources)


def resources_get(
    conn,
    *,
    apiVersion: str,
    kind: str,
    name: str,
    namespace: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    query = """
        SELECT r.manifest
        FROM resources r
        JOIN api_resources ar ON r.api_resource_id = ar.id
        LEFT JOIN namespaces ns ON r.namespace_id = ns.id
        WHERE ar.api_version = %s AND ar.kind = %s AND r.name = %s
              AND r.deleted_at IS NULL
    """
    params: list = [apiVersion, kind, name]
    if namespace:
        query += " AND ns.name = %s"
        params.append(namespace)
    query += " LIMIT 1"
    cur.execute(query, params)
    row = cur.fetchone()
    cur.close()

    if not row:
        return f"Error: {kind} '{name}' not found"
    return _yaml_out(row["manifest"])


def resources_create_or_update(conn, *, resource: str) -> str:
    # Parse YAML or JSON input
    try:
        obj = yaml.safe_load(resource)
    except Exception:
        obj = json.loads(resource)

    api_version = obj.get("apiVersion", "v1")
    kind = obj.get("kind", "Unknown")
    metadata = obj.get("metadata", {})
    name = metadata.get("name", "unnamed")
    ns_name = metadata.get("namespace")

    cur = _dict_cur(conn)

    # Get a cluster_id
    cur.execute("SELECT id FROM clusters LIMIT 1")
    cluster_row = cur.fetchone()
    cluster_id = cluster_row["id"] if cluster_row else 1

    # Find or create api_resource
    cur.execute(
        "SELECT id FROM api_resources WHERE api_version = %s AND kind = %s LIMIT 1",
        [api_version, kind],
    )
    ar_row = cur.fetchone()
    if not ar_row:
        cur.execute(
            "INSERT INTO api_resources (cluster_id, api_version, kind) "
            "VALUES (%s, %s, %s) RETURNING id",
            [cluster_id, api_version, kind],
        )
        ar_row = cur.fetchone()

    ns_id = None
    if ns_name:
        cur.execute("SELECT id FROM namespaces WHERE name = %s LIMIT 1", [ns_name])
        ns_row = cur.fetchone()
        ns_id = ns_row["id"] if ns_row else None

    import uuid

    cur.execute(
        """
        INSERT INTO resources (cluster_id, api_resource_id, namespace_id, name, uid, manifest)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (cluster_id, api_resource_id, namespace_id, name)
            WHERE deleted_at IS NULL
        DO UPDATE SET manifest = EXCLUDED.manifest
        RETURNING id
    """,
        [cluster_id, ar_row["id"], ns_id, name, str(uuid.uuid4()), psycopg2.extras.Json(obj)],
    )
    conn.commit()
    cur.close()

    return (
        "# The following resources (YAML) have been created or updated successfully\n"
        + _yaml_out(obj)
    )


def resources_delete(
    conn,
    *,
    apiVersion: str,
    kind: str,
    name: str,
    namespace: str | None = None,
    gracePeriodSeconds: int | None = None,
) -> str:
    cur = conn.cursor()
    query = """
        UPDATE resources SET deleted_at = NOW()
        WHERE name = %s AND deleted_at IS NULL
          AND api_resource_id = (
              SELECT id FROM api_resources
              WHERE api_version = %s AND kind = %s LIMIT 1
          )
    """
    params: list = [name, apiVersion, kind]
    if namespace:
        query += """
            AND namespace_id = (
                SELECT id FROM namespaces WHERE name = %s LIMIT 1
            )
        """
        params.append(namespace)
    cur.execute(query, params)
    conn.commit()
    affected = cur.rowcount
    cur.close()
    if affected:
        return "Resource deleted successfully"
    return f"Error: {kind} '{name}' not found"


def resources_scale(
    conn,
    *,
    apiVersion: str,
    kind: str,
    name: str,
    namespace: str | None = None,
    scale: int | None = None,
) -> str:
    cur = _dict_cur(conn)

    if scale is not None:
        update_q = """
            UPDATE resources SET replicas = %s
            WHERE name = %s AND deleted_at IS NULL
              AND api_resource_id = (
                  SELECT id FROM api_resources
                  WHERE api_version = %s AND kind = %s LIMIT 1
              )
        """
        update_params: list = [scale, name, apiVersion, kind]
        if namespace:
            update_q += " AND namespace_id = (SELECT id FROM namespaces WHERE name = %s LIMIT 1)"
            update_params.append(namespace)
        cur.execute(update_q, update_params)
        conn.commit()

    query = """
        SELECT r.replicas, r.name, ar.api_version, ar.kind
        FROM resources r
        JOIN api_resources ar ON r.api_resource_id = ar.id
        WHERE r.name = %s AND r.deleted_at IS NULL
          AND ar.api_version = %s AND ar.kind = %s
    """
    params: list = [name, apiVersion, kind]
    if namespace:
        query += " AND r.namespace_id = (SELECT id FROM namespaces WHERE name = %s LIMIT 1)"
        params.append(namespace)
    query += " LIMIT 1"
    cur.execute(query, params)
    row = cur.fetchone()
    cur.close()

    if not row:
        return f"Error: {kind} '{name}' not found"

    scale_obj = {
        "apiVersion": "autoscaling/v1",
        "kind": "Scale",
        "metadata": {"name": row["name"]},
        "spec": {"replicas": row["replicas"] or 0},
        "status": {"replicas": row["replicas"] or 0},
    }
    return "# Current resource scale (YAML) is below\n" + _yaml_out(scale_obj)


# ---------------------------------------------------------------------------
# Core: Configuration
# ---------------------------------------------------------------------------


def configuration_contexts_list(conn) -> str:
    cur = _dict_cur(conn)
    cur.execute(
        "SELECT context_name, server_url, is_default "
        "FROM kubeconfig_contexts ORDER BY context_name"
    )
    rows = cur.fetchall()
    cur.close()

    if not rows:
        return "No contexts found in kubeconfig"

    default_ctx = next((r["context_name"] for r in rows if r["is_default"]), "")

    text_lines = [f"Found {len(rows)} context(s). Default: {default_ctx}"]
    for r in rows:
        marker = "[*]" if r["is_default"] else "[ ]"
        text_lines.append(f"{marker} {r['context_name']} -> {r['server_url']}")

    structured = {
        "defaultContext": default_ctx,
        "contexts": [
            {
                "name": r["context_name"],
                "server": r["server_url"],
                "default": r["is_default"],
            }
            for r in rows
        ],
    }
    return "\n".join(text_lines) + "\n\n" + json.dumps(structured, indent=2)


def targets_list(conn) -> str:
    return "No targets configured"


# ---------------------------------------------------------------------------
# obs-mcp: Metrics
# ---------------------------------------------------------------------------


def list_metrics(conn, *, name_regex: str) -> str:
    cur = _dict_cur(conn)
    cur.execute("SELECT name FROM metrics WHERE name ~ %s ORDER BY name", [name_regex])
    rows = cur.fetchall()
    cur.close()
    return json.dumps({"metrics": [r["name"] for r in rows]})


def execute_instant_query(
    conn,
    *,
    query: str,
    time: str | None = None,
) -> str:
    # Simplified: extract metric name, return latest samples
    metric_name = _extract_metric_name(query)
    cur = _dict_cur(conn)
    cur.execute(
        """
        SELECT ms.timestamp, ms.value, mseries.labels
        FROM metric_samples ms
        JOIN metric_series mseries ON ms.series_id = mseries.id
        JOIN metrics m ON mseries.metric_id = m.id
        WHERE m.name = %s
        ORDER BY ms.timestamp DESC
        LIMIT 100
    """,
        [metric_name],
    )
    rows = cur.fetchall()
    cur.close()

    # Deduplicate by series (keep latest per series)
    seen: dict[str, dict] = {}
    for r in rows:
        fp = json.dumps(r["labels"], sort_keys=True)
        if fp not in seen:
            seen[fp] = r

    result = [
        {
            "metric": r["labels"],
            "value": [float(r["timestamp"].timestamp()), str(r["value"])],
        }
        for r in seen.values()
    ]
    return json.dumps({"resultType": "vector", "result": result})


def execute_range_query(
    conn,
    *,
    query: str,
    step: str,
    start: str | None = None,
    end: str | None = None,
    duration: str | None = None,
) -> str:
    metric_name = _extract_metric_name(query)
    cur = _dict_cur(conn)
    cur.execute(
        """
        SELECT ms.timestamp, ms.value, mseries.labels, mseries.id AS series_id
        FROM metric_samples ms
        JOIN metric_series mseries ON ms.series_id = mseries.id
        JOIN metrics m ON mseries.metric_id = m.id
        WHERE m.name = %s
        ORDER BY mseries.id, ms.timestamp
    """,
        [metric_name],
    )
    rows = cur.fetchall()
    cur.close()

    # Group by series
    series_map: dict[int, dict] = {}
    for r in rows:
        sid = r["series_id"]
        if sid not in series_map:
            series_map[sid] = {"metric": r["labels"], "values": []}
        series_map[sid]["values"].append(
            [float(r["timestamp"].timestamp()), str(r["value"])]
        )

    return json.dumps({"resultType": "matrix", "result": list(series_map.values())})


def show_timeseries(
    conn,
    *,
    query: str,
    step: str,
    start: str | None = None,
    end: str | None = None,
    duration: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> str:
    return "{}"


def get_label_names(
    conn,
    *,
    metric: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    if metric:
        cur.execute(
            """
            SELECT DISTINCT ml.label_name
            FROM metric_labels ml
            JOIN metrics m ON ml.metric_id = m.id
            WHERE m.name = %s
            ORDER BY ml.label_name
        """,
            [metric],
        )
    else:
        cur.execute("SELECT DISTINCT label_name FROM metric_labels ORDER BY label_name")
    rows = cur.fetchall()
    cur.close()
    return json.dumps({"labels": [r["label_name"] for r in rows]})


def get_label_values(
    conn,
    *,
    label: str,
    metric: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    if metric:
        cur.execute(
            """
            SELECT DISTINCT mlv.label_value
            FROM metric_label_values mlv
            JOIN metric_labels ml ON mlv.metric_label_id = ml.id
            JOIN metrics m ON ml.metric_id = m.id
            WHERE ml.label_name = %s AND m.name = %s
            ORDER BY mlv.label_value
        """,
            [label, metric],
        )
    else:
        cur.execute(
            """
            SELECT DISTINCT mlv.label_value
            FROM metric_label_values mlv
            JOIN metric_labels ml ON mlv.metric_label_id = ml.id
            WHERE ml.label_name = %s
            ORDER BY mlv.label_value
        """,
            [label],
        )
    rows = cur.fetchall()
    cur.close()
    return json.dumps({"values": [r["label_value"] for r in rows]})


def get_series(
    conn,
    *,
    matches: str,
    start: str | None = None,
    end: str | None = None,
) -> str:
    metric_name = _extract_metric_name(matches)
    cur = _dict_cur(conn)
    cur.execute(
        """
        SELECT mseries.labels
        FROM metric_series mseries
        JOIN metrics m ON mseries.metric_id = m.id
        WHERE m.name = %s
    """,
        [metric_name],
    )
    rows = cur.fetchall()
    cur.close()

    series_list = [r["labels"] for r in rows]
    return json.dumps({"series": series_list, "cardinality": len(series_list)})


# ---------------------------------------------------------------------------
# obs-mcp: Alerts & Silences
# ---------------------------------------------------------------------------


def get_alerts(
    conn,
    *,
    active: bool | None = None,
    silenced: bool | None = None,
    inhibited: bool | None = None,
    unprocessed: bool | None = None,
    filter: str | None = None,
    receiver: str | None = None,
) -> str:
    cur = _dict_cur(conn)
    query = "SELECT * FROM alerts WHERE 1=1"
    params: list = []

    if active:
        query += " AND state = 'active'"
    if silenced:
        query += " AND array_length(silenced_by, 1) > 0"
    if inhibited:
        query += " AND array_length(inhibited_by, 1) > 0"
    if unprocessed:
        query += " AND state = 'unprocessed'"

    query += " ORDER BY starts_at DESC"
    cur.execute(query, params)
    alerts_rows = cur.fetchall()

    # Fetch labels for each alert
    alert_ids = [a["id"] for a in alerts_rows]
    labels_map: dict[int, dict[str, str]] = {aid: {} for aid in alert_ids}
    if alert_ids:
        cur.execute(
            "SELECT alert_id, label_key, label_value FROM alert_labels WHERE alert_id = ANY(%s)",
            [alert_ids],
        )
        for r in cur.fetchall():
            labels_map[r["alert_id"]][r["label_key"]] = r["label_value"]

    cur.close()

    # Apply filter on labels
    if filter:
        for pair in filter.split(","):
            pair = pair.strip()
            if "=" in pair:
                k, _, v = pair.partition("=")
                k, v = k.strip(), v.strip()
                alerts_rows = [
                    a for a in alerts_rows if labels_map.get(a["id"], {}).get(k) == v
                ]

    alerts_out = []
    for a in alerts_rows:
        alert_obj: dict[str, Any] = {
            "labels": labels_map.get(a["id"], {}),
            "annotations": a.get("annotations", {}),
            "startsAt": str(a["starts_at"]),
            "status": {
                "state": a["state"],
                "silencedBy": a.get("silenced_by", []),
                "inhibitedBy": a.get("inhibited_by", []),
            },
        }
        if a.get("ends_at"):
            alert_obj["endsAt"] = str(a["ends_at"])
        alerts_out.append(alert_obj)

    return json.dumps({"alerts": alerts_out})


def get_silences(conn, *, filter: str | None = None) -> str:
    cur = _dict_cur(conn)
    cur.execute("SELECT * FROM silences ORDER BY starts_at DESC")
    silence_rows = cur.fetchall()

    silence_ids = [s["id"] for s in silence_rows]
    matchers_map: dict[int, list[dict]] = {sid: [] for sid in silence_ids}
    if silence_ids:
        cur.execute(
            "SELECT * FROM silence_matchers WHERE silence_id = ANY(%s)", [silence_ids]
        )
        for r in cur.fetchall():
            matchers_map[r["silence_id"]].append(
                {
                    "name": r["name"],
                    "value": r["value"],
                    "isRegex": r["is_regex"],
                    "isEqual": r["is_equal"],
                }
            )

    cur.close()

    # Apply filter on matchers
    if filter:
        for pair in filter.split(","):
            pair = pair.strip()
            if "=" in pair:
                k, _, v = pair.partition("=")
                k, v = k.strip(), v.strip()
                silence_rows = [
                    s
                    for s in silence_rows
                    if any(
                        m["name"] == k and m["value"] == v
                        for m in matchers_map.get(s["id"], [])
                    )
                ]

    silences_out = []
    for s in silence_rows:
        silences_out.append(
            {
                "id": s["silence_id"],
                "status": {"state": s["state"]},
                "matchers": matchers_map.get(s["id"], []),
                "startsAt": str(s["starts_at"]),
                "endsAt": str(s["ends_at"]),
                "createdBy": s.get("created_by", ""),
                "comment": s.get("comment", ""),
            }
        )
    return json.dumps({"silences": silences_out})


# ---------------------------------------------------------------------------
# Helpers (internal)
# ---------------------------------------------------------------------------


def _extract_metric_name(query_or_selector: str) -> str:
    """Best-effort metric name extraction from PromQL or series selector."""
    # Try: metric_name{...} or metric_name
    m = re.match(r"([a-zA-Z_:][a-zA-Z0-9_:]*)", query_or_selector.strip())
    return m.group(1) if m else query_or_selector.strip()


# ---------------------------------------------------------------------------
# Tool registry & dispatcher
# ---------------------------------------------------------------------------

TOOLS: dict[str, Callable] = {
    # Core: Events / Namespaces / Nodes
    "events_list": events_list,
    "namespaces_list": namespaces_list,
    "projects_list": projects_list,
    "nodes_log": nodes_log,
    "nodes_stats_summary": nodes_stats_summary,
    "nodes_top": nodes_top,
    # Core: Pods
    "pods_list": pods_list,
    "pods_list_in_namespace": pods_list_in_namespace,
    "pods_get": pods_get,
    "pods_delete": pods_delete,
    "pods_top": pods_top,
    "pods_exec": pods_exec,
    "pods_log": pods_log,
    "pods_run": pods_run,
    # Core: Resources / Config
    "resources_list": resources_list,
    "resources_get": resources_get,
    "resources_create_or_update": resources_create_or_update,
    "resources_delete": resources_delete,
    "resources_scale": resources_scale,
    "configuration_contexts_list": configuration_contexts_list,
    "targets_list": targets_list,
    # obs-mcp: Metrics
    "list_metrics": list_metrics,
    "execute_instant_query": execute_instant_query,
    "execute_range_query": execute_range_query,
    "show_timeseries": show_timeseries,
    "get_label_names": get_label_names,
    "get_label_values": get_label_values,
    "get_series": get_series,
    # obs-mcp: Alerts / Silences
    "get_alerts": get_alerts,
    "get_silences": get_silences,
}


def call_tool(conn, name: str, params: dict | None = None) -> str:
    """Dispatch a tool call by name."""
    if name not in TOOLS:
        return f"Error: unknown tool '{name}'"
    return TOOLS[name](conn, **(params or {}))


# ---------------------------------------------------------------------------
# Tool def loading (Anthropic format)
# ---------------------------------------------------------------------------

STRIP_PARAMS = {"context"}  # not supported by mock tools

EXTRA_TOOL_DEFS = [
    {
        "name": "projects_list",
        "description": "List all OpenShift projects (namespaces with display names) in the cluster",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "targets_list",
        "description": "List all Prometheus scrape targets and their status",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def make_tool_handler(conn):
    """Create a tool_handler callable bound to a psycopg2 connection."""
    def handler(name: str, params: dict) -> str:
        clean = {k: v for k, v in params.items() if k not in STRIP_PARAMS}
        return call_tool(conn, name, clean)
    return handler


def load_tool_defs() -> list[dict]:
    """Load tool defs from raw_tool_defs.json, convert to Anthropic format."""
    raw = json.loads((Path(__file__).parent / "raw_tool_defs.json").read_text())

    tools: list[dict] = []
    for _server, section in raw.items():
        if not isinstance(section, dict) or "tools" not in section:
            continue
        for t in section["tools"]:
            fn = t["function"]
            name = fn["name"]
            if name not in TOOLS:
                continue

            params = fn.get("parameters", {"type": "object", "properties": {}})
            props = {k: v for k, v in params.get("properties", {}).items() if k not in STRIP_PARAMS}
            required = [r for r in params.get("required", []) if r not in STRIP_PARAMS]

            input_schema: dict = {"type": "object", "properties": props}
            if required:
                input_schema["required"] = required

            tools.append({
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": input_schema,
            })

    for extra in EXTRA_TOOL_DEFS:
        if extra["name"] not in {t["name"] for t in tools}:
            tools.append(extra)

    return tools
