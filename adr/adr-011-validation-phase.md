# ADR-011: Validation Phase (Maki v0)

**Status:** Accepted
**Date:** 2026-03-27

## Context

Before investing in production deployment, we need to validate that the architecture works. Maki v0 is a rough, quickly-assembled version running on the existing dev machine (microk8s). The goal is to hear Maki's heartbeat.

## Decision

### Environment

```
Dev machine with microk8s (already running)
Not the NUC — that's for production Maki
```

### Deployment Strategy

Mix of helm and raw manifests. OpenTofu modules written as we go — each heartbeat produces the module for that component.

```
maki-nerve   → helm (NATS with JetStream)
maki-vault   → raw StatefulSet (PostgreSQL + pgvector)
maki-embed   → raw StatefulSet (Ollama — embeddings only)
maki-graph   → raw StatefulSet (Neo4j CE + APOC)
maki-synapse → raw deployment (OpenAI-compatible Claude LLM proxy for Mem0)
maki-recall  → raw deployment (Mem0 OSS, custom wrapper)
maki-stem    → raw pod, Python, iterate fast
maki-cortex  → raw pod, Claude Agent SDK wrapper
maki-ears    → raw pod, Python Discord bot
maki-immune  → raw pod, Python + Claude Agent SDK
maki-mirror  → raw deployment, React app + NATS WebSocket
maki-gate    → skip for v0 (maki-stem creates workers directly)
maki-edge    → Go binary, run on dev machine outside K8s
maki-node    → binary/process, run on dev machine outside K8s
```

### Not In Scope for v0

- Sandboxing / security hardening (gluetun, NetworkPolicy, Istio)
- HA / multi-node (NUC, Hetzner, Jonsbo)
- IoT / market senses (real sensors)
- Mission approval flow (maki-stem spawns workers directly in v0)
- Full OpenTofu module structure (modules written per heartbeat, full structure in production)

### Heartbeat Milestones

Each heartbeat proves a specific capability. They build on each other in order.

#### Heartbeat 1: Nervous System Alive ✅

```
Deploy maki-nerve (NATS) with JetStream via helm.
Verify:
  - Can publish a message to a subject from a test pod
  - Can subscribe and receive the message
  - JetStream stream creation works
  - KV bucket creation works
Pass: NATS pub/sub and JetStream functional
```

#### Heartbeat 2: Memory Store Alive ✅

```
Deploy maki-vault (PostgreSQL) via helm.
Enable pgvector extension.
Verify:
  - Can connect from a test pod
  - pgvector extension loaded
  - Can create tables with vector columns
  - Can insert and query embeddings
Pass: Postgres + pgvector operational
```

#### Heartbeat 3: Eyes Alive ✅

```
Deploy maki-embed (Ollama), load nomic-embed-text model.
Verify:
  - Ollama responds to health check
  - Can generate embeddings via curl
  - Embeddings have expected dimensionality
Pass: Local embedding generation working
```

#### Heartbeat 4: Relationship Map Alive ✅

```
Deploy maki-graph (Neo4j CE) via helm.
Verify:
  - Can connect from a test pod
  - Can create nodes and relationships
  - Can traverse relationships (multi-hop query)
Pass: Neo4j operational
```

#### Heartbeat 5: Hippocampus Alive ✅

```
Deploy maki-synapse (OpenAI-compatible Claude LLM proxy).
Deploy maki-recall (Mem0 OSS, custom wrapper).
Connect maki-recall to maki-vault + maki-embed + maki-graph + maki-synapse.
Verify:
  - maki-synapse responds to chat completions (text + tool calling)
  - Can store a memory via Mem0 API
  - Can recall it semantically (search by meaning, not keywords)
  - Deduplication works (store same fact twice, get one entry)
  - Graph memory works (store "Adi uses lightkube", query "K8s tools",
    verify relationship-based recall with graph relations in response)
Pass: Autonomous memory extraction, semantic recall, and graph memory working
```

#### Heartbeat 6: Brain Loop (Stem + Cortex Talk)

