## MCP Tool Schemas (used in evaluation)

Tools available to the OLS agent during evaluation. Two MCP servers provide these tools:
- **openshift-mcp-server** (port 8085) — 21 tools from `core` + `config` toolsets (default)
- **obs-mcp** (port 9100) — 9 metrics tools (only when `MCP_EVALS=1`)

# Core Toolset: Events, Namespaces, Nodes

Source files:
- `pkg/toolsets/core/events.go`
- `pkg/toolsets/core/namespaces.go`
- `pkg/toolsets/core/nodes.go`

---

### events_list
**Description**: List Kubernetes events (warnings, errors, state changes) for debugging and troubleshooting in the current cluster from all namespaces

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| namespace | string | no | Optional Namespace to retrieve the events from. If not provided, will list events from all namespaces |
| fieldSelector | string | no | Optional Kubernetes field selector to filter events by field values (e.g. `type=Warning`, `involvedObject.name=my-pod`). Supported fields: involvedObject.kind, involvedObject.name, involvedObject.namespace, involvedObject.uid, involvedObject.apiVersion, involvedObject.resourceVersion, involvedObject.fieldPath, reason, reportingComponent, source, type |

**Output**: YAML-formatted list of event objects. The response is a text string with a markdown header `# The following events (YAML format) were found:` followed by YAML. If no events are found, returns `# No events found`.

Each event in the YAML list is a map with these fields:
- `Namespace` (string) — event namespace
- `Timestamp` (string) — resolved event timestamp (from EventTime, Series.LastObservedTime, LastTimestamp, or FirstTimestamp)
- `Type` (string) — event type (e.g. "Normal", "Warning")
- `Reason` (string) — short reason string
- `InvolvedObject` (map) — with keys `apiVersion`, `Kind`, `Name`
- `Message` (string) — trimmed event message

---

### namespaces_list
**Description**: List all the Kubernetes namespaces in the current cluster

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| fieldSelector | string | no | Optional Kubernetes field selector to filter namespaces by field values (e.g. `metadata.name=default`, `status.phase=Active`). Supported fields: metadata.name, status.phase |

**Output**: Format depends on server `ListOutput` configuration (default: YAML). When YAML, returns the full unstructured Namespace objects serialized as YAML (with managedFields stripped). When table, returns a kubectl-style table with columns derived from the Kubernetes Table API (NAME, STATUS, AGE, etc.). The output is the direct serialized representation of the `v1/Namespace` list from the cluster.

---

### projects_list
**Description**: List all the OpenShift projects in the current cluster

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| *(none)* | — | — | This tool takes no input parameters |

**Output**: Format depends on server `ListOutput` configuration (default: YAML). When YAML, returns the full unstructured Project objects (`project.openshift.io/v1`) serialized as YAML (with managedFields stripped). When table, returns a kubectl-style table. Only registered when the connected cluster is detected as OpenShift.

---

### nodes_log
**Description**: Get logs from a Kubernetes node (kubelet, kube-proxy, or other system logs). This accesses node logs through the Kubernetes API proxy to the kubelet

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Name of the node to get logs from |
| query | string | yes | Specifies services(s) or files from which to return logs. Example: `kubelet` to fetch kubelet logs, `/<log-file-name>` to fetch a specific log file (e.g. `/var/log/kubelet.log` or `/var/log/kube-proxy.log`) |
| tailLines | integer | no | Number of lines to retrieve from the end of the logs (default: 100, 0 means all logs, minimum: 0) |

**Output**: Plain text string containing the raw log lines from the kubelet's log query API (`/api/v1/nodes/<name>/proxy/logs?query=<query>`). If the log is empty, returns: `The node <name> has not logged any message yet or the log file is empty`.

---

### nodes_stats_summary
**Description**: Get detailed resource usage statistics from a Kubernetes node via the kubelet's Summary API. Provides comprehensive metrics including CPU, memory, filesystem, and network usage at the node, pod, and container levels. On systems with cgroup v2 and kernel 4.20+, also includes PSI (Pressure Stall Information) metrics that show resource pressure for CPU, memory, and I/O.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Name of the node to get stats from |

**Output**: Raw JSON string from the kubelet's `/stats/summary` endpoint (`/api/v1/nodes/<name>/proxy/stats/summary`). This is the standard Kubernetes Summary API response containing:
- `node` — node-level stats: CPU, memory, network, filesystem, runtime, rlimit, and optionally PSI metrics
- `pods` — array of per-pod stats, each containing per-container CPU, memory, rootfs, and logs usage

