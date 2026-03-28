# ADR-004: Sandboxing and Security Model

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki accepts untrusted input from Discord, has shell access via Claude Code, and can spawn pods. The system processes deeply personal data (conversations, preferences, plans). Every layer must be locked down with least-privilege principles.

## Decision

### Core Principle

Internet access is not the primary threat — what the pod can do locally is the threat. Security controls focus on local access (filesystem, Kubernetes API, inter-pod communication) rather than network allowlists.

### Privacy Threat Model

Conversation history, memories, and preferences are the most sensitive data in the system. A compromised pod that can both read this data AND reach the internet is the worst-case scenario. The architecture ensures no single pod has both capabilities:

- **maki-cortex** has internet access (via gluetun) and NATS access (scoped subjects only) but NO direct access to Mem0, Postgres, or Neo4j. It only sees what maki-stem explicitly publishes to the turn request NATS subject.
- **maki-stem** has access to all data stores but NO internet access.

This separation means a compromised cortex can only leak the current turn's context (a few messages and relevant memories), not the entire history.

### Pod-Level Security (SecurityContext)

All pods apply:

```yaml
securityContext:
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
```

Writable space provided via emptyDir or PVC mounts where needed.

### ServiceAccount Policy

- **maki-gate:** Only pod with a ServiceAccount token. RBAC scoped to create/delete pods in maki-workers namespace only. Nothing else.
- **maki-stem:** ServiceAccount with RBAC to read/write configmaps in maki namespace only. No pod creation rights. Cannot create workers — must go through maki-gate.
- **All other pods:** No ServiceAccount token mounted. No Kubernetes API access whatsoever.

### Internet Access Model

All external traffic is anonymized through gluetun. No curated allowlists — those don't scale and become a maintenance burden.

```
Full internet via gluetun (anonymized, logged):
  - maki-cortex      (reasoning, package install, docs)
  - maki-immune    (CVE databases, upstream version checks)
  - maki-workers   (mission-scoped tasks)

Direct external access (no gluetun):
  - maki-ears      → Discord API (needs stable connection)

No internet access at all:
  - maki-stem
  - maki-gate
  - maki-recall
  - maki-embed
  - maki-vault
  - maki-nerve
  - maki-mirror
```

### Network Policy (Pod-to-Pod)

Deny-all by default. Explicit allow per connection:

```
maki-ears:
  ingress: Discord webhook
  egress:  maki-stem only

maki-stem:
  ingress: maki-ears only
  egress:  maki-cortex, maki-nerve, maki-recall, maki-gate

maki-cortex:
  ingress: none (subscribes to NATS subjects)
  egress:  maki-nerve (NATS, scoped subjects only)
           api.anthropic.com (inference, direct)
           internet via gluetun (everything else)

maki-recall:
  ingress: maki-stem
  egress:  maki-vault, maki-embed, maki-graph only

maki-embed:
  ingress: maki-recall only
  egress:  none

maki-graph:
  ingress: maki-recall only
  egress:  none

maki-vault:
  ingress: maki-recall only
  egress:  none (phase 1), replication to other sites (phase 2)

maki-nerve:
  ingress: maki-stem, maki-cortex, maki-gate, maki-mirror
  egress:  other NATS nodes via Tailnet (phase 2)

maki-gate:
  ingress: maki-stem only
  egress:  Kubernetes API (maki-workers ns), maki-nerve (audit)

maki-immune:
  ingress: none
  egress:  maki-nerve (NATS — findings, health, coordination)
           Kubernetes API (read-only — watch pods, events, resources)
           api.anthropic.com (inference, direct)
           internet via gluetun (CVE databases, upstream version checks)

maki-mirror:
  ingress: Tailnet only (your browser)
  egress:  maki-nerve (NATS WebSocket) only

maki-workers (K8s ephemeral maki-node pods):
  ingress: none
  egress:  maki-stem (POST results back),
           internet via gluetun (mission-scoped)

maki-node (bare metal / VM, outside K8s):
  Connects to maki-nerve via Tailnet only
  Full local machine access (trusted environment)
  See ADR-015
```

### Istio Ambient Layer

Planned for production (phase 2), designed for from the start:

- mTLS between all pods via ztunnel
- `AuthorizationPolicy` enforcing the pod-to-pod communication graph based on ServiceAccount identity
- `REGISTRY_ONLY` outbound traffic policy — anything not in a ServiceEntry is blocked and logged
- ServiceEntry resources created dynamically per mission by maki-gate, deleted on mission cleanup

### Worker Namespace Security

```
namespace: maki-workers

Default state: empty, deny-all NetworkPolicy

Worker pods:
  - readOnlyRootFilesystem: true
  - runAsNonRoot: true
  - No ServiceAccount token
  - No kubectl access
  - No access to NATS, Mem0, or Postgres
  - Can only POST results back to maki-stem
  - Internet via gluetun only
  - ResourceQuota enforced per mission
  - LimitRange enforced per pod

ResourceQuota (per mission, created by maki-gate):
  - Max pods: as approved
  - Max memory: as approved
  - Max CPU: as approved

LimitRange:
  - Per pod max: 1Gi memory, 500m CPU (default)
```

### Attack Scenarios Defended

```
Prompt injection via Discord:
  → Hits maki-ears (sanitization)
  → maki-stem validates before passing to maki-cortex
  → Even if maki-cortex compromised: no direct data store access,
    only current turn's context available, internet is anonymized

Compromised maki-cortex tries to exfiltrate:
  → Can only leak current turn context (not full history)
  → NATS access scoped to cortex subjects — cannot query conversation stream or KV
  → Traffic goes through gluetun (anonymized)
  → No access to Kubernetes API, Mem0, Postgres, or Neo4j

Malicious worker tries lateral movement:
  → NetworkPolicy blocks all pod-to-pod except POST to maki-stem
  → No ServiceAccount, can't talk to K8s API
  → No access to NATS, Postgres, or Mem0
  → ResourceQuota caps resource consumption

Worker tries to spawn more workers:
  → No ServiceAccount, no kubectl access
  → Only maki-gate can create pods
  → ResourceQuota limits enforced by Kubernetes

Compromised maki-stem:
  → Has data access but no internet — can't exfiltrate
  → Can't create pods — must go through maki-gate
  → maki-gate requires valid approval token from Discord
```

### Egress Logging

All internet-bound traffic is logged to NATS stream `maki.egress.log` with full URL, timestamp, response size, and source pod. Available via maki-mirror dashboard and `/activity` Discord command.

## Consequences

- No single compromised pod can both access sensitive data and reach the internet
- No curated allowlists to maintain — gluetun + logging replaces static lists
- Worker namespace is empty and locked down by default, resources created dynamically per approved mission
- Istio ambient adds defense in depth but the system is secure without it (NetworkPolicy as baseline)
- The security model doesn't impede Maki's functionality — cortex can still install packages, read docs, browse via gluetun