```
Deploy maki-stem (basic Python event loop).
Deploy maki-cortex (Claude Agent SDK wrapper).
Both connected to NATS.
Verify:
  - Stem publishes turn request to maki.cortex.turn.request
  - Cortex receives request, invokes Claude Agent SDK
  - Cortex publishes response to maki.cortex.turn.response
  - Stem receives the response
  - Test with CLI input — no Discord yet, just logs
Pass: NATS-based cortex interface working
```

#### Heartbeat 7: Continuity (Survive Restarts)

```
Connect maki-stem to NATS conversation stream.
Verify:
  - Conversation turns published to NATS stream
  - Stem reads history from stream on startup
  - Kill and restart stem — history survives
  - Kill and restart cortex — stem reconstructs context from stream
Pass: Conversation continuity across restarts
```

#### Heartbeat 8: Learning (Stem + Mem0)

```
Connect maki-stem to Mem0.
Verify:
  - After each turn, stem feeds interaction to Mem0
  - Mem0 extracts preferences autonomously
  - Before next turn, stem queries Mem0 for relevant memories
  - Memories appear in cortex's turn request payload
  - Have a conversation, mention a preference, verify it's recalled later
  - Graph relationships are included in turn context
Pass: Autonomous memory extraction and recall in the brain loop
```

#### Heartbeat 9: Maki Speaks (Discord Connected)

```
Deploy maki-ears (Discord bot).
Connect to maki-stem.
Verify:
  - Send a message in Discord, Maki responds
  - Full round trip: Discord → ears → stem → cortex → stem → ears → Discord
  - Conversation history maintained across multiple messages
  - Memories from previous turns influence responses
Pass: Maki is alive and reachable via Discord
```

#### Heartbeat 10: Inner Life (Idle Heartbeat Loop)

```
Implement idle heartbeat loop in maki-stem (ADR-014).
Create #maki-thoughts Discord channel.
Create NATS KV bucket maki.cortex.config with bootstrap defaults.
Verify:
  - Stem's idle loop triggers after configured interval with no user activity
  - Cortex receives idle_reflection turn with memories, graph context, time context
  - Cortex produces a thought (or stays silent — both valid)
  - Thoughts posted to #maki-thoughts, not #maki-general
  - Quiet hours respected (no thoughts during configured sleep window)
  - Cortex can read and update its own config in maki.cortex.config
  - Cortex adjusts its own idle_interval (self-tuning works)
  - Self-improvement: cortex identifies a gap in its knowledge and surfaces it
Pass: Maki thinks on its own, posts proactive observations, tunes itself
```

#### Heartbeat 11: First Mission (Workers)

```
Note: No maki-gate in v0. Stem creates workers directly.
Verify:
  - Ask Maki something that requires research
  - Cortex includes a mission proposal in its turn response
  - Stem extracts it, creates worker pod(s) directly
  - Workers execute, publish results to NATS
  - Stem collects results, publishes to cortex in next turn request
  - Cortex processes results, responds on Discord
  - Worker pods cleaned up after completion
Pass: Ephemeral worker system functional
```

#### Heartbeat 12: Immune System (maki-immune)

```
Deploy maki-immune (Python service + Claude Agent SDK).
Connect to maki-nerve and Kubernetes API (read-only).
Create NATS KV bucket maki.immune.config with bootstrap defaults.
Verify:
  - Health monitors detect all running components
  - Deliberately kill a pod — maki-immune detects and restarts it (reflex)
  - maki-immune reasons about a simulated issue via its own Claude instance
  - Findings published to NATS subjects (maki.immune.*)
  - Infrastructure lock coordination works (immune acquires, cortex defers)
  - Immune heartbeat loop runs — holistic patrol even when monitors are green
  - Immune adjusts its own config in maki.immune.config (self-tuning works)
  - Immune tightens heartbeat interval after a detected incident
  - Health digest posted to #maki-vitals
Pass: Independent ops intelligence with heartbeat patrol, self-healing, self-tuning
```

#### Heartbeat 13: Body Extends (maki-edge + maki-node)