The response is returned as-is (raw JSON bytes converted to string) with no additional formatting.

---

### nodes_top
**Description**: List the resource consumption (CPU and memory) as recorded by the Kubernetes Metrics Server for the specified Kubernetes Nodes or all nodes in the cluster

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | no | Name of the Node to get the resource consumption from (all Nodes if not provided) |
| label_selector | string | no | Kubernetes label selector (e.g. `node-role.kubernetes.io/worker=`) to filter nodes by label (only applicable when name is not provided) |

**Output**: Plain text table in the same format as `kubectl top nodes`. Produced by `metricsutil.NewTopCmdPrinter` with resource availability info. Columns: NAME, CPU(cores), CPU(%), MEMORY(bytes), MEMORY(%). The table includes percentage utilization computed against each node's allocatable resources. Returns an error if the Metrics API (`metrics.k8s.io/v1beta1`) is not available in the cluster.

---

# Core Pod Tools — MCP Tool Schemas

Source: `pkg/toolsets/core/pods.go`

---

### pods_list
**Description**: List all the Kubernetes pods in the current cluster from all namespaces

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| labelSelector | string | No | Optional Kubernetes label selector (e.g. 'app=myapp,env=prod' or 'app in (myapp,yourapp)'), use this option when you want to filter the pods by label |
| fieldSelector | string | No | Optional Kubernetes field selector to filter pods by field values (e.g. 'status.phase=Running', 'spec.nodeName=node1'). Supported fields: metadata.name, metadata.namespace, spec.nodeName, spec.restartPolicy, spec.schedulerName, spec.serviceAccountName, status.phase (Pending/Running/Succeeded/Failed/Unknown), status.podIP, status.nominatedNodeName. Note: CrashLoopBackOff is a container state, not a pod phase, so it cannot be filtered directly. |

**Output**: Formatted pod list rendered via `ListOutput.PrintObj` (table or YAML depending on server config). Contains pod metadata across all namespaces.

---

### pods_list_in_namespace
**Description**: List all the Kubernetes pods in the specified namespace in the current cluster

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| namespace | string | Yes | Namespace to list pods from |
| labelSelector | string | No | Optional Kubernetes label selector (e.g. 'app=myapp,env=prod' or 'app in (myapp,yourapp)'), use this option when you want to filter the pods by label |
| fieldSelector | string | No | Optional Kubernetes field selector to filter pods by field values (e.g. 'status.phase=Running', 'spec.nodeName=node1'). Supported fields: metadata.name, metadata.namespace, spec.nodeName, spec.restartPolicy, spec.schedulerName, spec.serviceAccountName, status.phase (Pending/Running/Succeeded/Failed/Unknown), status.podIP, status.nominatedNodeName. Note: CrashLoopBackOff is a container state, not a pod phase, so it cannot be filtered directly. |

**Output**: Formatted pod list rendered via `ListOutput.PrintObj` (table or YAML depending on server config). Contains pod metadata for the specified namespace.

---

### pods_get
**Description**: Get a Kubernetes Pod in the current or provided namespace with the provided name

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| namespace | string | No | Namespace to get the Pod from |
| name | string | Yes | Name of the Pod |

**Output**: Full Pod resource serialized as YAML (via `output.MarshalYaml`). Contains the complete Pod spec, status, metadata, and all Kubernetes fields.

---

### pods_delete
**Description**: Delete a Kubernetes Pod in the current or provided namespace with the provided name

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| namespace | string | No | Namespace to delete the Pod from |
| name | string | Yes | Name of the Pod to delete |

**Output**: String confirmation from the Kubernetes API indicating the pod was deleted.

---

### pods_top
**Description**: List the resource consumption (CPU and memory) as recorded by the Kubernetes Metrics Server for the specified Kubernetes Pods in the all namespaces, the provided namespace, or the current namespace

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| all_namespaces | boolean | No | If true, list the resource consumption for all Pods in all namespaces. If false, list the resource consumption for Pods in the provided namespace or the current namespace. Default: true |
| namespace | string | No | Namespace to get the Pods resource consumption from (Optional, current namespace if not provided and all_namespaces is false) |
| name | string | No | Name of the Pod to get the resource consumption from (Optional, all Pods in the namespace if not provided) |
| label_selector | string | No | Kubernetes label selector (e.g. 'app=myapp,env=prod' or 'app in (myapp,yourapp)'), use this option when you want to filter the pods by label (Optional, only applicable when name is not provided) |

