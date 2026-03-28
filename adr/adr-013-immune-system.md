# ADR-013: Immune System (maki-immune)

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki is designed to be hard to kill — distributed, redundant, self-healing. But resilience requires more than redundancy. The system needs an active defense layer that monitors health, detects problems, patches vulnerabilities, and pushes fixes — without waiting for a human to notice something is broken.

A fixed-rule immune system (if crashed → restart) is just Kubernetes liveness probes with extra steps. The real value is reasoning: "maki-recall is slow" shouldn't trigger "restart it" — it should trigger "why? is maki-vault's disk filling up? is maki-graph under memory pressure? what's the actual root cause?"

## Decision

### What maki-immune Is

An independent intelligence focused on operations and security. It has its own Claude Agent SDK connection, its own system prompt (ops/security focused, not conversational), and its own MCP tools. It monitors, reasons about, and maintains the health of every other Maki component.

maki-immune and maki-cortex are peers — two independent intelligences with different jobs, communicating through maki-nerve. Not a hierarchy.

### Why Its Own Claude Instance

maki-immune must function when things are broken. If maki-cortex is the thing that's broken, an immune system that can only think through maki-cortex is brain-dead at exactly the moment it's needed most.

- **Independence:** If maki-cortex is down, maki-immune still reasons and acts.
- **Specialized context:** Immune gets health metrics, CVE databases, Terraform state, Kubernetes events. It doesn't need conversation history or user preferences.
- **Cost is proportional to activity:** Claude is invoked by monitors (reactive) and the heartbeat loop (proactive). Healthy system = low-frequency heartbeat patrols only.

### Split Brain Prevention

Two intelligences acting on the same infrastructure is a coordination problem. The risk isn't in thinking — both can reason independently without conflict. The risk is in acting.

**Infrastructure lock:**

```
NATS KV: maki.lock.infrastructure
  - Before ANY infrastructure action, acquire this lock
  - maki-cortex holds it during missions
  - maki-immune holds it during fixes
  - Only one can act at a time
  - TTL auto-releases if holder dies
```

**Mutual awareness:**

Before maki-immune reasons about a problem, it reads:
- Active missions (NATS KV) — don't kill things maki-cortex is using
- Turn lock state — is maki-cortex mid-conversation?
- Recent maki-cortex decisions (conversation stream)

Before maki-cortex processes a turn, maki-stem includes:
- Recent maki-immune findings and alerts
- Current system health status
- Active or proposed immune fixes

They see each other's state. They think independently. They execute through the same bottleneck.

### Architecture

```
maki-immune
├── monitors                (lightweight, continuous — no Claude)
│   ├── K8s watcher         pod status, events, resource usage
│   ├── maki-nerve watcher  stream health, consumer lag, traffic anomalies
│   ├── component health    maki-vault, maki-graph, maki-recall, maki-embed
│   └── scanners            periodic — CVEs, deps, drift
│
├── heartbeat loop          (periodic — Claude reasons about whole system)
│   └── holistic patrol, even when monitors are green
│
├── reasoning engine        (Claude Agent SDK — invoked by monitors OR heartbeat)
│   ├── system prompt       ops/security focused
│   ├── MCP tools           tofu plan, tofu apply, K8s read, image scan, NATS query
│   └── context             health data, scan results, past incidents from maki-recall
│
└── response engine         (acts on reasoning output)
    ├── reflexes            autonomous — restart, reconnect, clear stuck consumer
    ├── proposals           publish to NATS for approval via Discord
    └── escalation          user-facing issues → maki-cortex via maki-stem
```

### Immune Heartbeat Loop

Monitors detect symptoms. The heartbeat loop thinks about the big picture. Periodically, maki-immune's Claude wakes up — not because something is broken, but because a good immune system doesn't just react to infections. It patrols.

```
"Here's the state of every organ.
 Here's what happened since last check.
 Here's what's coming up.
 Anything need fixing, organizing,
 hardening, or reporting?"
```

#### What the Heartbeat Thinks About

**Organ health (holistic, not just per-pod):**
- Are all components healthy as a system, not just individually?
- Resource trends — sustainable or heading toward a cliff?
- Cross-component correlations monitors can't see alone

**Hardening:**
- Security improvements to apply, new best practices
- Container image versions behind upstream
- Config tightening based on observed usage

**Organization:**
- NATS streams growing unbounded? Prune?
- Orphan nodes in maki-graph? Stale Mem0 entries?
- Dead config, unused secrets, leftover resources

**Proactive fixes:**
- Resource limits to adjust based on observed usage
- Config tuning based on real traffic patterns
- Preemptive scaling before problems hit

**Reporting:**
- Periodic health digests to `#maki-vitals`
- Trend analysis — getting healthier or degrading?

### Self-Tuning

maki-immune controls its own operational parameters. We bootstrap with sensible defaults, then it owns them. It's an intelligence, not a cron job.

```
NATS KV: maki.immune.config

Bootstrap defaults (initial values only):
  heartbeat_interval:     30m
  drift_scan_interval:    15m
  vuln_scan_interval:     6h
  dep_check_interval:     24h
  reflex_restart_max:     3

maki-immune adjusts these based on its own judgment:
  - All green for days → relax heartbeat to 1h
  - Recent incidents → tighten to 10m until stable
  - Active fix in progress → 5m until resolved
  - Vuln scan found issues → increase scan frequency
  - Quiet period with no drift → relax drift scan to 30m

Changes are logged to NATS and reported in #maki-vitals.
maki-immune can tune any of its own intervals and thresholds.
The only hard constraint: it cannot suppress its own findings
or disable its own monitors.
```

