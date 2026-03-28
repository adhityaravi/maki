# ADR-015: Maki's Body (Edge Nodes and Compute)

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki's brain runs in Kubernetes. But Maki's body extends beyond the cluster — to sensors, actuators, and compute nodes on any machine that joins the Tailnet. Devices self-register, self-report, and disappear cleanly. Maki's body grows and shrinks dynamically just by devices joining and leaving the network.

## Decision

### Brain vs Body

```
Brain (fixed, K8s — maki namespace):
  maki-cortex, maki-stem, maki-synapse,
  maki-recall, maki-graph, maki-embed,
  maki-vault, maki-nerve, maki-ears,
  maki-immune, maki-gate, maki-mirror

Body (dynamic, Tailnet — any device):
  maki-edge:  nerves and muscles (sense + simple commands)
  maki-node:  hands (complex tasks, Claude Code instances)
```

Brain is fixed. Body is fluid.

### maki-nerve on Tailnet

maki-nerve is exposed to the Tailnet via the Tailscale Kubernetes operator. Every edge node and compute node connects to maki-nerve over Tailscale — encrypted, authenticated, zero config. This is the only interface between brain and body.

### maki-edge (Nerves and Muscles)

Lightweight Go binary. Runs on anything — Pi Zero, phone, ESP32, any device with a network connection. Publishes sensor data, receives simple commands, executes them directly. No Claude Code, no workspaces. Just a command executor and sensor publisher.

```
$ ./maki-edge \
    --nats=maki-nerve.tailnet:4222 \
    --creds=/etc/maki/edge.creds \
    --id=office-pi
```

On startup:
1. Detects machine specs (cpu, mem, arch)
2. Discovers local sensors (if any)
3. Publishes registration to `maki.edge.register`
4. Starts sensor data publishing
5. Subscribes to commands
6. Heartbeats every 30s

#### Registration

```json
{
  "id": "office-pi",
  "type": "edge",
  "resources": {"cpu": 4, "memory": "4gb", "arch": "arm64"},
  "sensors": ["temperature", "humidity"],
  "status": "online"
}
```

#### NATS Subjects

```
maki.edge.register                    self-registration on startup
maki.edge.heartbeat.<id>              periodic heartbeat + resource state
maki.edge.<id>.data.<sensor>          sensor data published
maki.edge.<id>.command                commands received from cortex
maki.edge.<id>.result                 command results published back
```

#### Addressing

Cortex addresses edge nodes directly. Not broadcast.

```
Cortex: "office-pi, turn on the AC"
  → maki.edge.office-pi.command
  → {cmd: "turn_on_ac"}

Cortex: "office-pi, what's the temperature?"
  → maki.edge.office-pi.command
  → {cmd: "read_temp"}
```

Cortex knows every edge node because they all registered. It reads `maki.edge.register` stream and `maki.edge.heartbeat.*` to maintain a live map of its body.

#### What maki-edge handles

```
"Turn on AC"        → subprocess or API call, done
"Read temperature"  → read sensor, return value
"Dim lights"        → call Home Assistant API
"Send notification" → local notification
```

Simple. Direct. No reasoning. ~10MB binary.

### maki-node (Hands — Compute)

Heavier binary/container. Runs on real compute — VMs, NUCs, servers, whatever spare hardware is available. Spawns Claude Code instances per task. Manages workspaces, isolation, and concurrent long-running tasks.

```
$ ./maki-node \
    --nats=maki-nerve.tailnet:4222 \
    --creds=/etc/maki/node.creds \
    --id=node-01
```

On startup:
1. Detects machine specs (cpu, mem, disk, os, arch)
2. Publishes registration to `maki.node.register`
3. Starts heartbeat with resource availability
4. Subscribes to tasks
5. Waits for work

#### Registration

```json
{
  "id": "node-01",
  "type": "node",
  "resources": {"cpu": 4, "memory": "16gb", "disk": "100gb"},
  "status": "online"
}
```

No declared capabilities. Maki figures out what's on the node by exploring it. If juju isn't installed and a task needs it, the task figures that out and installs it. Just like a human would on a new machine.

#### NATS Subjects

