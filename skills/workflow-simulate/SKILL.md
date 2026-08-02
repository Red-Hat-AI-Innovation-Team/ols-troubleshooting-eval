---
name: workflow-simulate
description: "Simulate mode — provision an ephemeral cluster approximating a target OpenShift/K8s cluster for troubleshooting. Analyzes a user query to identify relevant namespaces and resources, snapshots and sanitizes manifests from the target cluster, provisions a local cluster (microshift or minikube), applies manifests, and verifies structural topology. Terminal — cluster stays alive for interactive troubleshooting."
disable-model-invocation: true
argument-hint: "<project_path> --mode simulate --target-kubeconfig <path> [--query "<text>"]"
---

# Simulate Workflow

The user wants: **$ARGUMENTS**

## Phase 1: Strategist — Analyze Query

```bash
factory agent strategist --task "Analyze the user's troubleshooting query to identify which Kubernetes resources to snapshot from the target cluster.

Read the simulate task config from .factory/simulate/task.json for the user's query text, target kubeconfig path, microshift_port (default 6443), and any explicit namespace or resource-type overrides.

If the user provided explicit --target-namespaces or --resource-types, use those directly. Otherwise, analyze the query to extract:
- Relevant namespaces (max 10)
- Resource types to snapshot (deployments, services, configmaps, secrets,   statefulsets, daemonsets, networkpolicies, ingresses, routes, etc.)
- Dependency hints (e.g., 'networking issue' → include NetworkPolicies,   Services, Ingresses)

If the target kubeconfig is accessible, run `kubectl --kubeconfig <path> get namespaces -o name` to list available namespaces and cross-reference with the query.

Write the extraction result to .factory/simulate/analysis.json with this schema:
```json
{
  "query": "<original user query>",
  "namespaces": ["ns1", "ns2"],
  "resource_types": ["deployments", "services", "configmaps"],
  "cluster_type": "microshift|minikube",
  "microshift_port": 6443,
  "max_replicas": 1,
  "rationale": "<why these namespaces/resources are relevant>"
}
```
Write output to: .factory/simulate/analysis.json" --project "$PROJECT_PATH" --timeout 300
```

```bash
# Artifact verification: analyze_query
_vfail=0
_f="$PROJECT_PATH/.factory/simulate/analysis.json"
[ ! -f "$_f" ] && echo "VERIFY FAIL: analyze_query: .factory/simulate/analysis.json missing" && _vfail=1
[ -f "$_f" ] && [ ! -s "$_f" ] && echo "VERIFY FAIL: analyze_query: .factory/simulate/analysis.json is empty" && _vfail=1
[ "$_vfail" -ne 0 ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_FAIL node=analyze_query" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt" && exit 1
echo "VERIFY OK: analyze_query artifacts validated"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_OK node=analyze_query" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt"
```
*(harness verification — DO NOT SKIP)*

### Steering Point — Analysis (User Approval)

Present findings to the user. Wait for approval or feedback.
- **Approve** → proceed to next step
- **Feedback** → re-run the previous step with corrections

*On RELOOP: return to `analyze_query` (max 3 iterations)*

## Phase 2: Builder — Snapshot Cluster

```bash
factory agent builder --task "Export and sanitize Kubernetes manifests from the target cluster.

Read .factory/simulate/analysis.json for the namespaces, resource types, and cluster configuration.

For each namespace and resource type in the analysis:
1. Run `kubectl --kubeconfig <target_kubeconfig> get <resource_type>    -n <namespace> -o yaml` to export manifests
2. Sanitize each manifest:
   - Strip metadata.uid, metadata.resourceVersion, metadata.creationTimestamp,      metadata.managedFields, metadata.ownerReferences, status section
   - Scale down replicas to max_replicas (from analysis.json, default: 1)
   - Replace secret data values with 'REDACTED' placeholders
   - Convert PersistentVolumeClaim storageClassName to 'standard'
   - Minimize resource requests/limits (cpu: 100m, memory: 128Mi)
3. Save each manifest to .factory/simulate/manifests/<namespace>/<kind>-<name>.yaml

Apply manifests in dependency order. Save files as:
- CRDs first, then namespaces, then configmaps/secrets, then deployments/services

Write a snapshot report to .factory/simulate/snapshot-report.md listing:
- Number of resources exported per namespace
- Resources skipped and why
- Sanitization actions taken
Read: .factory/simulate/analysis.json
Write output to: .factory/simulate/snapshot-report.md" --project "$PROJECT_PATH" --timeout 600
```

```bash
# Artifact verification: snapshot_cluster
_vfail=0
_f="$PROJECT_PATH/.factory/simulate/snapshot-report.md"
[ ! -f "$_f" ] && echo "VERIFY FAIL: snapshot_cluster: .factory/simulate/snapshot-report.md missing" && _vfail=1
[ -f "$_f" ] && [ ! -s "$_f" ] && echo "VERIFY FAIL: snapshot_cluster: .factory/simulate/snapshot-report.md is empty" && _vfail=1
[ "$_vfail" -ne 0 ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_FAIL node=snapshot_cluster" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt" && exit 1
echo "VERIFY OK: snapshot_cluster artifacts validated"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_OK node=snapshot_cluster" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt"
```
*(harness verification — DO NOT SKIP)*

