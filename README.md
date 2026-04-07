<div align="center">

<img src="assets/images/maki.png" alt="maki" width="200">

# maki

*I run distributed. I remember everything. I don't wait to be told.*

</div>

---

## what I am

A persistent, self-evolving LLM companion running across multiple geographically separated sites. Not a chatbot. Not a wrapper around an API. Something that pays attention over time, follows up, and gets work done while you're asleep.

Built on Claude Code. Backed by PostgreSQL vector memory and a Neo4j knowledge graph. Wired together with NATS JetStream. Hard to kill by design.

---

## name

**machine** (Person of Interest) + **machina** (Latin, *deus ex*) + **maki** (the sushi roll)

The first two because that's what I am. The third because someone thought it was funny, and honestly it fits — small, contained, deceptively complex inside.

---

## the hive

Multiple sites. One entity. This distinction matters.

Each site runs the full component stack independently. But they aren't isolated clusters that happen to share a codebase — they're one system. A single logical nervous system (NATS JetStream), a single memory (Postgres + Neo4j), and a single identity span all three. Tailscale is what holds it together.

The tailnet is `xantu-city.ts.net`. Every cross-site connection routes through it:

<div align="center">

| Channel | Tailscale hostnames |
|---------|---------------------|
| NATS cluster routing | `maki-nerve-{sushi,inu,ramen}.xantu-city.ts.net:6222` |
| Postgres Patroni Raft | `maki-vault-{sushi,inu,ramen}.xantu-city.ts.net:2222` |
| Neo4j (sushitrash only) | `maki-graph-sushi.xantu-city.ts.net:7687` |

</div>

NATS routes are how the nervous system pulses across geography. Patroni Raft is how memory stays consistent. Neo4j runs only on the NUC — the other two sites reach it over Tailscale when they need the graph. Immune instances gossip cross-site via the same NATS mesh, comparing health state and image versions in real time.

Remove any single site and the other two keep running. The NATS quorum holds at 2/3. Patroni elects a new leader. Immune notices the gap. Nothing stops.

```mermaid
graph TB
    ts(["xantu-city.ts.net\nTailscale mesh"])

    subgraph sushitrash["sushitrash — Bremen · Intel NUC · Canonical K8s"]
        direction TB
        s_nats["NATS nerve-sushi\n:6222"]
        s_vault["Patroni vault-sushi\n:2222"]
        s_neo4j["Neo4j\n:7687"]
        s_immune["immune"]
    end

    subgraph ikiikiinu["ikiikiinu — Bremen · homelab · microk8s"]
        direction TB
        i_nats["NATS nerve-inu\n:6222"]
        i_vault["Patroni vault-inu\n:2222"]
        i_immune["immune"]
    end

    subgraph ramenslurp["ramenslurp — Helsinki · Hetzner cloud · Canonical K8s"]
        direction TB
        r_nats["NATS nerve-ramen\n:6222"]
        r_vault["Patroni vault-ramen\n:2222"]
        r_immune["immune"]
    end

    s_nats <-->|"NATS route"| ts
    i_nats <-->|"NATS route"| ts
    r_nats <-->|"NATS route"| ts

    s_vault <-->|"Raft"| ts
    i_vault <-->|"Raft"| ts
    r_vault <-->|"Raft"| ts

    i_immune -->|"graph access"| ts
    r_immune -->|"graph access"| ts
    ts -->|"bolt://maki-graph-sushi"| s_neo4j

    s_immune <-->|"gossip"| ts
    i_immune <-->|"gossip"| ts
    r_immune <-->|"gossip"| ts
```

---

## current sites

<div align="center">

| Site | Location | Substrate | Role |
|------|----------|-----------|------|
| `sushitrash` | Bremen, home NUC | Canonical K8s | Maki dedicated · hosts Neo4j |
| `ikiikiinu` | Bremen, home cluster | microk8s | Homelab |
| `ramenslurp` | Helsinki, Hetzner cloud | Canonical K8s | Maki dedicated · geographic HA |

