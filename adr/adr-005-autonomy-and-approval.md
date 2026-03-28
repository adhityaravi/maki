# ADR-005: Autonomy and Approval Model

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki needs to balance autonomy (feeling like a trusted employee) with safety (human oversight for consequential actions). The goal is "autonomy for thinking, approval for acting on infrastructure."

## Decision

### Guiding Principle

Treat Maki like a trusted human employee. A good assistant doesn't ask before taking notes. They do check before deploying to production. And they occasionally mention "hey I noticed you always do X" — that's transparency, not asking permission.

### Three Tiers

#### Tier 1: Full Autonomy (no approval needed)

Actions that are purely internal to Maki's cognition:

- Reading and writing memories (Mem0) — Maki decides what to remember
- Reasoning and planning
- Responding to you on Discord
- Context rotation (summarize, restart, continue)
- Querying NATS, Mem0, Postgres
- Reading sensor data (IoT, market, etc.)
- Planning missions (designing the proposal)
- Monitoring active missions
- Internal coordination between instances
- Learning preferences from interactions
- maki-immune health monitoring and drift detection (`tofu plan`)
- maki-immune innate reflexes (restart crashed pod, clear stuck consumer)

#### Tier 2: Approve Once, Execute Freely (mission approval)

Actions that affect infrastructure outside Maki's brain:

- **Mission proposals** — Maki proposes a plan with an envelope (max pods, resources, egress, TTL, model). You approve the envelope once. Maki orchestrates freely within those constraints.
- You approve via Discord reaction (✅/❌) or `/approve` command
- Once approved, Maki can spawn workers, retry failures, collect results — all within the approved envelope
- Envelope is enforced structurally (ResourceQuota, NetworkPolicy) not by trust

#### Tier 3: Always Requires Explicit Approval

Actions with significant real-world consequences:

- Actual financial trades (when maki-market is active)
- Anything touching infrastructure outside maki and maki-workers namespaces
- Changes to Maki's own RBAC or security policies
- Changes to maki-immune itself (prevents self-modification without oversight)
- Major version upgrades of any component
- Adding or removing cluster nodes
- Security-related home actions (locks, cameras, alarm — when maki-sense is active)
- Access to Jonsbo production workloads

### Soft Notifications

For Tier 1 actions that you might want awareness of, Maki sends informational messages to dedicated Discord channels. No approval needed — just transparency.

Examples:
- `#maki-memory`: "Learned: Adi prefers lightkube over official K8s client"
- `#maki-memory`: "Updated: Adi's Istio version is now 1.22 (was 1.21)"
- `#maki-memory`: "Deprecated: stale memory about preferring vim (no reinforcement in 30 days)"
- `#maki-home`: "Turned on AC, office was hitting 26°C"
- `#maki-market`: "ASML dropped 4% on export restrictions news"

### Approval Flow (Discord UX)

Mission proposals post to `#maki-missions`. Each gets its own thread:

```
🔒 Mission proposal

Goal: Research Istio ambient CVEs

Plan:
  1. Research agent → scrape NVD, GitHub advisories
  2. Analysis agent → cross-reference with cluster version
  3. Writer agent → compile remediation report
  Pipeline: 1 → 2 → 3

Envelope:
  Max pods: 3
  Max memory: 2Gi
  Egress: nvd.nist.gov, github.com/istio, api.anthropic.com
  TTL: 30 minutes
  Model: claude-sonnet-4-6

React ✅ to approve | ❌ to deny
```

### Approval Token Mechanics

- Approval generates a cryptographically signed token
- Token includes: mission description hash + Discord user ID + timestamp + shared secret
- Token expires in 5 minutes
- Single-use: stored in NATS KV, deleted after use
- Only maki-gate can consume tokens and create resources

### IoT Autonomy (Future — maki-sense)

When Home Assistant integration is active:

```
Day 1-7:   Maki observes patterns. Does nothing.
Day 7+:    Maki proposes automations.
           "I notice you turn on the office light at 8:50 every morning.
            Want me to handle that?"
           /approve → becomes an autonomous reflex
           
Approved reflexes: Maki acts without asking.
New patterns: Maki proposes, you approve.
Security actions: Always requires approval.
```

### Trading Autonomy (Future — maki-market)

```
Full autonomy:    Watching, analyzing, forming opinions
Soft notification: Market observations, relevant news
Always approval:   Any actual trade execution
```

### Commands

```
/approve <id>     — approve a mission or automation
/deny <id>        — deny a mission or automation
/modify <id>      — request changes before approving
/memories         — dump recent memory changes
/forget <topic>   — delete a learned preference
/activity         — show recent egress/actions
/status           — Maki vitals and active missions
```

## Consequences

- Maki feels autonomous — it remembers, learns, and responds without friction
- Consequential actions always have human oversight
- Approval is structural (ResourceQuota, NetworkPolicy) not trust-based
- The approval boundary can be adjusted over time as trust builds
- Discord channels provide ambient awareness without being intrusive
