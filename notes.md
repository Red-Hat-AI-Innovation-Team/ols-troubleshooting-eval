
Setting up CRC Cluster

- `make env-up` to start the CRC cluster.
- `make env-down` stop services but keep the CRC VM
- `make env-nuke` full cleanup. On next run might hit timeout issues during pulling of the docker images.

---

2 MCP servers:
- external_libs/openshift-mcp-server: K8s MCP
- external_libs/obs-mcp: Prometheus/Alertmanager MCP

---

## Skill Taxonomy for OpenShift Troubleshooting

From the eval results, I see **six core skills** a model needs, ordered from foundational to advanced:

### Skill 1: Resource Reading & Comprehension
**What**: Given tool output (kubectl describe, get, logs), extract the relevant facts.
**Why it matters**: This is the foundation — every scenario starts here. The "solved" scenarios (envvar_missing, namespace_pod_count, storage_binding) require only this.

**Training tasks to generate**:
- Give the model a `kubectl describe pod` output → ask "what is the restart count?"
- Give `kubectl get events` → ask "which event is an error?"
- Give `kubectl logs` → ask "what exception was thrown?"
- Give `oc get routes` → ask "which route points to service X?"
- Vary the output length, noise level, number of resources

### Skill 2: Resource Relationship Mapping
**What**: Understand how Kubernetes resources reference each other — Service → Pod (via selector), Ingress → Service, NetworkPolicy → Pod (via podSelector), Deployment → ReplicaSet → Pod, PVC → PV → StorageClass.
**Why it matters**: `ingress_rule_mismatch` and `wrong_networkpolicy` require this. The model must trace relationships across resources.

**Training tasks to generate**:
- Give a Service spec and a set of Pods → ask "which pods does this service select?"
- Give an Ingress and a Service → ask "does this ingress correctly route to this service?"
- Give a NetworkPolicy → ask "what traffic is allowed/blocked to pods with label X?"
- Give a Deployment and its ReplicaSet → ask "is the rollout healthy?"
- **Mismatch detection**: Give two resources that *should* match but have a subtle error (wrong label, wrong port, wrong namespace) → ask the model to find it

### Skill 3: Log Pattern Analysis
**What**: Read through logs and identify error patterns, root causes, and correlations.
**Why it matters**: `batch_failure`, `config_drift_analysis` require parsing logs for meaning.

**Training tasks to generate**:
- Give a container log with a mix of INFO/WARN/ERROR → ask "what is the root cause error?"
- Give logs from multiple pods → ask "which pod is the source of the failure?"
- Give before/after config snapshots → ask "what changed?"
- Give a log with a stack trace → ask "what component failed and why?"
- **Noise resistance training**: Embed the critical error in 200 lines of normal logs

### Skill 4: Temporal Reasoning
**What**: Parse timestamps, compute time differences, detect patterns/periodicity/windows.
**Why it matters**: This is the **biggest gap** — `scheduled_outage_detection` (0% all models) and `periodic_failure_window` (mostly 0%) live here.

**Training tasks to generate**:
- Give a list of event timestamps → ask "do these events happen at a regular interval?"
- Give timestamps → ask "what time window do these cluster in?"
- Give events with timestamps → ask "which events happened within 5 minutes of each other?"
- Give a cron schedule expression → ask "when does this run?"
- Give pod restart timestamps → ask "is this pod restarting periodically or randomly?"
- **Explicit arithmetic**: "Event A at 02:15, Event B at 02:45. How many minutes apart?" Build up to "Events at 02:00, 03:00, 04:00 — what's the period?"
- **Mixed temporal tasks**: "These failures happened at [timestamps]. A CronJob runs at '0 2 * * *'. Are they related?"

### Skill 5: Structured Comparison (Diffing)
**What**: Systematically compare two YAML/JSON documents and identify meaningful differences.
**Why it matters**: `config_drift_analysis` requires this. All models struggle (20-40%).

