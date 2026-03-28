# ADR-009: Infrastructure and High Availability

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki must be resilient — hard to kill, geographically distributed, and self-healing. The available hardware includes an Intel NUC (16GB), a Jonsbo N5 (running existing production workloads), and a planned Hetzner VPS (16GB). All connected via Tailscale mesh.

## Decision

### Hardware Fleet

```
Intel NUC 16GB          Primary brain. Dedicated to Maki.
Jonsbo N5               Production box (Plex, *arr, Kavita).
                        Existing single-node cluster. Minimal touch.
                        Supports Maki with lightweight quorum services.
Hetzner VPS 16GB        Geographic redundancy. Full Maki peer.
```

### Kubernetes Distribution

**Canonical K8s (k8s-snap)** — not k3s, not vanilla k8s.

Reasons:
- Where Canonical is heading strategically
- Suitable resource footprint for NUC and VPS
- Well-supported snap-based lifecycle

### Cluster Topology

**Separate clusters per site, not one stretched cluster.**

Stretched clusters over Tailnet are unreliable — etcd/dqlite is sensitive to network latency, Tailscale WireGuard hops can spike, and temporary tunnel drops cause node evictions. Prior experience confirmed join failures over Tailnet.

```
Site A: Intel NUC (home LAN)
  Canonical K8s single-node cluster
  Full maki namespace

Site B: Hetzner VPS
  Canonical K8s single-node cluster
  Maki peer: PA pod, NATS node, Postgres replica

Site C: Jonsbo N5 (home LAN)
  Existing cluster, untouched for prod workloads
  Adds: NATS quorum voter pod, Patroni witness
  Future: Home Assistant
```

NATS and Postgres handle their own clustering over Tailscale, independent of Kubernetes. No Kubernetes federation needed.

### NATS Clustering (3 nodes)

```
NUC     → NATS node (full, stores data)
Hetzner → NATS node (full, stores data)
Jonsbo  → NATS node (quorum voter, lightweight)
```

Three nodes provide Raft quorum. Lose any one site, the remaining two maintain consensus. NATS clusters over Tailnet — it's designed for higher-latency links and tolerates brief disconnections.

Jonsbo's NATS node is ~50MB RAM. Negligible alongside Plex.

### Postgres Replication

```
NUC     → Postgres primary
Hetzner → Postgres streaming replica (async)
Jonsbo  → Patroni witness (vote only, no data)
```

**Patroni with built-in Raft** (Patroni 2.x) manages failover. No external DCS (etcd/ZooKeeper) needed. Three Patroni nodes give quorum for leader election.

If NUC dies:
1. Patroni detects primary loss
2. Jonsbo witness + Hetzner replica form quorum
3. Hetzner replica promoted to primary
4. Maki instances reconnect automatically

### Network Topology

```
┌─── Intel NUC (primary brain) ────────────┐
│  Canonical K8s                            │
│  Full maki namespace                      │
│  NATS node, Postgres primary, Neo4j       │
│  Mem0, Ollama, all brain pods             │
└───────────────────────────────────────────┘
          │ tailscale
┌─── Jonsbo N5 (prod + support) ───────────┐
│  Existing cluster                         │
│  NATS quorum voter (~50MB)                │
│  Patroni witness (vote only)              │
│  Home Assistant (future)                  │
└───────────────────────────────────────────┘
          │ tailscale
┌─── Hetzner VPS 16GB (geographic HA) ─────┐
│  Canonical K8s                            │
│  maki-cortex (active peer)                  │
│  maki-stem (active peer)                  │
│  maki-ears (active peer)                  │
│  NATS node, Postgres replica              │
│  Mem0 (reads local replica,               │
│        writes to NUC primary)             │
└───────────────────────────────────────────┘
```

### Failure Scenarios

```
NUC dies:
  NATS:     Hetzner + Jonsbo have quorum ✓
  Postgres: Patroni promotes Hetzner replica ✓
  Maki:     Hetzner PA keeps responding ✓
  Result:   You don't notice.

Hetzner dies:
  NATS:     NUC + Jonsbo have quorum ✓
  Postgres: NUC primary still running ✓
  Maki:     NUC PA keeps running ✓
  Result:   You don't notice.

Jonsbo dies:
  NATS:     NUC + Hetzner have quorum ✓
  Postgres: NUC primary + Hetzner replica ✓
            (Patroni has 2/3 quorum still)
  Maki:     Fully operational ✓
  Result:   You don't notice. Plex goes down.

Home power outage (NUC + Jonsbo):
  NATS:     Hetzner alone, no quorum, read-only
  Postgres: Hetzner replica, can promote manually
  Maki:     Hetzner PA responds, degraded memory
            (can read but not persist new memories
             until quorum restores)
  Result:   Degraded but alive.

Two of three sites down simultaneously:
  Remaining node: read-only NATS, can respond
  from cached context but can't persist.
  Result:   Degraded but present.
```

### Scaling Pattern

Adding a new node follows the same pattern forever:
1. Provision machine (physical or VPS)
2. Join Tailnet
3. Bootstrap Canonical K8s
4. Deploy Maki pods
5. NATS joins cluster, Postgres starts replicating
6. Maki instance syncs identity and history
7. Immediately operational

### Build Phases

```
Phase 1: NUC only
  Dev on existing dev machine (microk8s)
  Deploy to NUC when validated
  Single node, full brain, not HA

Phase 2: Hetzner + Jonsbo join simultaneously
  Full HA from day one — no half-measures
  3-node NATS quorum
  Postgres with Patroni + witness
  Geographic redundancy active
  Merge of phases 2+3 from original plan

Phase 3+: Senses and growth
  IoT via Home Assistant on Jonsbo
  Market data, edge devices, etc.
```

### Future: Istio Ambient Mesh

Designed for from the start but deployed in phase 2:
- mTLS between all pods
- AuthorizationPolicy for pod-to-pod access control
- ServiceEntry for egress control
- East-west gateways for cross-cluster communication if needed (likely not needed since NATS and Postgres handle their own clustering)

## Consequences

- Three separate K8s clusters avoids the pain of stretched clusters over Tailnet
- maki-nerve and maki-vault cluster independently — no dependency on Kubernetes federation
- 3-node quorum for both NATS and Postgres — lose any single site gracefully
- Jonsbo's prod workloads are untouched — only lightweight quorum services added
- The NUC is dedicated to Maki — no resource contention
- Geographic redundancy means Maki survives home-level failures
- Adding nodes in the future follows the same pattern
