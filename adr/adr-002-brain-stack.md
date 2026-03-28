# ADR-002: Brain Stack (NATS + Mem0 + Postgres)

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki needs a data layer that provides real-time coordination, durable long-term memory, and autonomous memory management. The stack must support geographic distribution across multiple Kubernetes clusters connected via Tailscale.

## Decision

The brain stack consists of three components, each serving a distinct purpose.

### NATS with JetStream — The Nervous System

**Role:** Real-time coordination and short-term state.

**Why NATS:**
- Single binary, tiny footprint (~50-100MB RAM)
- Built-in clustering with Raft consensus — handles multi-node distribution natively over Tailnet
- JetStream provides durable ordered streams for conversation history
- KV store built on streams for shared state (identity, locks, task registry)
- Pub/sub for real-time inter-instance communication and heartbeat
- Replaces what would otherwise require Redis + Kafka + etcd as separate components
- CNCF incubating project, actively maintained

**What NATS handles:**
- Conversation stream — ordered log of all interactions, enables continuity across instances
- KV buckets — identity, preferences cache, task registry, distributed locks
- Pub/sub — heartbeat/gossip between nodes, mission events, sensor signals
- Inter-worker communication during missions (scoped subjects per mission)

**Alternatives considered:**
- Redis Streams — no built-in clustering with consensus
- Kafka/Redpanda — too heavy for a homelab setup
- RabbitMQ — heavier, overkill for this use case
- MQTT (Mosquitto/EMQX) — no persistence/streaming, would still need a DB alongside

### Mem0 — The Hippocampus

**Role:** Autonomous memory management layer.

**Why Mem0:**
- Automatically decides what to remember from conversations — no manual curation
- Handles contradiction resolution (preference changed → updates, doesn't accumulate)
- Handles deduplication (won't store "prefers Neovim" fifty times)
- Handles memory decay (old patterns that no longer apply)
- Supports semantic search via vector embeddings ("have I seen something like this before?")
- Python-native, runs as a REST API server
- Backs onto Postgres + pgvector, no separate vector DB needed

**What Mem0 handles:**
- Preference extraction from raw conversations
- Relationship graphs between entities (tools, projects, people)
- Semantic recall — search by meaning, not just keywords
- Memory lifecycle — extraction, deduplication, contradiction resolution, decay

**Alternatives considered:**
- Manual CLAUDE.md editing (OpenClaw pattern) — not autonomous, doesn't scale
- Raw pgvector queries — would have to build all the memory logic ourselves
- Neo4j — too heavy for homelab, JVM-based
- Zep — viable alternative but Mem0 has broader adoption
- Letta/MemGPT — more complex than needed

### Postgres with pgvector — The Memory Store

**Role:** Durable backend for Mem0 and vector embeddings.

**Why Postgres + pgvector:**
- Already charmed in the Juju ecosystem (postgresql-k8s charm exists)
- pgvector extension provides embedding storage and similarity search
- For personal PA scale, pgvector performance is more than sufficient
- Single datastore instead of Postgres + separate vector DB
- Well-understood replication (streaming, Patroni) for HA
- Stores structured memory metadata in regular tables alongside vector-enabled tables

### Ollama — The Eyes

**Role:** Local embedding model for Mem0.

**Why Ollama with local embeddings:**
- Full data sovereignty — no conversation data sent to external embedding APIs
- nomic-embed-text or similar lightweight model (~2GB RAM)
- Only does embeddings, not inference — inference goes to Anthropic API
- Removes external dependency from the memory pipeline
- Mem0 calls Ollama for vectorization before storing in Postgres

**Alternative considered:**
- Anthropic API for embeddings — higher quality but adds external dependency to memory pipeline and sends personal data externally

### Neo4j — The Relationship Map

**Role:** Graph database for entity-relationship reasoning in Mem0.

**Why Neo4j:**
- Mem0's graph memory layer requires Neo4j — it's the only supported graph backend
- Enables multi-hop relationship inference beyond flat semantic search
- Connects entities through typed relationships (person → uses → tool → is_a → category)
- Graph operations run in parallel with vector operations via ThreadPoolExecutor — no latency penalty
- Community Edition is sufficient for personal PA scale (~1-1.5GB RAM)
- Queries like "what tools does Adi use for Kubernetes?" retrieve answers even if the exact phrasing was never used

**What Neo4j handles:**
- Entity storage (people, tools, projects, preferences as graph nodes)
- Relationship edges between entities (uses, prefers, works_on, is_a)
- Multi-hop graph traversal for inference
- BM25 reranking of graph search results

**Without Neo4j, Mem0 only has flat semantic search:**
```
"Adi prefers Neovim"      → retrieved by similarity to "editor"
"Adi uses lightkube"      → NOT retrieved by "Kubernetes tools" (no relationship link)
```

**With Neo4j, Mem0 builds a connected knowledge graph:**
```
Adi --uses--> lightkube --is_a--> K8s client library
Adi --prefers--> Neovim --is_a--> terminal editor
Adi --works_on--> Juju charms --requires--> Python
```

**Alternatives considered:**
- Skip graph entirely — loses relationship reasoning, only flat vector recall
- Other graph DBs — Mem0 only supports Neo4j for graph memory

## How They Work Together

```
NATS answers:   "What's happening right now across all instances?"
Mem0 answers:   "What do I know about Adi from all past interactions?"
Postgres:        Durable storage backend for Mem0's knowledge
Ollama:          Turns text into vectors for semantic search
Neo4j:           "How are these entities related to each other?"
```

### Data Flow for a Single Interaction

```
1. Message arrives (via NATS subject from maki-ears)
2. Brainstem acquires turn lock (NATS KV)
3. Brainstem loads recent context (NATS stream: last N turns)
4. Brainstem loads relevant memories (Mem0 semantic + graph search)
5. Brainstem publishes context to cortex (NATS turn request subject)
6. Cortex receives context, reasons, publishes response (NATS turn response subject)
7. Brainstem publishes turn to NATS conversation stream (other instances sync)
8. Brainstem feeds interaction to Mem0 (autonomous extraction + graph update)
9. Brainstem releases lock
```

## Consequences

- Five infrastructure components to deploy and manage (NATS, Postgres, Ollama, Neo4j) plus Mem0
- NATS and Postgres handle their own clustering over Tailnet — Kubernetes doesn't need to span sites
- Mem0 is stateless itself — can run multiple instances pointing at the same Postgres and Neo4j
- Neo4j adds ~1-1.5GB RAM but enables relationship reasoning beyond flat vector recall
- All personal data stays on owned infrastructure (no external embedding APIs)
- The stack is the same whether running on one node or distributed across three sites