**Training tasks to generate**:
- Give two YAML manifests → ask "what fields differ?"
- Give a "desired state" and "actual state" → ask "what drifted?"
- Give two versions of a ConfigMap → ask "what configuration changed?"
- **Semantic significance**: Give a diff with 5 changes, only 1 matters → ask "which change could cause the observed error?"
- Give a Deployment spec from yesterday and today → ask "why might the pod be crashing now?"

### Skill 6: Tool Selection & Chaining
**What**: Know which tool/command to run and in what order to diagnose a problem.
**Why it matters**: The model needs to plan its diagnostic workflow, not just interpret output.

**Training tasks to generate**:
- Given a symptom ("pod is in CrashLoopBackOff") → ask "what commands would you run to diagnose this?"
- Given a symptom ("service is unreachable") → generate the sequence: check pod status → check service endpoints → check network policies → check ingress
- **Conditional chaining**: "You ran `kubectl get pods` and see pod X is Pending. What do you run next?" (answer: `kubectl describe pod X` to check events)
- **Tool output → next tool**: Give the output of one command, ask what the next diagnostic step is
- **Multi-turn troubleshooting trajectories**: Full conversations where the model decides what to investigate at each turn

---

## Key Insight from the Eval Data

The eval tells you exactly where to invest training budget:

| Skill | Current Gap | Training Priority |
|-------|------------|-------------------|
| Temporal Reasoning | Massive (0% all models) | **Highest** — no model has this |
| Structured Comparison | Large (20-40%) | **High** — scale doesn't fix it |
| Resource Relationships | Model-dependent (20-100%) | **Medium** — small models lack it |
| Log Analysis | Model-dependent (60-100%) | **Medium** |
| Resource Reading | Solved (100%) | **Low** — even 12B handles it |
| Tool Chaining | Implicit in results | **Medium** — hard to measure separately |

A 2-4B model trained heavily on temporal reasoning and structured comparison tasks could potentially match or beat a general-purpose 31B model on this specific eval, because those skills aren't well-represented in general pre-training data.

---

## Synthetic Data Generation Pipeline

Here's how I'd structure the pipeline:

### Phase 1: Build a "world model" catalog

Create JSON/YAML files describing fictional but realistic clusters:
- Namespaces, pods, deployments, services, nodes
- Current states (running, crashed, pending, evicted)
- Events associated with resources
- Metrics available and their approximate values
- Active alerts and silences

This is your ground truth. 50-100 cluster scenarios with varying complexity.

### Phase 2: Question generation

For each scenario, use an LLM (70B+ class — Llama 3.1 70B, Qwen 2.5 72B) to generate questions at each difficulty tier. Prompt it with:
- The scenario description
- The tool schemas
- The tier you want
- Example questions from other scenarios

**Critical**: Have the generator LLM also produce the expected tool call sequence (the "gold" plan) AND the expected final answer. This gives you end-to-end training signal.

### Phase 3: Synthetic execution traces

For each (scenario, question, gold plan), simulate the execution:
1. Generate realistic tool outputs consistent with the scenario
2. Show the model the question, the tool output, and ask it to either (a) extract the needed info and call the next tool, or (b) synthesize the final answer

This creates multi-turn conversation training data:
```
User: Why is checkout-service unhealthy?
Assistant: [thinking] I should first check events for warnings... [tool_call: events_list(namespace="production", fieldSelector="type=Warning")]
Tool result: [realistic YAML output]
Assistant: [thinking] I see ImagePullBackOff events for checkout-service-7f8b9c-x2k4d. Let me check the pod logs for more detail... [tool_call: pods_log(name="checkout-service-7f8b9c-x2k4d", namespace="production", previous=true)]
Tool result: [realistic log output]
Assistant: The checkout-service is unhealthy because...
```

### Phase 4: Hard negative augmentation

Take correct examples and create adversarial variants:
- Same question, wrong tool selected → train model to avoid
- Right tool, wrong parameters (e.g., missing namespace, wrong fieldSelector syntax)
- Correct tool output but wrong information extracted
- Unnecessary tool calls (model calls 5 tools when 2 suffice)