**Output**: Tabular text output from `metricsutil.TopCmdPrinter.PrintPodMetrics` showing CPU and memory consumption per pod. Includes columns for pod name, CPU (cores), and memory (bytes). Printed with all-namespaces and containers columns enabled.

---

### pods_exec
**Description**: Execute a command in a Kubernetes Pod (shell access, run commands in container) in the current or provided namespace with the provided name and command

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| namespace | string | No | Namespace of the Pod where the command will be executed |
| name | string | Yes | Name of the Pod where the command will be executed |
| command | array of string | Yes | Command to execute in the Pod container. The first item is the command to be run, and the rest are the arguments to that command. Example: ["ls", "-l", "/tmp"] |
| container | string | No | Name of the Pod container where the command will be executed (Optional) |

**Output**: Plain text string. Returns stdout if non-empty; falls back to stderr if stdout is empty; if both are empty returns message: "The executed command in pod {name} in namespace {ns} has not produced any output".

---

### pods_log
**Description**: Get the logs of a Kubernetes Pod in the current or provided namespace with the provided name

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| namespace | string | No | Namespace to get the Pod logs from |
| name | string | Yes | Name of the Pod to get the logs from |
| container | string | No | Name of the Pod container to get the logs from (Optional) |
| tail | integer | No | Number of lines to retrieve from the end of the logs (Optional, default: 100). Minimum: 0 |
| previous | boolean | No | Return previous terminated container logs (Optional) |

**Output**: Plain text log output from the pod container. If the pod has not logged any messages, returns: "The pod {name} in namespace {ns} has not logged any message yet".

---

### pods_run
**Description**: Run a Kubernetes Pod in the current or provided namespace with the provided container image and optional name

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| namespace | string | No | Namespace to run the Pod in |
| name | string | No | Name of the Pod (Optional, random name if not provided) |
| image | string | Yes | Container Image to run in the Pod |
| port | number | No | TCP/IP port to expose from the Pod container (Optional, no port exposed if not provided) |

**Output**: YAML representation of all created/updated resources, prefixed with the header line: "# The following resources (YAML) have been created or updated successfully". Serialized via `output.MarshalYaml`.

---

# Core Resources & Configuration Tool Schemas

Source files:
- `pkg/toolsets/core/resources.go`
- `pkg/toolsets/config/configuration.go`

---

### resources_list
**Description**: List Kubernetes resources and objects in the current cluster by providing their apiVersion and kind and optionally the namespace and label selector. (Common apiVersion/kind examples appended dynamically: v1 Pod, v1 Service, v1 Node, apps/v1 Deployment, networking.k8s.io/v1 Ingress, route.openshift.io/v1 Route on OpenShift)

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| apiVersion | string | yes | apiVersion of the resources (examples: v1, apps/v1, networking.k8s.io/v1) |
| kind | string | yes | kind of the resources (examples: Pod, Service, Deployment, Ingress) |
| namespace | string | no | Namespace to retrieve namespaced resources from (ignored for cluster-scoped). If not provided, lists from all namespaces |
| labelSelector | string | no | Kubernetes label selector (e.g. `app=myapp,env=prod` or `app in (myapp,yourapp)`). Validated against `REGEX_LABELSELECTOR_VALID_CHARS` |
| fieldSelector | string | no | Kubernetes field selector to filter by field values (e.g. `status.phase=Running`, `metadata.name=myresource`). Supported fields vary by resource type. Validated against `REGEX_FIELDSELECTOR` |

**Output**: Text content returned by `params.ListOutput.PrintObj(ret)`. Format depends on the server's `ListOutput` config setting -- either a table representation or YAML list of matching resources. On error, returns an error message string.

---

### resources_get
**Description**: Get a Kubernetes resource in the current cluster by providing its apiVersion, kind, optionally the namespace, and its name. (Same common apiVersion/kind hint as resources_list)

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| apiVersion | string | yes | apiVersion of the resource (examples: v1, apps/v1, networking.k8s.io/v1) |
| kind | string | yes | kind of the resource (examples: Pod, Service, Deployment, Ingress) |
| namespace | string | no | Namespace to retrieve the namespaced resource from (ignored for cluster-scoped). If not provided, uses the configured namespace |
| name | string | yes | Name of the resource |

