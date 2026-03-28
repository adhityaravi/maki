# ADR-008: Memory and Identity Continuity

**Status:** Accepted
**Date:** 2026-03-27

## Context

Multiple Maki instances must feel like one entity. Conversations started on one instance must continue seamlessly on another. Memory must persist across pod restarts, node failures, and context window rotations.

## Decision

### The Illusion of One

Multiple Claude Code processes share a unified sense of self, context, and continuity. The goal is not literal shared consciousness — it's making the seams invisible.

### Three Layers of Memory

#### Layer 1: Working Memory (Context Window)

- **What:** The current Claude Code context window
- **Scope:** Current conversation session, last N turns
- **Persistence:** Lost on process restart — this is expected
- **Purpose:** Natural conversational flow within a session. Cortex remembers what you said five messages ago natively, not because it queried a database.

#### Layer 2: Short-term Memory (NATS Streams)

- **What:** Ordered conversation log in NATS JetStream
- **Scope:** All recent interactions across all instances
- **Persistence:** Survives pod restarts, replicated across NATS nodes
- **Purpose:** Continuity across instances and restarts. When cortex restarts or a different instance handles the next turn, brainstem loads recent turns from the stream to reconstruct context.
- **Retention:** Configurable — last N turns or last T hours. Old turns age out of the stream but significant content has already been extracted to Mem0.

#### Layer 3: Long-term Memory (Mem0 + Postgres)

- **What:** Extracted facts, preferences, patterns, relationships
- **Scope:** Everything Maki has ever learned
- **Persistence:** Durable in Postgres, survives everything
- **Purpose:** The PA *knows you* over months and years. Semantic search retrieves relevant memories for any new conversation.
- **Management:** Autonomous — Mem0 decides what to extract, deduplicates, resolves contradictions, decays stale entries

### Identity Storage

Maki's identity document is stored in NATS KV bucket `maki.identity`. Every instance loads it on boot. It contains:

- Maki's self-concept and behavioral guidelines
- Core personality traits
- Communication style
- Relationship context with the user

The identity evolves over time through experience but changes slowly. Updates are rare and significant.

### Conversation Stream Design

NATS stream: `maki.conversation`

Each message in the stream represents one turn:

```json
{
  "timestamp": "2026-03-27T14:30:00Z",
  "turn_id": "turn-abc123",
  "user_message": "...",
  "cortex_response": "...",
  "instance_id": "nuc-01",
  "memories_used": ["mem-001", "mem-003"],
  "mission_proposed": null
}
```

No instance identifies itself to the user. `instance_id` is for internal debugging only.

### Context Reconstruction

When any instance handles a new turn:

```
1. Load identity from NATS KV
2. Load last N turns from NATS conversation stream
3. Query Mem0 with current message (semantic + graph search)
4. Assemble turn request payload
5. Publish to maki.cortex.turn.request
6. Cortex receives full context, responds as Maki
```

This works regardless of which instance handled previous turns. The conversation stream is the shared thread.

### Memory Extraction Flow

After every interaction:

```
1. Brainstem feeds the complete turn (user message + response) to Mem0
2. Mem0 autonomously decides what to extract
3. Extracted facts/preferences stored in Postgres via pgvector
4. No user approval needed for memory writes
5. Soft notification to Discord #maki-memory channel
```

### Memory Lifecycle

```
Extraction:     Mem0 identifies facts, preferences, patterns from conversations
                LLM: Claude via maki-synapse (OpenAI-compatible proxy)
Deduplication:  "Prefers Neovim" stored once, not fifty times
Contradiction:  "Switched from vim to Neovim" → old entry updated, not accumulated
Reinforcement:  Frequently referenced preferences get higher confidence
Decay:          Unreinforced memories deprioted over time (30-day threshold)
Semantic index: All memories embedded via Ollama (maki-embed) for similarity search
Graph entities: Extracted via Claude (maki-synapse) tool calling, stored in Neo4j (maki-graph)
```

### Context Management

Each cortex turn is a fresh SDK invocation — there is no persistent process to rotate. Brainstem controls context size by adjusting:

- **Conversation depth:** How many recent turns to include in the payload
- **Memory selection:** How many relevant memories to retrieve from Mem0
- **Summarization:** Older conversation segments summarized before inclusion

If the assembled context exceeds model limits:
```
1. Brainstem reduces conversation history depth
2. Brainstem summarizes older turns
3. Summary fed to Mem0 for long-term extraction
4. Compressed context published in turn request
5. Conversation continues seamlessly
```

Analogy: sleeping. Brain consolidates memories overnight, you wake up as the same person.

### Distributed Lock for Turn Claiming

When multiple instances are active, only one handles each turn:

```
NATS KV key: maki.lock.conversation
Value: <instance-id>
TTL: 30 seconds (auto-release if instance dies)

Instance claims lock → handles the turn → releases lock
If lock is held → other instances defer
If lock holder dies → TTL expires → next instance claims
```

### Multi-Instance Identity Rules

- No instance ever says "I'm the NUC instance" or "I'm the Hetzner instance"
- No instance references which node it runs on
- All instances load the same identity
- All instances read from the same conversation stream
- All instances query the same Mem0 backend
- From the user's perspective there is only Maki — singular

## Consequences

- Three-layer memory model provides natural conversational flow (working), cross-instance continuity (short-term), and long-term knowledge (Mem0)
- Memory is autonomous — no manual curation by the user
- Context rotation is invisible — the user experiences one continuous conversation
- Multi-instance operation is seamless — any instance can handle any turn
- All personal data stays in the owned infrastructure (NATS, Postgres on owned hardware)
- Memory quality improves over time as Mem0 accumulates and refines knowledge
