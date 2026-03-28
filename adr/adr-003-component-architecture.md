# ADR-003: Component Architecture

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki's brain runs as multiple pods in Kubernetes, each with a single responsibility. This separation enables independent scaling, restart, and security boundaries.

## Decision

### Namespaces

```
maki           — persistent brain pods
maki-workers   — ephemeral mission pods (empty between tasks)
```

### Component Map

#### maki-cortex (The Thinker)

- **What:** Wrapper around Claude Agent SDK
- **Role:** Reasoning engine. Plans, thinks, writes, decides. Has an inner life — proactively reflects, researches, and surfaces insights during idle time (ADR-014).
- **Build:** Custom
- **Interface:** NATS-based. Receives context via NATS turn request subject, publishes responses and mission proposals via NATS subjects. No direct access to maki-recall, maki-vault, or maki-graph.
- **Internet:** Via gluetun sidecar (anonymized)
- **Direct connections:** api.anthropic.com (inference via SDK, direct), maki-nerve (NATS, scoped subjects only) — no access to data stores
- **Note:** Uses Claude Agent SDK directly (MCP tools, multi-turn, full agentic loop). Does NOT go through maki-synapse.

#### maki-synapse (LLM Gateway for Mem0)

- **What:** OpenAI-compatible LLM proxy backed by Claude Agent SDK
- **Role:** Translates OpenAI-format chat completion requests (including tool calling) into Claude SDK calls. Authenticates via Claude subscription OAuth token. Exists because mem0 requires an OpenAI-compatible LLM endpoint for memory extraction and graph entity work.
- **Build:** Custom (FastAPI + claude-agent-sdk)
- **Connections:** api.anthropic.com (inference via SDK). No access to data stores or NATS.
- **Consumer:** maki-recall only
- **Note:** maki-cortex and maki-immune each have their own direct Claude SDK instances.
- **TODO:** Current implementation uses stateless `query()` per request (~15s overhead from CLI subprocess spawn). Switch to pooled `ClaudeSDKClient` instances with persistent connections to reduce per-call latency to ~2-3s.

#### maki-stem (Brainstem — The Coordinator)

- **What:** Python service, async event loop
- **Role:** Autonomic nervous system. Keeps everything running. Manages context for cortex. Watches missions. Feeds hippocampus. Triggers cortex when decisions are needed.
- **Build:** Custom
- **Interface:** The only component that talks to everything internal. No internet access.
- **Key responsibilities:**
  - Receives events via NATS (Discord message, mission complete, mission failed, worker crashed)
  - Prepares context for cortex (identity, conversation history, relevant memories)
  - Publishes turn requests to cortex via NATS subject
  - Receives cortex responses via NATS subject
  - Publishes turns to NATS conversation stream
  - Feeds interactions to Mem0 for autonomous memory extraction (flat + graph)
  - Monitors mission progress via NATS
  - Handles worker retries and TTL enforcement
  - Manages context window rotation for cortex

#### maki-recall (Hippocampus — Long-term Memory)

- **What:** Mem0 server (custom thin wrapper around mem0ai library)
- **Role:** Autonomous memory extraction, preference learning, semantic recall, graph memory
- **Build:** Custom wrapper, mem0ai library
- **Connections:** maki-vault (vector store), maki-embed (embeddings), maki-graph (graph memory), maki-synapse (LLM for memory extraction and graph entity work)

#### maki-embed (Eyes — Perception)

- **What:** Ollama serving embedding model (nomic-embed-text)
- **Role:** Turns text into vector embeddings for maki-recall's semantic search
- **Build:** Existing software (Ollama), deployed via OpenTofu
- **Connections:** None. No network access at all. Model loaded locally.

#### maki-graph (Relationship Map — Neo4j)

- **What:** Neo4j Community Edition
- **Role:** Entity-relationship graph database for maki-recall's graph memory layer
- **Build:** Existing software (Neo4j CE), deployed via OpenTofu
- **Connections:** None inbound except maki-recall. No internet access.
- **What it stores:** Entity nodes (people, tools, projects, preferences) and typed relationship edges between them. Enables multi-hop inference queries.

#### maki-ears (Ears/Mouth — Discord Interface)

- **What:** Python Discord bot
- **Role:** Receives messages, sanitizes input, formats output, sends approval requests
- **Build:** Custom (discord.py)
- **Connections:** Discord API (external), maki-stem (internal) only
- **Discord channel structure:**
  - `#maki-general` — normal conversation
  - `#maki-missions` — mission proposals, each gets a thread, ✅/❌ reactions for approval
  - `#maki-memory` — soft notifications ("Learned: Adi prefers X")
  - `#maki-thoughts` — proactive observations, follow-ups, curiosity (ADR-014)
  - `#maki-vitals` — health status, immune system alerts
  - `#maki-security` — CVE findings, security scan results
  - `#maki-market` — future: market alerts and trade proposals
  - `#maki-home` — future: home automation notifications

#### maki-gate (Gatekeeper — Approval Controller)