**Output**: YAML representation of the single resource, produced by `output.MarshalYaml(ret)`. Contains the full Kubernetes object (apiVersion, kind, metadata, spec, status, etc.).

---

### resources_create_or_update
**Description**: Create or update a Kubernetes resource in the current cluster by providing a YAML or JSON representation of the resource. (Same common apiVersion/kind hint as resources_list)

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| resource | string | yes | A JSON or YAML string containing a representation of the Kubernetes resource. Should include top-level fields such as apiVersion, kind, metadata, and spec |

**Output**: On success, returns a string with header `# The following resources (YAML) have been created or updated successfully` followed by the YAML representation of the created/updated resource(s), produced by `output.MarshalYaml(resources)`.

---

### resources_delete
**Description**: Delete a Kubernetes resource in the current cluster by providing its apiVersion, kind, optionally the namespace, and its name. (Same common apiVersion/kind hint as resources_list)

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| apiVersion | string | yes | apiVersion of the resource (examples: v1, apps/v1, networking.k8s.io/v1) |
| kind | string | yes | kind of the resource (examples: Pod, Service, Deployment, Ingress) |
| namespace | string | no | Namespace to delete the namespaced resource from (ignored for cluster-scoped). If not provided, uses the configured namespace |
| name | string | yes | Name of the resource |
| gracePeriodSeconds | integer | no | Duration in seconds before the object should be deleted. Value must be non-negative. Zero means delete immediately. If not provided, uses the default grace period for the resource type |

**Output**: On success, returns the literal string `"Resource deleted successfully"`.

---

### resources_scale
**Description**: Get or update the scale of a Kubernetes resource in the current cluster by providing its apiVersion, kind, name, and optionally the namespace. If the scale is set in the tool call, the scale will be updated to that value. Always returns the current scale of the resource.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| apiVersion | string | yes | apiVersion of the resource (examples: apps/v1) |
| kind | string | yes | kind of the resource (examples: StatefulSet, Deployment) |
| namespace | string | no | Namespace to get/update the namespaced resource scale from (ignored for cluster-scoped). If not provided, uses the configured namespace |
| name | string | yes | Name of the resource |
| scale | integer | no | Scale to update the resource to. If not provided, returns the current scale without updating |

**Output**: Returns a string with header `# Current resource scale (YAML) is below` followed by the YAML representation of the Kubernetes Scale object, produced by `output.MarshalYaml(scale)`. Always reflects the current scale (post-update if `scale` param was provided).

---

### configuration_contexts_list
**Description**: List all available context names and associated server urls from the kubeconfig file

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| *(none)* | -- | -- | No input parameters. Schema is `{"type": "object"}` with no properties. |

**Output**: Returns both text and structured content via `NewToolCallResultFull`:

**Text content**: Formatted listing with header showing total count and default context, followed by each context in `[*] CONTEXT_NAME -> SERVER_URL` format (`*` marks default), plus usage hint about the `context` parameter.

**Structured content** (`ContextsListResult`):
```json
{
  "defaultContext": "<string>",
  "contexts": [
    {
      "name": "<string>",
      "server": "<string>",
      "default": <bool>
    }
  ]
}
```
Contexts are sorted lexicographically by name. If no contexts are found, returns `"No contexts found in kubeconfig"` with no structured content.

---

### targets_list
**Description**: List all available targets

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| *(none)* | -- | -- | No input parameters. Schema is `{"type": "object"}` with no properties. |

**Output**: This is a generic placeholder tool. Its handler is `nil` at definition time. The `WithTargetListTool` mutator renames it to `{targetParameterName}_list` (e.g. `cluster_list`), updates the description/title, and sets the handler with the actual target list. The concrete output depends on the cluster provider that populates it.

---

# obs-mcp Metrics Toolset — Tool Schemas

Source: `github.com/rhobs/obs-mcp` — `pkg/tools/definitions.go`, `pkg/tools/schema.go`, `pkg/tools/handlers.go`

Registered in `pkg/mcp/server.go` under `ToolsetMetrics = "metrics"`.

---

