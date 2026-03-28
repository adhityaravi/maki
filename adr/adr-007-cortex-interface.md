# ADR-007: Cortex NATS-Based Interface

**Status:** Accepted
**Date:** 2026-03-27

## Context

The cortex (Claude Agent SDK) is the most powerful and least trusted component. It can reason, plan, and execute shell commands. To minimize attack surface, cortex must be isolated from data stores and Kubernetes. Cortex and brainstem run as separate pods with separate failure modes — communication happens via NATS subjects, not shared volumes.

## Decision

### Principle

Cortex is a pure reasoning engine with no direct access to persistent state. It only sees what the brainstem explicitly publishes to scoped NATS subjects. It communicates intent by publishing to NATS subjects, not by querying data stores.

### Why NATS-Based

- **Security:** Cortex connects to NATS but only on scoped subjects (`maki.cortex.*`). It cannot query the conversation stream, KV buckets, Mem0, Postgres, or Neo4j. If compromised, it only has the current turn's context.
- **Distribution:** Separate pods communicate over the network. This works identically on a single node (v0) and across geographic sites (phase 2). No architecture change needed when scaling.
- **Failure isolation:** Cortex crashing does not take down brainstem. Brainstem detects the failure and handles recovery independently.
- **Replaceability:** The cortex implementation could change (different LLM, different runtime) without changing the interface. Anything that subscribes to NATS and publishes responses can be the cortex.
- **Consistency:** NATS is already the nervous system for all inter-component communication. Using it for cortex↔stem avoids introducing a second IPC mechanism.

### NATS Subjects

```
maki.cortex.turn.request     brainstem publishes context + prompt
maki.cortex.turn.response    cortex publishes response
maki.cortex.mission.propose  cortex publishes mission proposals
maki.cortex.mission.results  brainstem publishes completed mission results
maki.cortex.health           cortex publishes heartbeat
```

### Turn Request Payload (Brainstem → Cortex)

Brainstem publishes a single message containing all context for the turn:

```json
{
  "turn_id": "turn-abc123",
  "identity": "... Maki's self-concept and behavioral guidelines ...",
  "conversation": [
    {"role": "user", "content": "...", "timestamp": "..."},
    {"role": "assistant", "content": "...", "timestamp": "..."}
  ],
  "memories": [
    {"text": "Prefers Neovim over VS Code", "relevance": 0.92},
    {"text": "Works on Juju charms daily", "relevance": 0.87}
  ],
  "graph_context": [
    "Adi --uses--> lightkube --is_a--> K8s client",
    "Adi --works_on--> Juju charms --requires--> Python"
  ],
  "prompt": "the current user message or event",
  "mission_results": null
}
```

Cortex receives exactly what it needs for this turn — nothing more.

### Turn Response Payload (Cortex → Brainstem)

```json
{
  "turn_id": "turn-abc123",
  "response": "... Maki's response text ...",
  "mission_proposals": []
}
```

Mission proposals are embedded in the response payload rather than published separately, keeping the turn atomic. Brainstem extracts and processes them.

### Context Preparation (Brainstem)

Before each turn, brainstem assembles the context:

1. **Identity** — loaded from NATS KV bucket `maki.identity`. Maki's persistent self-concept. Updated rarely.
2. **Conversation** — last N turns read from NATS JetStream `maki.conversation` stream. Provides continuity.
3. **Memories** — brainstem queries Mem0 with the current message, retrieves semantically relevant memories (flat vector search + graph relationships).
4. **Mission results** — if a mission just completed, results are included in the payload.

### Response Handling (Brainstem)

After receiving cortex's response:

1. Sends the response to Discord via maki-ears (NATS subject)
2. Publishes the full turn (user message + cortex response) to NATS conversation stream
3. Feeds the interaction to Mem0 for autonomous memory extraction (flat + graph)
4. Processes any mission proposals from the response
5. Releases turn lock

### Cortex Implementation

maki-cortex is a Python service that:

1. Subscribes to `maki.cortex.turn.request`
2. On receiving a request, constructs a system prompt from identity + memories + graph context
3. Invokes the Claude Agent SDK with the user prompt and system prompt
4. Collects the response from the SDK
5. Publishes the response to `maki.cortex.turn.response`
6. Exposes MCP tools to the SDK for mission proposals and structured actions

The Claude Agent SDK spawns the Claude Code CLI as a subprocess. Authentication uses OAuth (Claude subscription). The CLI has internet access via the pod's gluetun sidecar for inference (api.anthropic.com) and general web access. maki-cortex uses the SDK directly — it does NOT go through maki-synapse (which exists only as an OpenAI-compatible proxy for maki-recall/Mem0).

### Context Window Management

Each turn is a fresh SDK invocation with context provided via the NATS payload. There is no persistent Claude Code process to rotate. Brainstem controls how much context to include by adjusting the conversation history depth and memory selection.

If context becomes too large for a single turn:
1. Brainstem reduces conversation history depth
2. Brainstem summarizes older turns before including them
3. Summary is fed to Mem0 for long-term extraction
4. Conversation continues with compressed context

### Turn Lifecycle

```
1. Event arrives (via NATS: Discord message, mission result, mission failure)
2. Brainstem acquires turn lock (NATS KV)
3. Brainstem assembles context:
   - identity (from NATS KV)
   - conversation history (from NATS stream, last N turns)
   - memories (from Mem0 semantic + graph search on current query)
   - mission results if applicable
4. Brainstem publishes turn request to maki.cortex.turn.request
5. Cortex receives request, invokes Claude Agent SDK
   SDK reasons, may call MCP tools (propose_mission, etc.)
6. Cortex publishes response to maki.cortex.turn.response
7. Brainstem receives response
8. Brainstem sends response to Discord via maki-ears
9. Brainstem publishes turn to NATS conversation stream
10. Brainstem feeds interaction to Mem0
11. Brainstem processes any mission proposals
12. Brainstem releases turn lock
```

## Consequences

- Cortex has no direct access to Mem0, Postgres, or Neo4j — only scoped NATS subjects
- A compromised cortex can only leak the current turn's context
- The interface works identically on one node or across geographic sites
- Cortex and brainstem fail independently — no shared volume coupling
- No architecture change needed when moving from v0 to production multi-node
- The cortex could be replaced with any service that speaks NATS — not tied to Claude Agent SDK
- Each turn is a fresh invocation — no context window rotation complexity
- All context assembly logic lives in brainstem, keeping cortex focused on reasoning