- **What:** Small Python service
- **Role:** The only pod with RBAC to create worker pods. Acts only on validated approval tokens.
- **Build:** Custom
- **Connections:** Kubernetes API (maki-workers namespace only), NATS (audit logging)
- **Design:** Smallest, simplest, most auditable component. Validates cryptographically signed approval tokens with 5-minute expiry. Single-use tokens stored in NATS KV, deleted after use.

#### maki-nerve (Nervous System — NATS)

- **What:** NATS server with JetStream
- **Role:** Real-time coordination, conversation stream, KV state, pub/sub. Also the single interface between brain (K8s) and body (Tailnet devices).
- **Build:** Existing software (NATS), deployed via OpenTofu
- **Tailnet exposure:** Exposed to Tailnet via Tailscale Kubernetes operator. Edge nodes and compute nodes connect here.
- **Connections:** Other maki-nerve nodes via Tailnet (phase 2), maki-edge and maki-node binaries on Tailnet devices

#### maki-vault (Memory Store — Postgres)

- **What:** PostgreSQL with pgvector extension
- **Role:** Durable backend for maki-recall
- **Build:** Existing software (PostgreSQL), deployed via OpenTofu
- **Connections:** Replication to other sites via Tailnet (phase 2)

#### maki-mirror (Dashboard — Self-awareness)

- **What:** React web app
- **Role:** Living dashboard showing Maki's state in real time
- **Build:** Custom (React)
- **Connections:** NATS via WebSocket (subscribes to maki.> subjects)
- **Features:**
  - Neural map — force-directed graph of pods, edges pulse with traffic, nodes glow when active, workers appear/fade as they spawn/die
  - Thought stream — cortex reasoning in real time
  - Recent memories — what Maki has learned
  - Active missions — progress bars, agent pipeline visualization
  - Vitals — uptime, memory count, turn count, node status
  - Signal flow — NATS traffic visualization
  - Senses — IoT state, market data, Discord status
- **Accessible via Tailnet only**

#### maki-immune (Immune System — Self-healing)

- **What:** Python service with Claude Agent SDK
- **Role:** Independent ops/security intelligence. Monitors health, detects drift, scans vulnerabilities, pushes fixes. Has its own Claude instance for reasoning about infrastructure issues.
- **Build:** Custom
- **Connections:** maki-nerve (NATS), Kubernetes API (read-only), api.anthropic.com (inference), internet via gluetun (CVE databases)
- **Details:** See ADR-013

### Pod Count Summary

```
namespace: maki (13 persistent pods)
├── maki-cortex      (Claude Agent SDK wrapper — The Thinker)
├── maki-synapse     (OpenAI-compatible LLM proxy for Mem0)
├── maki-stem        (orchestrator)
├── maki-recall      (Mem0)
├── maki-embed       (Ollama — embeddings only)
├── maki-graph       (Neo4j)
├── maki-ears        (Discord bot)
├── maki-gate        (approval controller)
├── maki-immune      (ops/security intelligence)
├── maki-nerve       (NATS)
├── maki-vault       (Postgres)
├── maki-mirror      (dashboard)
├── maki-sense       (future: IoT bridge)
└── maki-market      (future: financial data)

namespace: maki-workers (ephemeral)
└── (maki-node pods come and go)

Tailnet body (dynamic, any device):
├── maki-edge instances  (sensors + simple commands, lightweight Go binary)
└── maki-node instances  (compute + Claude Code, heavier binary)
    See ADR-015 for details.
```

### Resource Estimates (NUC 16GB)

```
maki-embed:    ~2GB    (embedding model — embeddings only, no LLM)
maki-graph:    ~1.5GB  (Neo4j Community Edition)
maki-vault:    ~1GB    (Postgres)
maki-nerve:    ~100MB  (NATS)
maki-recall:   ~500MB  (Mem0)
maki-synapse:  ~500MB  (Claude SDK LLM proxy)
maki-cortex:   ~500MB  (Claude Agent SDK wrapper)
maki-stem:     ~200MB  (orchestrator)
maki-ears:     ~100MB  (Discord bot)
maki-gate:     ~100MB  (approval controller)
maki-immune:   ~300MB  (ops/security intelligence)
maki-mirror:   ~100MB  (dashboard)
────────────────────────
Total:         ~7-7.5GB

Leaves ~8.5-9GB for K8s overhead, workers, headroom.
```

### Build Classification

All components are deployed and managed via OpenTofu + Terragrunt (ADR-012).

**Existing software (deployed via helm/OpenTofu):**
- maki-nerve (NATS)
- maki-vault (PostgreSQL + pgvector)
- maki-graph (Neo4j CE)
- maki-embed (Ollama)
- maki-recall (Mem0 OSS)

**Custom (built from scratch):**
- maki-cortex (Claude Agent SDK wrapper)
- maki-synapse (OpenAI-compatible LLM proxy for Mem0)
- maki-stem (orchestrator)
- maki-ears (Discord bot)
- maki-gate (approval controller)
- maki-immune (ops/security intelligence)
- maki-mirror (dashboard)

## Consequences

- 13 persistent pods in the brain namespace — manageable on 16GB NUC
- Clear separation of concerns enables independent development and testing
- Each custom component can be built and tested in isolation before integration
- All deployment managed declaratively via OpenTofu (ADR-012)
- Adding new senses (IoT, market, etc.) means adding new pods without changing existing ones