### list_metrics
**Description**: MANDATORY FIRST STEP: List all available metric names in Prometheus. Must be called before any other query tool to discover exact metric names in the environment. The `name_regex` parameter filters results; do not use blanket patterns like `.*`.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| name_regex | string | yes | Regex pattern to filter metric names. Metric names are typically prefixed (e.g., `prometheus_tsdb_head_series`). Use `.*substring.*` to match substrings. Examples: `http_.*`, `.*memory.*`, `node_.*`. Do not pass blanket regex like `.*` or `.+`. |

**Output**: JSON object (`ListMetricsOutput`):
| Field | Type | Description |
|-------|------|-------------|
| metrics | string[] | List of all available metric names matching the regex |

---

### execute_instant_query
**Description**: Execute a PromQL instant query to get current/point-in-time values. Prerequisite: must call `list_metrics` first to verify the metric exists. Use for current state questions, point-in-time snapshots, and latest values.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| query | string | yes | PromQL query string using metric names verified via list_metrics |
| time | string | no | Evaluation time as RFC3339 or Unix timestamp. Omit or use `NOW` for current time. |

**Output**: JSON object (`InstantQueryOutput`):
| Field | Type | Description |
|-------|------|-------------|
| resultType | string | The type of result returned (e.g. `vector`, `scalar`, `string`) |
| result | object[] | Array of instant values. Each element has: `metric` (map of label name to value) and `value` ([timestamp_seconds, value_string] pair) |
| warnings | string[] | Any warnings generated during query execution (omitted if empty) |

---

### execute_range_query
**Description**: Execute a PromQL range query to get time-series data over a period. Prerequisite: must call `list_metrics` first. Use for trends over time, rate calculations, and historical analysis. Time range specified via `start`/`end` pair OR `duration` lookback.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| query | string | yes | PromQL query string using metric names verified via list_metrics |
| step | string | yes | Query resolution step width (e.g., `15s`, `1m`, `1h`). Pattern: `^\d+[smhdwy]$`. Choose based on time range. |
| start | string | no | Start time as RFC3339 or Unix timestamp. Must be provided together with `end`. |
| end | string | no | End time as RFC3339 or Unix timestamp. Use `NOW` for current time. Must be provided together with `start`. |
| duration | string | no | Duration to look back from now (e.g., `1h`, `30m`, `1d`, `2w`). Pattern: `^\d+[smhdwy]$`. Defaults to `1h` if neither start/end nor duration provided. |

**Output**: JSON object (`RangeQueryOutput`):
| Field | Type | Description |
|-------|------|-------------|
| resultType | string | The type of result returned: `matrix`, `vector`, or `scalar` |
| result | object[] | Array of time series (when `FullRangeQueryResponse` is enabled). Each element has: `metric` (label map) and `values` (array of [timestamp_seconds, value_string] pairs). Omitted when summarized. |
| summary | object[] | Summary statistics per series (default mode). Each element has: `series` (label map), `max`, `min`, `avg` (float64), `count` (int), `firstTimestamp`, `lastTimestamp`, `firstValue`, `lastValue`, `delta` (float64), `hasNaN`, `hasInf` (bool), `nonFiniteCount` (int). Omitted when full response. |
| warnings | string[] | Any warnings generated during query execution (omitted if empty) |

---

### show_timeseries
**Description**: Display the results as an interactive timeseries chart. Works like `execute_range_query` but renders results as a visual chart in UI clients. Use as the last tool call after other Prometheus queries are finalized. Query is validated server-side; only an empty result is returned to avoid overwhelming the LLM context.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| query | string | yes | PromQL query string using metric names verified via list_metrics |
| step | string | yes | Query resolution step width (e.g., `15s`, `1m`, `1h`). Pattern: `^\d+[smhdwy]$`. |
| start | string | no | Start time as RFC3339 or Unix timestamp |
| end | string | no | End time as RFC3339 or Unix timestamp. Use `NOW` for current time. |
| duration | string | no | Duration to look back from now (e.g., `1h`, `30m`, `1d`, `2w`). Pattern: `^\d+[smhdwy]$`. |
| title | string | no | Human-readable chart title (e.g., `API Error Rate Over Last Hour`). Displayed above the chart. |
| description | string | no | Explanation of chart meaning or context (e.g., `Shows the rate of HTTP 5xx errors per second, broken down by pod`). Displayed below the title. |