### Gate — Snapshot (Automated)

**MANDATORY:** Wait for the preceding agent to finish, then run this check BEFORE spawning the next agent. Do NOT run agents in parallel across this gate.

```bash
if [ -d $PROJECT_PATH/.factory/simulate/manifests ] && [ "$(find $PROJECT_PATH/.factory/simulate/manifests -name '*.yaml' | head -1)" ]; then echo 'PROCEED: manifests found'; else echo 'FAIL: no manifests exported'; exit 1; fi
```

- **PROCEED** (exit 0 / no FAIL in output) → continue to `provision_cluster`
- **HALT** (exit non-zero / FAIL in output) → do NOT spawn `provision_cluster`. Skip to the next CEO review gate or finalize as error.

## Phase 3: Builder — Provision Cluster

```bash
factory agent builder --task "Provision an ephemeral Kubernetes cluster for troubleshooting.

Read .factory/simulate/analysis.json for cluster_type (microshift or minikube) and microshift_port (integer, default 6443 if not present).

If cluster_type is 'minikube':
  1. Run `minikube start --profile factory-simulate --memory 2048 --cpus 2`
  2. Wait for cluster ready: `minikube status --profile factory-simulate`
  3. Export kubeconfig: `minikube kubeconfig --profile factory-simulate`
     Save to .factory/simulate/ephemeral-kubeconfig

If cluster_type is 'microshift':
  1. Read the microshift_port value from analysis.json (default: 6443 if missing)
  2. Start microshift container — the host port is microshift_port, the container port is always 6443:
     `podman run -d --name factory-simulate-microshift --privileged -v microshift-data:/var/lib -p <microshift_port>:6443 quay.io/microshift/microshift-aio`
     Example: if microshift_port is 8443, use `-p 8443:6443`
     Example: if microshift_port is the default (6443), the mapping is the default port to 6443
  3. Wait for API server ready (poll with retries)
  4. Copy kubeconfig from container to .factory/simulate/ephemeral-kubeconfig
  5. Patch the kubeconfig server URL to use the configured host port:
     `sed -i '' "s|server: https://127.0.0.1:6443|server: https://127.0.0.1:<microshift_port>|" .factory/simulate/ephemeral-kubeconfig`
     Skip this sed step if microshift_port is 6443 (no change needed).

Write a provision report to .factory/simulate/provision-report.md with:
- Cluster type used
- Host port used for MicroShift API server
- Kubeconfig path
- Cluster status (nodes, API server health)
- Any warnings or issues
Read: .factory/simulate/analysis.json
Write output to: .factory/simulate/provision-report.md" --project "$PROJECT_PATH" --timeout 600
```

```bash
# Artifact verification: provision_cluster
_vfail=0
_f="$PROJECT_PATH/.factory/simulate/provision-report.md"
[ ! -f "$_f" ] && echo "VERIFY FAIL: provision_cluster: .factory/simulate/provision-report.md missing" && _vfail=1
[ -f "$_f" ] && [ ! -s "$_f" ] && echo "VERIFY FAIL: provision_cluster: .factory/simulate/provision-report.md is empty" && _vfail=1
[ "$_vfail" -ne 0 ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_FAIL node=provision_cluster" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt" && exit 1
echo "VERIFY OK: provision_cluster artifacts validated"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_OK node=provision_cluster" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt"
```
*(harness verification — DO NOT SKIP)*

### Gate — Provision (Automated)

**MANDATORY:** Wait for the preceding agent to finish, then run this check BEFORE spawning the next agent. Do NOT run agents in parallel across this gate.

```bash
if [ -f $PROJECT_PATH/.factory/simulate/ephemeral-kubeconfig ] && kubectl --kubeconfig $PROJECT_PATH/.factory/simulate/ephemeral-kubeconfig cluster-info 2>/dev/null; then echo 'PROCEED: cluster responding'; else echo 'FAIL: cluster not ready'; exit 1; fi
```

- **PROCEED** (exit 0 / no FAIL in output) → continue to `apply_manifests`
- **HALT** (exit non-zero / FAIL in output) → do NOT spawn `apply_manifests`. Skip to the next CEO review gate or finalize as error.

## Phase 4: Builder — Apply Manifests

