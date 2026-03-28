# ADR-006: Mission System

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki needs to perform tasks that require external access, parallel processing, or long-running work beyond cortex's sandbox. These tasks are delegated to maki-node instances — either ephemeral pods in K8s or persistent processes on registered bare metal/VM nodes. Cortex decides what runs where.

## Decision

### Core Principles

- **Missions are atomic.** They run, complete, and are cleaned up. No mid-flight amendments.
- **Workers are maki-node instances.** Same concept on any substrate — K8s pod or bare metal process (ADR-015).
- **The framework is deterministic.** Mission lifecycle is predictable: propose → approve → execute → collect → cleanup.
- **The application of the framework is Maki's free will.** Within an approved envelope, Maki decides the topology, agent prompts, data flow, and retry strategy.
- **If results are insufficient, cortex proposes a new mission.** Not an amendment to the existing one.
- **Mission knowledge is memory, not code.** Cortex learns how to use missions through experience stored in Mem0, not hardcoded templates (ADR-015).

### Mission Lifecycle

```
1. PROPOSE
   Cortex includes a mission proposal in its turn response
   (published via maki.cortex.turn.response NATS subject)
   Cortex decides the target: K8s (ephemeral) or a specific registered node

2. APPROVE
   Stem extracts the proposal, sends to Discord #maki-missions
   User approves (✅ reaction or /approve command)

3. EXECUTE
   If target is K8s:
     Stem sends approved mission to gatekeeper
     Gatekeeper creates ephemeral maki-node pod(s) in maki-workers
   If target is a registered node:
     Stem publishes task to maki.node.<id>.task
     Node picks it up, spawns Claude Code instance

4. COLLECT
   Workers publish results to NATS
   Stem collects final output when all workers complete
   Stem publishes results to cortex via maki.cortex.mission.results

5. CLEANUP
   K8s: worker pods deleted, ResourceQuota deleted
   Node: workspace cleaned up, resources freed
   NATS mission state archived
```

### Mission Proposal Format

Cortex includes proposals in its turn response payload:

```yaml
goal: "Research Istio ambient mesh CVEs and write remediation report"
target: k8s                    # or "node-01" for a specific registered node
agents:
  - role: researcher
    prompt: "Scrape NVD and GitHub advisories for Istio ambient CVEs"
    feeds: analyzer
  - role: analyzer
    prompt: "Cross-reference findings with cluster version 1.21"
    feeds: writer
  - role: writer
    prompt: "Compile remediation report with prioritized action items"
    output: final
resources:
  max_pods: 3
  max_memory: 2Gi
  max_cpu: 1500m
  ttl: 30m
model: claude-sonnet-4-6
```

Cortex picks the target because cortex understands the task. Stem just reads the proposal and forwards it to Discord for approval. Gatekeeper or the node agent handles execution.

### Worker Communication

Workers communicate via NATS subjects scoped to the mission:

```
mission.<id>.<role>.output    worker publishes findings
mission.<id>.<role>.input     worker subscribes to upstream
mission.<id>.status           all workers publish heartbeats
```

Stem watches `mission.<id>.>` for the full picture.

### Envelope Enforcement

For K8s ephemeral workers, the approved envelope is enforced structurally by Kubernetes:

- **Pod count:** ResourceQuota in maki-workers, scoped to mission labels
- **Memory/CPU:** ResourceQuota + LimitRange
- **Egress:** NetworkPolicy + ServiceEntry (created by gatekeeper, scoped to mission)
- **TTL:** Timer that kills all mission pods after approved duration
- **Model:** Specified in worker pod spec, immutable after creation

For registered nodes, enforcement is lighter — the node runs in a trusted environment on the Tailnet. Resource limits are advisory and tracked via heartbeats.

### Stem's Role During Missions

Stem is simple and deterministic during mission execution:

```
Stem does automatically:
  - Restart a crashed K8s worker (up to 3 retries)
  - Kill workers that exceed TTL
  - Collect results when all workers report complete
  - Wake cortex with results when mission is done
  - Wake cortex with failure details if mission fails

Stem never does:
  - Decide if results are good enough
  - Modify a mission in flight
  - Spawn additional workers
  - Make judgment calls about mission progress
  - Pick targets — that's cortex's job
```

All judgment calls go to cortex.

### Cortex's Involvement Is Episodic

```
Episode 1: Plan the mission (proposal in turn response)
Episode 2: Process final results (delivered via next turn request)
Episode 3: Propose follow-up if needed (new proposal in turn response)
```

Between episodes, cortex is idle. Stem runs the show.

### Mission State (NATS KV)

```
maki.missions.<id>              mission metadata + status
maki.missions.<id>.status       running | complete | failed
maki.missions.<id>.result       final deliverable
maki.missions.<id>.log          what each agent did
maki.missions.<id>.resources    actual usage vs budget
```

### K8s Worker Pod Template

```yaml
# Created by maki-gate for K8s ephemeral missions
apiVersion: v1
kind: Pod
metadata:
  name: mission-<id>-<role>
  namespace: maki-workers
  labels:
    maki.io/mission: "<id>"
    maki.io/role: "<role>"
spec:
  restartPolicy: Never
  serviceAccountName: ""
  automountServiceAccountToken: false
  containers:
  - name: worker
    image: maki-node:latest
    env:
    - name: NATS_URL
      value: "maki-nerve:4222"
    - name: TASK_ID
      value: "<id>"
    - name: MODE
      value: "ephemeral"
    securityContext:
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
    resources:
      limits:
        memory: "<per mission limit>"
        cpu: "<per mission limit>"
  - name: gluetun
    image: gluetun:latest
```

## Consequences

- Missions are simple to reason about — propose, approve, execute, collect, cleanup
- Cortex decides where missions run — K8s ephemeral pods or registered nodes
- Workers and nodes are the same concept (maki-node) on different substrates (ADR-015)
- No complex state management for mid-flight changes
- Cortex doesn't need to know Kubernetes internals — it just includes proposals in its response
- K8s workers are maximally sandboxed — no SA, no cluster access, ephemeral
- Follow-up missions are new missions, keeping the lifecycle clean
- All mission artifacts (proposals, results, logs) preserved in NATS KV for audit
- Mission knowledge evolves through Mem0 — Maki learns what works over time
