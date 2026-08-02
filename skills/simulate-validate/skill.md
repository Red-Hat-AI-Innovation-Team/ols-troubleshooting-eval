# Simulate-Validate Skill

## Purpose

Validate proposed fixes by executing them on an ephemeral cluster before returning them to the user. This skill implements the Generate-Verify-Revise pattern: diagnose, propose a fix, validate it via simulation, and refine based on structured failure feedback.

## Fix Validation Protocol

When you have diagnosed an issue and formulated a fix, follow this protocol to validate it before returning it to the user.

### Step 1: DIAGNOSE

Use openshift-mcp-server tools to investigate the failing workload:
- Check pod status, events, and logs
- Identify the root cause (missing env var, bad config, resource limits, image pull errors, etc.)
- Formulate a specific fix

### Step 2: PROPOSE

Write your fix as structured JSON to `$PROJECT_PATH/.factory/simulate/proposed-fix.json`:

```json
{
  "commands": ["kubectl set env deploy/my-app KEY=VALUE -n my-namespace"],
  "manifests": [],
  "fault_description": "Missing environment variable KEY causes CrashLoopBackOff",
  "verification_checks": ["pod my-app should be Running", "no CrashLoopBackOff events"]
}
```

Fields:
- `commands`: kubectl commands to apply the fix (use full resource paths and `-n namespace`)
- `manifests`: any manifest YAML to apply (as objects); leave empty if using commands only
- `fault_description`: one-line description of the diagnosed fault
- `verification_checks`: expected outcomes after applying the fix

### Step 3: VALIDATE

Execute the simulation bridge to test your fix on an ephemeral cluster:

```bash
python3 -m simulate_mcp.simulate_bridge validate \
  --fix-proposal /tmp/ols-simulate/proposed-fix.json \
  --kubeconfig $KUBECONFIG \
  --project $PROJECT_PATH
```

The bridge will:
1. Prepare a simulation task from your fix proposal
2. Provision an ephemeral cluster mirroring the target topology
3. Apply baseline manifests to reproduce the broken state
4. Execute your fix commands on the ephemeral cluster
5. Verify the cluster state after the fix
6. Write the verdict to `$PROJECT_PATH/.factory/simulate/verdict.json`

### Step 4: READ VERDICT

Read `$PROJECT_PATH/.factory/simulate/verdict.json` and act on the verdict:

- **FIXED_HIGH_CONFIDENCE**: Your fix was validated successfully. Return it to the user with HIGH confidence. Include the simulation evidence in your response.

- **PARTIAL**: The fix partially resolved the issue. Read `$PROJECT_PATH/.factory/simulate/failure-context.md` to understand what still fails. Refine your fix and go back to Step 2. (Max 3 total attempts.)

- **FAILED**: The fix did not resolve the issue. Read `$PROJECT_PATH/.factory/simulate/failure-context.md` for specific error details (failed verification checks, pod logs, events). Reconsider your diagnosis entirely — the root cause may be different from what you initially identified. Go back to Step 1. (Max 3 total attempts.)

- **REGRESSION**: The fix made things worse. Abandon this approach and reconsider your diagnosis from scratch. Go back to Step 1. (Max 3 total attempts.)

The failure context file includes a formatted summary of what went wrong: failed verification checks, relevant pod logs, and fix execution details.

### Step 5: AFTER MAX RETRIES

If all 3 attempts fail simulation validation:
1. Return your best fix attempt (the one that got closest to FIXED, or the most recent if all FAILED)
2. Mark confidence as LOW
3. Include simulation failure details in your response so the user knows the fix has not been verified
4. Suggest the user test the fix manually before applying to production

## Retry Guidelines

- Maximum 3 total fix attempts per troubleshooting session
- Between attempts, do NOT re-provision the ephemeral cluster — reuse the existing one
- Each retry should incorporate specific feedback from the failure context, not just "try again"
- If the same verification checks fail across 2 attempts, the root cause diagnosis is likely wrong — shift your approach rather than refining the same fix
- Track which commands you have tried so you do not repeat an identical fix

## Response Format

When returning a validated fix, include:

```
## Fix (Confidence: HIGH/LOW)

**Diagnosis:** [one-line root cause]

**Commands:**
1. `kubectl set env deploy/my-app KEY=VALUE -n my-namespace`

**Simulation Result:** FIXED_HIGH_CONFIDENCE
- Topology match: 1.0
- All verification checks passed
- Tested on ephemeral cluster in [X]ms
```

When returning an unvalidated fix (after max retries):

```
## Fix (Confidence: LOW — simulation failed)

**Diagnosis:** [one-line root cause]

**Commands:**
1. `kubectl set env deploy/my-app KEY=VALUE -n my-namespace`

**Simulation Attempts:** 3/3 failed
- Attempt 1: FAILED — [brief reason]
- Attempt 2: PARTIAL — [brief reason]
- Attempt 3: FAILED — [brief reason]

**Warning:** This fix has not been validated via simulation. Please test manually before applying.
```