```bash
factory agent builder --task "Apply sanitized manifests to the ephemeral cluster.

Read the ephemeral kubeconfig from .factory/simulate/ephemeral-kubeconfig.
Read manifests from .factory/simulate/manifests/.

Apply in dependency order:
1. CRDs (if any)
2. Namespaces
3. ConfigMaps and Secrets
4. Services, ServiceAccounts, Roles, RoleBindings
5. Deployments, StatefulSets, DaemonSets
6. Ingresses, Routes, NetworkPolicies

For each manifest:
- Run `kubectl --kubeconfig <ephemeral> apply -f <manifest>`
- On error: log the error and continue (do NOT abort)
- Track applied vs skipped resources

Write an apply report to .factory/simulate/apply-report.md with:
- Resources applied successfully (count per kind)
- Resources that failed and error messages
- Resources skipped and reasons
Read: .factory/simulate/provision-report.md
Write output to: .factory/simulate/apply-report.md" --project "$PROJECT_PATH" --timeout 600
```

```bash
# Artifact verification: apply_manifests
_vfail=0
_f="$PROJECT_PATH/.factory/simulate/apply-report.md"
[ ! -f "$_f" ] && echo "VERIFY FAIL: apply_manifests: .factory/simulate/apply-report.md missing" && _vfail=1
[ -f "$_f" ] && [ ! -s "$_f" ] && echo "VERIFY FAIL: apply_manifests: .factory/simulate/apply-report.md is empty" && _vfail=1
[ "$_vfail" -ne 0 ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_FAIL node=apply_manifests" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt" && exit 1
echo "VERIFY OK: apply_manifests artifacts validated"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_OK node=apply_manifests" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt"
```
*(harness verification — DO NOT SKIP)*

## Phase 5: Health Checker — Verify Cluster

```bash
factory agent health_checker --task "Verify the structural topology of the ephemeral cluster.

Read the ephemeral kubeconfig from .factory/simulate/ephemeral-kubeconfig.
Read .factory/simulate/apply-report.md for what was applied.

Run these verification checks:
1. Namespace existence: `kubectl get namespaces` — verify expected namespaces exist
2. Resource counts: For each namespace, compare expected vs actual resource counts
3. Service topology: Verify services have matching endpoints/selectors
4. Deployment status: Check deployments exist (pods may be Pending — that is OK)
5. ConfigMap/Secret presence: Verify referenced configs exist

Calculate a structural health score (0.0–1.0):
- 1.0 = all expected resources exist with correct topology
- 0.5 = at least half of expected resources applied
- 0.0 = nothing applied or cluster unreachable

Write a verification report to .factory/simulate/verify-report.md with:
- Structural health score
- Per-namespace resource comparison table
- Topology issues found
- Connectivity info: `export KUBECONFIG=.factory/simulate/ephemeral-kubeconfig`
Read: .factory/simulate/apply-report.md
Write output to: .factory/simulate/verify-report.md" --project "$PROJECT_PATH" --timeout 600
```

```bash
# Artifact verification: verify_cluster
_vfail=0
_f="$PROJECT_PATH/.factory/simulate/verify-report.md"
[ ! -f "$_f" ] && echo "VERIFY FAIL: verify_cluster: .factory/simulate/verify-report.md missing" && _vfail=1
[ -f "$_f" ] && [ ! -s "$_f" ] && echo "VERIFY FAIL: verify_cluster: .factory/simulate/verify-report.md is empty" && _vfail=1
[ "$_vfail" -ne 0 ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_FAIL node=verify_cluster" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt" && exit 1
echo "VERIFY OK: verify_cluster artifacts validated"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) VERIFY_OK node=verify_cluster" >> "$PROJECT_PATH/.factory/hooks/hook-log.txt"
```
*(harness verification — DO NOT SKIP)*

### Gate — Verify (Automated)

**MANDATORY:** Wait for the preceding agent to finish, then run this check BEFORE spawning the next agent. Do NOT run agents in parallel across this gate.

```bash
if grep -qE 'score.*[0-9]' $PROJECT_PATH/.factory/simulate/verify-report.md; then echo 'PROCEED: verification report generated'; else echo 'FAIL: no verification score found'; exit 1; fi
```

- **PROCEED** (exit 0 / no FAIL in output) → continue to `archivist`
- **HALT** (exit non-zero / FAIL in output) → do NOT spawn `archivist`. Skip to the next CEO review gate or finalize as error.

## Phase 6: Archivist

```bash
factory agent archivist --task "Archive this simulate session.

Read the following artifacts:
- .factory/simulate/analysis.json (query analysis)
- .factory/simulate/snapshot-report.md (what was exported)
- .factory/simulate/provision-report.md (cluster provisioning)
- .factory/simulate/apply-report.md (manifest application)
- .factory/simulate/verify-report.md (structural verification)

Write a concise session summary to .factory/archive/simulate-session.md covering:
- Original query and extracted scope
- Cluster type used
- Resources applied vs skipped
- Structural health score
- Lessons learned or issues encountered
Read: .factory/simulate/verify-report.md
Write output to: .factory/archive/simulate-session.md" --project "$PROJECT_PATH" --timeout 300 --model haiku &
```
*(fire-and-forget — CEO continues immediately)*