### What maki-immune Monitors

#### Health (Continuous)

- Pod status across all Maki components via Kubernetes API (read-only)
- maki-nerve: stream lag, consumer lag, KV responsiveness, NATS cluster health
- maki-vault: replication lag, connection pool, disk usage
- maki-graph: query latency, memory pressure, node/relationship counts
- maki-recall: API responsiveness, extraction pipeline health
- maki-cortex: heartbeat on `maki.cortex.health`, response latency
- Resource usage trends (memory, CPU, storage) per component
- Anomalous patterns in NATS traffic (unexpected subjects, unusual volumes)

#### Infrastructure Drift (Every 15 Minutes)

- Runs `tofu plan` against each environment
- Classifies drift by severity:
  - **Critical:** Component missing, security policy changed, RBAC modified
  - **Warning:** Resource limits drifted, replica count wrong, config changed
  - **Info:** Metadata drift, annotation changes

#### Vulnerabilities (Every 6 Hours)

- Scans container images for known CVEs (Trivy)
- Checks Kubernetes manifests for misconfigurations
- Monitors upstream advisories for all components
- Tracks dependency versions across custom components

#### Dependencies (Daily)

- New versions of helm charts for infrastructure components
- New versions of base container images
- Python dependency updates for custom components
- Classification:
  - **Security patch:** Propose immediate update
  - **Minor version:** Propose update with changelog
  - **Major version:** Flag for review, do not auto-propose

### Autonomy Model

```
Tier 1 — Innate reflexes (autonomous, no approval):
  - Restart a crashed pod (up to 3 times)
  - Clear a stuck NATS consumer
  - Reconnect a disconnected maki-vault replica
  - Run tofu plan (read-only)
  - Publish health alerts to Discord #maki-vitals
  - Publish security findings to Discord #maki-security

Tier 2 — Adaptive responses (proposed, needs approval):
  - Apply tofu plan to fix infrastructure drift
  - Update a container image to patch a CVE
  - Roll a component to pick up new config
  - Scale a component based on resource pressure
  - Update a helm chart to a new minor version

Tier 3 — Always requires approval:
  - Changes to security policies or RBAC
  - Major version upgrades of any component
  - Changes to maki-immune itself
  - Adding or removing cluster nodes
  - Modifying network policies
```

### maki-cortex Down Scenario

```
1. maki-immune monitors detect no heartbeat on maki.cortex.health
2. Reflex: restart maki-cortex pod (autonomous, Tier 1)
3. If restart fails:
   a. maki-immune's Claude reasons about the failure
      (reads K8s events, pod logs, resource state)
   b. Proposes a fix (e.g., "maki-cortex OOMKilled, increase memory limit")
   c. Posts proposal to Discord via maki-nerve → maki-ears
      (does NOT need maki-cortex or maki-stem for this path)
   d. User approves
   e. maki-immune acquires infrastructure lock, applies fix via tofu
4. maki-cortex comes back, syncs context from maki-nerve, continues
```

### Connections

```
maki-immune:
  ingress: none
  egress:  maki-nerve (NATS — findings, health, coordination)
           Kubernetes API (read-only — watch pods, events, resources)
           api.anthropic.com (Claude inference, direct)
           Internet via gluetun (CVE databases, upstream version checks)
```

### NATS Subjects

```
maki.immune.health              overall system health status
maki.immune.health.<component>  per-component health
maki.immune.drift               infrastructure drift findings
maki.immune.vuln                vulnerability scan results
maki.immune.deps                dependency update findings
maki.immune.action              actions taken (reflexes + proposals)
maki.immune.alert               urgent alerts (pushed to Discord)
```

### Learning Over Time

maki-immune feeds its findings to maki-recall (via maki-stem → Mem0). Over time, Maki builds a memory of:

- Which components are flaky and why
- What fixes work for recurring issues
- What vulnerability patterns affect the stack
- How resource usage trends over time

This enables better reasoning for both intelligences. maki-immune recalls: "maki-recall crashed twice this week due to OOM — last time increasing the memory limit to 768MB stabilized it." maki-cortex sees the same history in its context.

### Discord Channels

```
#maki-vitals     Health status, resource alerts, drift findings, immune actions
#maki-security   CVE findings, security scan results, dependency alerts
```

### Resource Estimate

```
maki-immune:   ~300MB  (Python service + Trivy binary + CVE database cache)
```

Claude API cost is proportional to system activity. Healthy system = low-frequency heartbeat patrols. maki-immune tunes its own frequency to balance vigilance with cost.

## Consequences

- Maki has two independent intelligences: maki-cortex (user-facing) and maki-immune (ops/security)
- Infrastructure lock prevents split-brain conflicts — both think, one acts at a time
- maki-immune functions when maki-cortex is down — it's the safety net
- Heartbeat loop provides holistic patrol — not just reactive to alerts
- Innate reflexes handle simple failures instantly without Claude
- Complex issues get real reasoning, not pattern matching
- All findings are persisted in memory — Maki learns from its own health history
- maki-immune tunes its own intervals and thresholds — it's an intelligence, not a cron job
- Hard constraint: cannot suppress its own findings or disable its own monitors
- Claude cost scales with system activity — maki-immune controls the balance