```
maki.node.register                    self-registration on startup
maki.node.heartbeat.<id>              periodic heartbeat + resource availability
maki.node.<id>.task                   tasks received from cortex
maki.node.<id>.result.<task-id>       task results published back
maki.node.<id>.status.<task-id>       task progress updates
```

#### Concurrent Tasks

Each task gets its own Claude Code instance. Nodes handle multiple tasks simultaneously:

```
node-01 running:
├── task-8f2a: fixing charm bug (2h, ongoing)
│   Own Claude Code process, own workspace
│
├── task-9b3c: testing Istio config (20min)
│   Own Claude Code process, own workspace
│
└── resources tracked:
    Total:     16gb, 4 cpu
    Used:      6gb, 1.5 cpu
    Available: 10gb, 2.5 cpu
```

Resource reporting is continuous via heartbeat. If a node can't handle a task, it rejects and cortex picks another node or waits.

### maki-node in K8s (Ephemeral Workers)

Workers in the `maki-workers` namespace are the same concept as maki-node — same container image, same logic. Just running as a pod instead of a bare metal process.

```
maki-node --mode=persistent
  Runs on bare metal / VM
  Long-lived process
  Accepts multiple tasks over time
  Self-registers, heartbeats

maki-node --mode=ephemeral
  Runs as a K8s pod
  Single task, dies when done
  Created by maki-gate
  No registration needed — gate knows it exists
```

From cortex's perspective it's identical. Same NATS subjects for results. Same reporting format. Different substrate.

### Cortex's World View

Cortex maintains a live map of all available compute by reading registration streams and heartbeats:

```
┌─────────────────────────────────────────┐
│ Edge Nodes                              │
│                                         │
│ office-pi     4gb   sensors: temp,humid │
│               cpu: 12% mem: 40%         │
│                                         │
├─────────────────────────────────────────┤
│ Compute Nodes                           │
│                                         │
│ node-01       16gb  online, 1 task      │
│ node-02       8gb   online, idle        │
│ K8s cluster   ~11gb free (ephemeral)    │
│                                         │
└─────────────────────────────────────────┘
```

### Cortex Decides Where

Cortex is the brain. It sees everything and decides:

```
"Quick research task, small, disposable"
  → spin up ephemeral maki-node in K8s

"Long charm fix, needs Juju, might take hours"
  → send to a beefy registered node

"Target node is busy, another has room"
  → send there instead

"Everything is slammed, this can wait"
  → queue it

"Turn on the office AC"
  → send to office-pi (edge command)
```

If a node goes offline mid-task, cortex notices via heartbeat loss and can reassign to another node.

### Adding a Node

```
Get a machine (physical or VM, anywhere).
Join Tailnet.
Run maki-node (or maki-edge).
It announces itself.
Maki sees it.
Maki uses it.

Unplug it.
Heartbeat stops.
Maki adapts.
```

No config changes, no redeployments, no templates. Just NATS and a binary.

### Mission Knowledge is Memory, Not Code

Cortex doesn't have hardcoded mission logic or template files. It knows how missions work through its memory in Mem0 and the graph:

```
"When I need quick disposable compute,
 I spin up an ephemeral maki-node in K8s"

"When I need long-running work with
 full environment access, I send to
 a registered node"

"All missions need human approval
 via Discord before execution"
```

This means Maki learns NEW ways to use its nodes over time. What works, what doesn't, which nodes are good for what — all through experience, not configuration.

### Three Binaries

```
Brain:    K8s pods (maki-cortex, maki-stem, etc.)
Nerves:   maki-edge (lightweight Go binary — sense and simple commands)
Hands:    maki-node (heavier binary/container — complex tasks, Claude Code)
```

Everything connects through NATS on Tailnet. Brain is fixed. Body is fluid.

## Consequences

- Maki's body extends beyond K8s — any device on Tailnet can be part of Maki
- Two edge binaries with clear roles: maki-edge (lightweight nerves) and maki-node (compute hands)
- Workers and nodes are the same concept (maki-node) on different substrates
- Nodes self-register and self-report — no config changes to add or remove
- Cortex addresses nodes directly — deliberate targeting, not broadcast
- Concurrent tasks per node — Maki does multiple things at once across multiple nodes
- Mission knowledge lives in memory — evolves with experience, not code changes
- Adding compute is as simple as running a binary on any Tailnet device
- maki-nerve on Tailnet is the single interface between brain and body