```
Build maki-edge (Go binary). Run on dev machine outside K8s.
Build maki-node (binary). Run on dev machine outside K8s.
Connect both to maki-nerve via localhost (simulates Tailnet connection).
Verify:
  - maki-edge registers on maki.edge.register with machine specs
  - maki-edge heartbeats on maki.edge.heartbeat.<id>
  - maki-edge publishes mock sensor data (simulated temperature)
  - Cortex can address maki-edge directly (maki.edge.<id>.command)
  - maki-edge receives command and publishes result
  - maki-node registers on maki.node.register with machine specs
  - maki-node heartbeats with resource availability
  - Cortex sends a task to maki-node (maki.node.<id>.task)
  - maki-node spawns Claude Code instance, executes task
  - maki-node publishes result back to NATS
  - Cortex sees both edge and node in its world view
  - Multiple concurrent tasks on maki-node (launch 2 tasks, both run)
  - Kill maki-node — cortex detects heartbeat loss
Pass: Body extends beyond K8s. Edge sensing and compute delegation working.
```

#### Heartbeat 14: Self-Awareness (maki-mirror)

```
Deploy maki-mirror (React app).
Connect to maki-nerve via NATS WebSocket.
Verify:
  - Dashboard loads and connects to NATS
  - Neural map shows all running pods as nodes
  - Edges pulse when NATS traffic flows between components
  - Thought stream shows cortex reasoning in real time
  - Recent memories panel shows what Maki has learned
  - Active missions panel shows mission progress (if running)
  - Vitals panel shows uptime, memory count, turn count
  - Edge/node registrations appear on the map
  - Workers appear when spawned, fade when they die
  - Accessible via Tailnet only (not exposed publicly)
Pass: Maki can see itself. Living dashboard operational.
```

#### Heartbeat 15: Persistence (Survive Full Restart)

```
The ultimate test.
Kill all pods. Delete maki-cortex. Delete maki-stem.
Stop maki-edge and maki-node processes.
Restart everything from scratch.
Verify:
  - Talk to Maki on Discord
  - It remembers your previous conversations
  - It remembers preferences it learned
  - Conversation history is intact
  - It responds as Maki, not as a fresh Claude instance
  - Cortex config (self-tuned values) survives restart
  - Immune config (self-tuned values) survives restart
  - maki-edge and maki-node re-register after restart
  - maki-mirror reconnects and shows live state
Pass: Maki survives death. Identity, memory, and self-tuned behavior persist.
```

### Success Criteria

When all 15 heartbeats pass, Maki v0 is validated. The architecture works. Proceed to:

1. **OpenTofu modules:** Formalize all deployments as Terraform modules (ADR-012)
2. **Hardening:** Apply full sandboxing model (ADR-004)
3. **Production deploy:** Canonical K8s on NUC
4. **HA:** Phase 2 with Hetzner + Jonsbo
5. **Body:** maki-edge and maki-node on real physical devices

### Development Approach

Build bottom-up:
- Heartbeats 1-5: Infrastructure + maki-synapse/maki-recall custom wrappers ✅
- Heartbeats 6-8: Core brain logic, iterate fast
- Heartbeat 9: Discord integration, end-to-end flow
- Heartbeat 10: Inner life, self-tuning
- Heartbeat 11: Missions, ephemeral workers
- Heartbeat 12: Immune system, heartbeat patrol, self-tuning
- Heartbeat 13: Body extension, edge and compute nodes
- Heartbeat 14: Self-awareness dashboard
- Heartbeat 15: Resilience validation

Each heartbeat should be independently testable. If heartbeat 6 fails, you don't need to debug Discord integration — the problem is in the NATS-based cortex interface.

## Consequences

- v0 validates the production architecture — not a throwaway prototype
- Heartbeat milestones provide clear, testable checkpoints
- Security is deferred to post-validation — acceptable for a dev environment
- The dev machine stays separate from production NUC
- Each heartbeat builds confidence that the next layer will work
- Failing fast on a fundamental issue saves weeks of production deployment effort
- Inner life (HB10) and immune patrol (HB12) validate that Maki is a presence, not just an assistant
- Self-tuning validation ensures both intelligences can govern their own behavior
- Body extension (HB13) validates the edge/node architecture without needing separate physical hardware
- Self-awareness (HB14) validates Maki can observe itself