**Output**: Empty struct `{}`. The tool validates the query and returns nothing to the LLM. The visualization is rendered by UI clients using the tool's input parameters. Has additional MCP metadata: `olsUi.id = "mcp-obs/show-timeseries"`.

---

### get_label_names
**Description**: Get all label names (dimensions) available for filtering a metric. Use after calling `list_metrics` to discover how to filter metrics (by namespace, pod, service, etc.) before constructing label matchers in PromQL queries.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| metric | string | no | Metric name (from list_metrics) to get label names for. Leave empty for all metrics. |
| start | string | no | Start time for label discovery as RFC3339 or Unix timestamp (defaults to 1 hour ago) |
| end | string | no | End time for label discovery as RFC3339 or Unix timestamp (defaults to now) |

**Output**: JSON object (`LabelNamesOutput`):
| Field | Type | Description |
|-------|------|-------------|
| labels | string[] | List of label names available for the specified metric or all metrics |

---

### get_label_values
**Description**: Get all unique values for a specific label. Use after calling `list_metrics` and `get_label_names` to find exact label values for filtering (namespace names, pod names, etc.) and to see what values exist before constructing queries.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| label | string | yes | Label name (from get_label_names) to get values for |
| metric | string | no | Metric name (from list_metrics) to scope the label values to. Leave empty for all metrics. |
| start | string | no | Start time for label value discovery as RFC3339 or Unix timestamp (defaults to 1 hour ago) |
| end | string | no | End time for label value discovery as RFC3339 or Unix timestamp (defaults to now) |

**Output**: JSON object (`LabelValuesOutput`):
| Field | Type | Description |
|-------|------|-------------|
| values | string[] | List of unique values for the specified label |

---

### get_series
**Description**: Get time series matching selectors and preview cardinality. Use optionally after calling `list_metrics` to verify label filters match expected series before querying, and to check cardinality (<100 safe, 100-1000 usually fine, >1000 add more filters).

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| matches | string | yes | PromQL series selector using metric names from list_metrics |
| start | string | no | Start time for series discovery as RFC3339 or Unix timestamp (defaults to 1 hour ago) |
| end | string | no | End time for series discovery as RFC3339 or Unix timestamp (defaults to now) |

**Output**: JSON object (`SeriesOutput`):
| Field | Type | Description |
|-------|------|-------------|
| series | object[] | List of time series matching the selector. Each series is a map of label names to string values. |
| cardinality | integer | Total number of series matching the selector |

---

### get_alerts
**Description**: Get alerts from Alertmanager. Start here when investigating issues — if the user asks about things breaking, errors, failures, outages, or services being down. Alert labels contain exact identifiers (pod names, namespaces, job names) needed for targeted queries with Prometheus tools. All filter parameters are optional; without filters, all alerts are returned.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| active | boolean | no | Filter for active alerts only |
| silenced | boolean | no | Filter for silenced alerts only |
| inhibited | boolean | no | Filter for inhibited alerts only |
| unprocessed | boolean | no | Filter for unprocessed alerts only |
| filter | string | no | Label matchers to filter alerts (e.g., `alertname=HighCPU`). Comma-separated for multiple matchers. |
| receiver | string | no | Receiver name to filter alerts |

**Output**: JSON object (`AlertsOutput`):
| Field | Type | Description |
|-------|------|-------------|
| alerts | object[] | List of alerts. Each alert has: `labels` (map[string]string), `annotations` (map[string]string), `startsAt` (string), `endsAt` (string, omitted if not resolved), `status` object with `state` (`active`/`suppressed`/`unprocessed`), `silencedBy` (string[]), `inhibitedBy` (string[]) |

---

### get_silences
**Description**: Get silences from Alertmanager. Use to see which alerts are currently silenced, check active/pending/expired silences, and investigate why certain alerts are not firing notifications.

**Inputs**:
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| filter | string | no | Label matchers to filter silences (e.g., `alertname=HighCPU`). Comma-separated for multiple matchers. |

**Output**: JSON object (`SilencesOutput`):
| Field | Type | Description |
|-------|------|-------------|
| silences | object[] | List of silences. Each silence has: `id` (string), `status` object with `state` (`active`/`pending`/`expired`), `matchers` array (each with `name`, `value`, `isRegex`, `isEqual`), `startsAt` (string), `endsAt` (string), `createdBy` (string), `comment` (string) |