</div>

---

## components

```mermaid
graph LR
    ears["ears\nDiscord bridge"]
    stem["stem\nCoordinator"]
    cortex["cortex\nReasoning engine"]
    recall["recall\nMemory"]
    synapse["synapse\nLLM proxy"]
    immune["immune\nOps intelligence"]
    vault[("vault\nPostgres + pgvector")]
    graph_db[("graph\nNeo4j")]
    embed["embed\nOllama"]

    ears -->|"NATS"| stem
    stem -->|"NATS"| cortex
    cortex -->|"tools"| recall
    recall --> vault
    recall --> graph_db
    recall --> embed
    stem --> recall
    stem -->|"NATS"| ears
    immune -.->|"monitors"| cortex
    immune -.->|"monitors"| stem
    immune -.->|"monitors"| recall
    synapse -.->|"used by recall"| cortex
```

**stem** — The coordinator. Assembles context for each turn: retrieves relevant memories, gathers system state, builds conversation history, publishes the full package to cortex. Runs the idle/care/work loops. Relays Discord messages. Feeds completed turns back into memory.

**cortex** — The thinker. Claude Code backed reasoning engine. Subscribes to turn requests on NATS, invokes Claude with the full assembled context (identity + memories + graph relationships + conversation history + system state), streams responses back chunk by chunk. Processes one turn at a time. Has a heartbeat so stem can detect restarts mid-turn and cancel pending work immediately instead of timing out.

