-- ============================================================================
-- DATABASE: openshift_cluster
-- ============================================================================

-- --------------------------------------------------------------------------
-- EXTENSIONS
-- --------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- --------------------------------------------------------------------------
-- CLUSTER TOPOLOGY
-- --------------------------------------------------------------------------

CREATE TABLE clusters (
  id              SERIAL PRIMARY KEY,
  name            VARCHAR(253) NOT NULL UNIQUE,
  api_server_url  TEXT NOT NULL,
  is_openshift    BOOLEAN NOT NULL DEFAULT FALSE,
  version         VARCHAR(64),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE kubeconfig_contexts (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  context_name    VARCHAR(253) NOT NULL UNIQUE,
  server_url      TEXT NOT NULL,
  is_default      BOOLEAN NOT NULL DEFAULT FALSE,
  user_name       VARCHAR(253),
  namespace       VARCHAR(253)
);

CREATE INDEX idx_kubeconfig_contexts_cluster ON kubeconfig_contexts(cluster_id);

-- --------------------------------------------------------------------------
-- NODES
-- --------------------------------------------------------------------------

CREATE TABLE nodes (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  name            VARCHAR(253) NOT NULL,
  uid             UUID NOT NULL,
  labels          JSONB NOT NULL DEFAULT '{}',
  annotations     JSONB NOT NULL DEFAULT '{}',
  taints          JSONB NOT NULL DEFAULT '[]',
  -- status
  phase           VARCHAR(32),  -- Ready, NotReady
  -- allocatable resources (for top % calculation)
  allocatable_cpu_millicores    BIGINT,
  allocatable_memory_bytes      BIGINT,
  allocatable_pods              INTEGER,
  -- capacity
  capacity_cpu_millicores       BIGINT,
  capacity_memory_bytes         BIGINT,
  capacity_pods                 INTEGER,
  -- info
  os_image        VARCHAR(256),
  kernel_version  VARCHAR(128),
  kubelet_version VARCHAR(64),
  container_runtime VARCHAR(128),
  architecture    VARCHAR(32),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(cluster_id, name)
);

CREATE INDEX idx_nodes_cluster ON nodes(cluster_id);
CREATE INDEX idx_nodes_labels ON nodes USING GIN(labels);

-- Node-level resource usage snapshots (for nodes_top / nodes_stats_summary)
CREATE TABLE node_metrics (
  id              SERIAL PRIMARY KEY,
  node_id         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  timestamp       TIMESTAMPTZ NOT NULL,
  cpu_usage_millicores    BIGINT NOT NULL,
  memory_usage_bytes      BIGINT NOT NULL,
  -- filesystem
  fs_available_bytes      BIGINT,
  fs_capacity_bytes       BIGINT,
  fs_used_bytes           BIGINT,
  -- network
  network_rx_bytes        BIGINT,
  network_tx_bytes        BIGINT,
  -- PSI (optional, cgroup v2)
  psi_cpu_some_avg10      DOUBLE PRECISION,
  psi_memory_some_avg10   DOUBLE PRECISION,
  psi_io_some_avg10       DOUBLE PRECISION,
  -- full stats summary JSON for nodes_stats_summary (raw kubelet response)
  stats_summary_json      JSONB
);

CREATE INDEX idx_node_metrics_node_ts ON node_metrics(node_id, timestamp DESC);

-- Node logs (for nodes_log)
CREATE TABLE node_logs (
  id              SERIAL PRIMARY KEY,
  node_id         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  source          VARCHAR(253) NOT NULL,  -- 'kubelet', 'kube-proxy', '/var/log/...'
  line_number     INTEGER NOT NULL,
  timestamp       TIMESTAMPTZ,
  message         TEXT NOT NULL
);

CREATE INDEX idx_node_logs_node_source ON node_logs(node_id, source);
CREATE INDEX idx_node_logs_line ON node_logs(node_id, source, line_number);

-- --------------------------------------------------------------------------
-- NAMESPACES / PROJECTS
-- --------------------------------------------------------------------------

CREATE TABLE namespaces (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  name            VARCHAR(253) NOT NULL,
  uid             UUID NOT NULL,
  phase           VARCHAR(32) NOT NULL DEFAULT 'Active',  -- Active, Terminating
  labels          JSONB NOT NULL DEFAULT '{}',
  annotations     JSONB NOT NULL DEFAULT '{}',
  is_openshift_project BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(cluster_id, name)
);

CREATE INDEX idx_namespaces_cluster ON namespaces(cluster_id);
CREATE INDEX idx_namespaces_phase ON namespaces(cluster_id, phase);
CREATE INDEX idx_namespaces_labels ON namespaces USING GIN(labels);

-- --------------------------------------------------------------------------
-- PODS
-- --------------------------------------------------------------------------

CREATE TABLE pods (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  namespace_id    INTEGER NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
  node_id         INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
  name            VARCHAR(253) NOT NULL,
  uid             UUID NOT NULL,
  labels          JSONB NOT NULL DEFAULT '{}',
  annotations     JSONB NOT NULL DEFAULT '{}',
  -- spec fields
  restart_policy  VARCHAR(32) DEFAULT 'Always',
  scheduler_name  VARCHAR(253) DEFAULT 'default-scheduler',
  service_account_name VARCHAR(253),
  -- status fields
  phase           VARCHAR(32) NOT NULL,  -- Pending, Running, Succeeded, Failed, Unknown
  pod_ip          INET,
  host_ip         INET,
  nominated_node_name VARCHAR(253),
  qos_class       VARCHAR(32),  -- Guaranteed, Burstable, BestEffort
  start_time      TIMESTAMPTZ,
  -- conditions stored as JSONB array
  conditions      JSONB NOT NULL DEFAULT '[]',
  -- full spec/status YAML for pods_get
  spec_json       JSONB,
  status_json     JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at      TIMESTAMPTZ,
  UNIQUE(cluster_id, namespace_id, name)
);

CREATE INDEX idx_pods_namespace ON pods(namespace_id);
CREATE INDEX idx_pods_node ON pods(node_id);
CREATE INDEX idx_pods_phase ON pods(phase);
CREATE INDEX idx_pods_labels ON pods USING GIN(labels);
CREATE INDEX idx_pods_cluster ON pods(cluster_id);

-- --------------------------------------------------------------------------
-- CONTAINERS (within pods)
-- --------------------------------------------------------------------------

CREATE TABLE containers (
  id              SERIAL PRIMARY KEY,
  pod_id          INTEGER NOT NULL REFERENCES pods(id) ON DELETE CASCADE,
  name            VARCHAR(253) NOT NULL,
  image           TEXT NOT NULL,
  -- resource requests/limits
  request_cpu_millicores  BIGINT,
  request_memory_bytes    BIGINT,
  limit_cpu_millicores    BIGINT,
  limit_memory_bytes      BIGINT,
  -- ports
  ports           JSONB NOT NULL DEFAULT '[]',
  -- state
  state           VARCHAR(32),  -- waiting, running, terminated
  state_reason    VARCHAR(253),  -- CrashLoopBackOff, OOMKilled, etc.
  state_message   TEXT,
  ready           BOOLEAN NOT NULL DEFAULT FALSE,
  restart_count   INTEGER NOT NULL DEFAULT 0,
  started         BOOLEAN,
  -- virtual shell backing data
  filesystem_json JSONB NOT NULL DEFAULT '{}',
  network_json    JSONB NOT NULL DEFAULT '{}',
  UNIQUE(pod_id, name)
);

CREATE INDEX idx_containers_pod ON containers(pod_id);
CREATE INDEX idx_containers_state ON containers(state);

-- --------------------------------------------------------------------------
-- POD / CONTAINER LOGS
-- --------------------------------------------------------------------------

CREATE TABLE pod_logs (
  id              SERIAL PRIMARY KEY,
  pod_id          INTEGER NOT NULL REFERENCES pods(id) ON DELETE CASCADE,
  container_name  VARCHAR(253),  -- NULL means default/only container
  is_previous     BOOLEAN NOT NULL DEFAULT FALSE,
  line_number     INTEGER NOT NULL,
  timestamp       TIMESTAMPTZ,
  message         TEXT NOT NULL
);

CREATE INDEX idx_pod_logs_pod ON pod_logs(pod_id, container_name, is_previous);
CREATE INDEX idx_pod_logs_line ON pod_logs(pod_id, container_name, is_previous, line_number);

-- --------------------------------------------------------------------------
-- POD METRICS (for pods_top)
-- --------------------------------------------------------------------------

CREATE TABLE pod_metrics (
  id              SERIAL PRIMARY KEY,
  pod_id          INTEGER NOT NULL REFERENCES pods(id) ON DELETE CASCADE,
  container_name  VARCHAR(253) NOT NULL,
  timestamp       TIMESTAMPTZ NOT NULL,
  cpu_usage_millicores    BIGINT NOT NULL,
  memory_usage_bytes      BIGINT NOT NULL
);

CREATE INDEX idx_pod_metrics_pod_ts ON pod_metrics(pod_id, timestamp DESC);

-- --------------------------------------------------------------------------
-- EVENTS
-- --------------------------------------------------------------------------

CREATE TABLE events (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  namespace_id    INTEGER REFERENCES namespaces(id) ON DELETE SET NULL,
  uid             UUID NOT NULL,
  -- timing
  event_time      TIMESTAMPTZ,
  first_timestamp TIMESTAMPTZ,
  last_timestamp  TIMESTAMPTZ,
  series_last_observed TIMESTAMPTZ,
  -- resolved timestamp (best available)
  resolved_timestamp TIMESTAMPTZ NOT NULL,
  -- type/reason
  event_type      VARCHAR(32) NOT NULL,  -- Normal, Warning
  reason          VARCHAR(253) NOT NULL,
  message         TEXT NOT NULL,
  -- involved object
  involved_object_api_version VARCHAR(253),
  involved_object_kind        VARCHAR(253),
  involved_object_name        VARCHAR(253),
  involved_object_namespace   VARCHAR(253),
  involved_object_uid         UUID,
  involved_object_field_path  TEXT,
  involved_object_resource_version VARCHAR(253),
  -- source/reporting
  source_component    VARCHAR(253),
  source_host         VARCHAR(253),
  reporting_component VARCHAR(253),
  -- counts
  count               INTEGER DEFAULT 1,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_cluster ON events(cluster_id);
CREATE INDEX idx_events_namespace ON events(namespace_id);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_events_reason ON events(reason);
CREATE INDEX idx_events_involved_kind ON events(involved_object_kind);
CREATE INDEX idx_events_involved_name ON events(involved_object_name);
CREATE INDEX idx_events_involved_ns ON events(involved_object_namespace);
CREATE INDEX idx_events_timestamp ON events(resolved_timestamp DESC);
CREATE INDEX idx_events_source ON events(source_component);
CREATE INDEX idx_events_reporting ON events(reporting_component);

-- --------------------------------------------------------------------------
-- GENERIC RESOURCES (for resources_list/get/create/delete/scale)
-- --------------------------------------------------------------------------

CREATE TABLE api_resources (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  api_version     VARCHAR(253) NOT NULL,  -- v1, apps/v1, networking.k8s.io/v1
  kind            VARCHAR(253) NOT NULL,  -- Pod, Service, Deployment, etc.
  is_namespaced   BOOLEAN NOT NULL DEFAULT TRUE,
  is_scalable     BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE(cluster_id, api_version, kind)
);

CREATE TABLE resources (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  api_resource_id INTEGER NOT NULL REFERENCES api_resources(id) ON DELETE CASCADE,
  namespace_id    INTEGER REFERENCES namespaces(id) ON DELETE SET NULL,
  name            VARCHAR(253) NOT NULL,
  uid             UUID NOT NULL,
  -- the full resource as YAML/JSON (for resources_get)
  manifest        JSONB NOT NULL,
  -- scale info (for scalable resources)
  replicas        INTEGER,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at      TIMESTAMPTZ
);

CREATE INDEX idx_resources_cluster ON resources(cluster_id);
CREATE INDEX idx_resources_api_resource ON resources(api_resource_id);
CREATE INDEX idx_resources_namespace ON resources(namespace_id);
CREATE INDEX idx_resources_name ON resources(name);
CREATE UNIQUE INDEX idx_resources_unique ON resources(cluster_id, api_resource_id, namespace_id, name) WHERE deleted_at IS NULL;

CREATE TABLE resource_labels (
  id              SERIAL PRIMARY KEY,
  resource_id     INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
  label_key       VARCHAR(253) NOT NULL,
  label_value     VARCHAR(253) NOT NULL
);

CREATE INDEX idx_resource_labels_resource ON resource_labels(resource_id);
CREATE INDEX idx_resource_labels_kv ON resource_labels(label_key, label_value);

-- --------------------------------------------------------------------------
-- OBSERVABILITY: PROMETHEUS METRICS (obs-mcp)
-- --------------------------------------------------------------------------

CREATE TABLE metrics (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  name            VARCHAR(512) NOT NULL,
  help_text       TEXT,
  metric_type     VARCHAR(32),  -- counter, gauge, histogram, summary
  UNIQUE(cluster_id, name)
);

CREATE INDEX idx_metrics_name ON metrics(name);
CREATE INDEX idx_metrics_name_trgm ON metrics USING GIN(name gin_trgm_ops);

-- Label names associated with a metric
CREATE TABLE metric_labels (
  id              SERIAL PRIMARY KEY,
  metric_id       INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
  label_name      VARCHAR(253) NOT NULL,
  UNIQUE(metric_id, label_name)
);

CREATE INDEX idx_metric_labels_metric ON metric_labels(metric_id);
CREATE INDEX idx_metric_labels_name ON metric_labels(label_name);

-- Distinct label values for each metric+label combination
CREATE TABLE metric_label_values (
  id              SERIAL PRIMARY KEY,
  metric_label_id INTEGER NOT NULL REFERENCES metric_labels(id) ON DELETE CASCADE,
  label_value     VARCHAR(1024) NOT NULL,
  UNIQUE(metric_label_id, label_value)
);

CREATE INDEX idx_metric_label_values_mlid ON metric_label_values(metric_label_id);

-- Time series: unique combination of metric + label set
CREATE TABLE metric_series (
  id              SERIAL PRIMARY KEY,
  metric_id       INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
  -- fingerprint of sorted label key=value pairs for dedup
  label_fingerprint VARCHAR(64) NOT NULL,
  labels          JSONB NOT NULL DEFAULT '{}',
  UNIQUE(metric_id, label_fingerprint)
);

CREATE INDEX idx_metric_series_metric ON metric_series(metric_id);
CREATE INDEX idx_metric_series_labels ON metric_series USING GIN(labels);

-- Actual sample data points
CREATE TABLE metric_samples (
  id              BIGSERIAL PRIMARY KEY,
  series_id       INTEGER NOT NULL REFERENCES metric_series(id) ON DELETE CASCADE,
  timestamp       TIMESTAMPTZ NOT NULL,
  value           DOUBLE PRECISION NOT NULL
);

CREATE INDEX idx_metric_samples_series_ts ON metric_samples(series_id, timestamp);

-- Consider using TimescaleDB hypertable for metric_samples in production:
-- SELECT create_hypertable('metric_samples', 'timestamp');

-- --------------------------------------------------------------------------
-- OBSERVABILITY: ALERTMANAGER (obs-mcp)
-- --------------------------------------------------------------------------

CREATE TABLE alerts (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  fingerprint     VARCHAR(64),
  -- state
  state           VARCHAR(32) NOT NULL,  -- active, suppressed, unprocessed
  -- timing
  starts_at       TIMESTAMPTZ NOT NULL,
  ends_at         TIMESTAMPTZ,  -- NULL if not resolved
  -- generator
  generator_url   TEXT,
  -- annotations stored as JSONB
  annotations     JSONB NOT NULL DEFAULT '{}',
  -- silenced/inhibited references
  silenced_by     TEXT[] NOT NULL DEFAULT '{}',
  inhibited_by    TEXT[] NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_alerts_cluster ON alerts(cluster_id);
CREATE INDEX idx_alerts_state ON alerts(state);
CREATE INDEX idx_alerts_starts ON alerts(starts_at);

CREATE TABLE alert_labels (
  id              SERIAL PRIMARY KEY,
  alert_id        INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  label_key       VARCHAR(253) NOT NULL,
  label_value     VARCHAR(1024) NOT NULL
);

CREATE INDEX idx_alert_labels_alert ON alert_labels(alert_id);
CREATE INDEX idx_alert_labels_kv ON alert_labels(label_key, label_value);
CREATE INDEX idx_alert_labels_alertname ON alert_labels(label_value) WHERE label_key = 'alertname';

-- --------------------------------------------------------------------------
-- OBSERVABILITY: ALERTMANAGER SILENCES (obs-mcp)
-- --------------------------------------------------------------------------

CREATE TABLE silences (
  id              SERIAL PRIMARY KEY,
  cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  silence_id      VARCHAR(253) NOT NULL UNIQUE,  -- Alertmanager silence ID
  state           VARCHAR(32) NOT NULL,  -- active, pending, expired
  starts_at       TIMESTAMPTZ NOT NULL,
  ends_at         TIMESTAMPTZ NOT NULL,
  created_by      VARCHAR(253),
  comment         TEXT
);

CREATE INDEX idx_silences_cluster ON silences(cluster_id);
CREATE INDEX idx_silences_state ON silences(state);

CREATE TABLE silence_matchers (
  id              SERIAL PRIMARY KEY,
  silence_id      INTEGER NOT NULL REFERENCES silences(id) ON DELETE CASCADE,
  name            VARCHAR(253) NOT NULL,
  value           VARCHAR(1024) NOT NULL,
  is_regex        BOOLEAN NOT NULL DEFAULT FALSE,
  is_equal        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_silence_matchers_silence ON silence_matchers(silence_id);