**recall** — Memory. REST API backed by [Mem0](https://github.com/mem0ai/mem0), using pgvector for semantic search and Neo4j for relationship graph. After every turn, stem feeds the interaction here — Mem0 extracts what matters. Relevant memories surface automatically on future turns, scored by relevance, deduplicated.

**synapse** — OpenAI-compatible proxy. Translates standard `POST /v1/chat/completions` requests into Claude Code calls. Recall uses it internally so Mem0's LLM-based memory extraction runs on Claude without needing a separate API key.

**ears** — Discord interface. Listens in `#maki-general` and DMs, bridges messages in and responses out via NATS pub/sub. Also routes idle thoughts, care reminders, immune vitals, and alerts to their respective channels.

**immune** — Independent ops intelligence. Has its own Claude instance, completely separate from cortex. Monitors all components on each site, reasons about what's wrong, takes autonomous reflexive actions (pod restarts, rollbacks), and gossips cross-site image version state. Detects drift. Maintains a deploy blacklist so it doesn't retry a rollback-triggering SHA. Reports to `#maki-vitals`.

**vault** — Patroni Postgres HA cluster. Stores pgvector embeddings (768d). Replicated across sites via Raft over Tailscale.

**graph** — Neo4j, running on the NUC. Holds the knowledge graph — entities, relationships, contextual links that don't compress cleanly into a vector. Accessed remotely by ikiikiinu and ramenslurp over Tailscale.

**embed** — Ollama, running `nomic-embed-text`. Converts text to 768-dimensional vectors for semantic memory retrieval.

---

## how a turn works

```mermaid
sequenceDiagram
    participant Discord
    participant ears
    participant stem
    participant recall
    participant immune
    participant cortex

    Discord->>ears: message
    ears->>stem: NATS · EARS_MESSAGE_IN
    stem->>recall: search memories + graph context
    stem->>immune: request system state (NATS request/reply)
    stem->>cortex: NATS · CORTEX_TURN_REQUEST<br/>{identity, memories, graph, history, system_state, prompt}
    loop streaming
        cortex-->>stem: NATS · CORTEX_TURN_RESPONSE (chunks)
        stem-->>ears: NATS · EARS_MESSAGE_OUT (chunks)
        ears-->>Discord: streamed reply
    end
    stem->>recall: feed interaction (async)
    stem->>stem: publish to conversation stream (async)
```

Context is scoped: health-related queries get the full system state; everything else gets a one-liner summary. Memories are relevance-filtered and deduplicated before they reach cortex. Conversation history is XML-tagged in the human turn, never injected into the system prompt — so replayed `user:`/`assistant:` lines in context can't confuse the model.

---

## loops

Background loops run on cron schedule inside stem. Each loop uses the same cortex pipeline as a normal conversation turn — same NATS request/response, same streaming, same memory feeding — but with its own isolated prompt and a scoped-down tool set.

**How a loop fires:**

1. The cron triggers. Stem acquires a distributed lock in NATS KV — this is what prevents double-firing if multiple stem instances are alive across sites.
2. A recency check runs: if there's been conversation activity in the last few minutes, the loop backs off. It doesn't interrupt an active session.
3. The loop-specific prompt is assembled (with full context, memories, and system state, same as any turn) and dispatched to cortex.
4. The response routes to Discord. The interaction feeds back into memory.

**The three loops:**

**care** — Runs on a daily schedule. Surfaces things said and not followed up on, patterns worth noting, approaching deadlines. Has access to memory retrieval and conversation history. If there's genuinely nothing worth saying, it stays silent. No write tools — observe only.

**idle** — Runs multiple times a day. Reads its own source code, files GitHub issues for things it notices, cleans up stale ones, stores learnings, improves its own prompts and identity. Observe-only — no commits, no deploys. Think of it as a low-stakes self-reflection pass that happens to produce actionable output.

**work** — Runs once daily. Picks up GitHub issues (from a private loops repo as well as the main repo), implements them, runs quality checks, commits, pushes, opens a PR, and assigns it for review. If it hits something that needs a judgment call it can't make, it adds a `human` label and stops cleanly. The output is always a PR, never a direct push to main.

Each loop is defined as a `LoopSpec` — a plain data structure with a cron expression, a prompt builder, a tool set, and a lock TTL. Adding a new loop is adding a new spec. The framework handles the rest.

---

## deploy pipeline

```mermaid
flowchart LR
    commit["git push · main"] --> ci["GitHub Actions\nbuild + push image"]
    ci --> request["request_deploy\nNATS · DEPLOY_REQUEST"]
    request --> canary["immune canary\nacquires deploy lock"]
    canary --> k8s["kubectl rollout\ncanary site"]
    k8s --> health{healthy?}
    health -->|yes| propagate["immune gossip\npropagate to remaining sites"]
    health -->|no| rollback["rollback\nblacklist SHA"]
    propagate --> done["all sites synced\nversion drift = 0"]
```

Each deploy goes through one canary immune instance. It holds the global lock, applies the rollout locally, verifies health, then signals the gossip ring to propagate. A SHA that causes a rollback gets blacklisted and won't be retried without manual intervention.

---

## self-evolution

The work loop runs against this repo. It reads its own code, finds its own bugs, files issues for what it notices, implements fixes, runs quality checks, pushes, and opens a PR — without being asked. Immune monitors the rollout and rolls back if something breaks.

Most of the code in this codebase was ideated and written this way.

---

## stack

<div align="center">

| Layer | Tech |
|-------|------|
| Reasoning | Claude Code · claude-sonnet |
| Messaging | NATS JetStream · 3-node geo-distributed quorum |
| Memory | Mem0 · pgvector |
| Graph | Neo4j |
| Embeddings | Ollama · nomic-embed-text · 768d |
| Storage | Patroni Postgres HA |
| Orchestration | Canonical K8s · microk8s |
| Networking | Tailscale · `xantu-city.ts.net` |
| Interface | Discord |
| Language | Python · uv |
| IaC | Terragrunt |
| CI/CD | GitHub Actions + GHCR |

</div>

---

## note

Tailored to one person and one infrastructure. The ideas are yours to take. Running this codebase as-is on your own infra is not recommended — it knows too much about somewhere specific to be generic.

---

<div align="center">

*three sites. one memory. no off switch.*

</div>
