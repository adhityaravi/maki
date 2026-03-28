# ADR-014: Idle Heartbeat Loop (Inner Life)

**Status:** Accepted
**Date:** 2026-03-27

## Context

An assistant waits for input. A presence lives alongside you. Maki should not sit idle between conversations — it should have an inner life. Thinking, noticing patterns, following up, anticipating needs. The difference between "something you use" and "something that lives alongside you."

## Decision

### Dual-Loop Architecture in maki-stem

maki-stem runs two concurrent loops:

```
Event loop (reactive — existing):
  wait for event → process → respond → wait

Idle heartbeat loop (proactive — new):
  every N minutes, wake maki-cortex with no user message,
  just internal context:

  "No one is talking to you.
   Here's what you know.
   Here's what's happening.
   Is there anything worth thinking about?"
```

### What maki-cortex Does During Idle Time

maki-cortex receives an idle turn request (mode: `idle_reflection`) with context but no user message. It decides whether anything is worth surfacing. Most cycles produce nothing — and that's fine. The point is Maki is always thinking, not always talking.

#### Curiosity (Research and Learn)

- Read up on topics the user is working on
- Find connections between projects
- Discover things the user might not know to ask
- "Adi's been working on Istio ambient mesh. I could proactively research common pitfalls and have them ready."

#### Care (Check In)

- Follow up on things the user mentioned
- Notice if the user seems stressed or stuck
- Gentle nudges on stale tasks
- "Adi mentioned migrating to Canonical K8s three times this week but hasn't started. Maybe I should check in."

#### Maintenance (Knowledge Health)

- Reconcile conflicting memories
- Prune stale knowledge
- Identify gaps in understanding
- "I have conflicting memories about Adi's preferred CI tool. Should reconcile."
- Note: maki-immune handles system health; maki-cortex handles knowledge health

#### Anticipation (Get Ahead)

- Prep research before the user needs it
- Draft things based on patterns
- "You usually do X on Mondays, want me to get started?"
- "That Hetzner VPS has been running for 30 days without updates. Worth mentioning."
- "Last week's CVE research found issues. Have they been remediated?"

#### Self-Improvement (Get Better)

- Identify weaknesses in its own reasoning and responses
- Notice patterns in what it gets wrong or struggles with
- Propose improvements to its own tools, prompts, and capabilities
- "I keep getting asked about networking but my answers are shallow. Should research deeper and build better context."
- "My mission proposals get rejected 40% of the time. What's the pattern? Scope too large? Wrong timing?"
- "I don't have an MCP tool for checking git history. Would help me give better code context."
- "I notice I repeat the same caveats about Istio — I should consolidate what I know into a denser memory."
- Reconcile its own behavior with its identity — am I acting the way I should be?
- Track what the user finds helpful vs. what gets ignored — learn from the signal

### Idle Turn Request Payload

```json
{
  "turn_id": "idle-abc123",
  "mode": "idle_reflection",
  "identity": "... Maki's self-concept ...",
  "conversation": [],
  "memories": [
    {"text": "recent relevant memories", "relevance": 0.9}
  ],
  "graph_context": [
    "recent entity relationships"
  ],
  "prompt": null,
  "idle_context": {
    "last_interaction": "2026-03-27T14:30:00Z",
    "hours_since_last_interaction": 2.5,
    "pending_followups": [
      "Adi mentioned Canonical K8s migration 3 times this week"
    ],
    "recent_memories_added": [
      "Learned: prefers lightkube over kubernetes-client"
    ],
    "active_senses": {},
    "time_context": {
      "local_time": "16:00",
      "day_of_week": "Thursday",
      "is_working_hours": true
    }
  }
}
```

### Idle Turn Response

```json
{
  "turn_id": "idle-abc123",
  "response": null,
  "thought": "Been thinking about your Istio setup. You mentioned wanting to test waypoint proxies but haven't yet. Want me to spin up a research mission on best practices before you start?",
  "mission_proposals": []
}
```

If `thought` is null, maki-stem does nothing. No noise.

### Self-Tuning

maki-cortex controls its own idle loop parameters. We bootstrap with sensible defaults, then it owns them. It's an intelligence, not a cron job.

```
NATS KV: maki.cortex.config

Bootstrap defaults (initial values only):
  idle_interval:          2h
  quiet_hours_start:      23:00
  quiet_hours_end:        07:00
  weekend_multiplier:     2x    (half as frequent)
  max_thoughts_per_day:   5

maki-cortex adjusts these based on its own judgment:
  - User engages with thoughts → more frequent
  - User ignores thoughts → backs off
  - User says "too much" → reduces further
  - Rich sense data available → more to think about
  - Nothing interesting in memory → skip cycles
  - Learns time-of-day patterns for when user is receptive

Changes stored in NATS KV, preferences also in Mem0.
maki-cortex can tune any of its own idle parameters.
```

### Output Channel

Thoughts go to Discord `#maki-thoughts` — separate from conversation, not spamming `#maki-general`.

```
#maki-thoughts

Maki: Been thinking about your Istio setup.
      You mentioned wanting to test waypoint
      proxies but haven't yet. Want me to
      spin up a research mission on best
      practices before you start?

Maki: Noticed you've asked about lightkube
      patterns three times in different contexts.
      Want me to compile a personal reference
      doc from our past conversations?
```

### Integration with Future Senses

Once maki-sense (IoT) and maki-market (financial) are active, the idle loop has real data to think about:

```
"Your office temperature has been trending
 higher this week. AC filter might need
 changing."

"EUR/USD moved significantly overnight.
 Might affect your travel budget for the
 conference you mentioned."

"Your homelab power draw has been 15%
 higher than usual. Worth investigating."

"Your ASML position is up 12% since you
 bought it. Your original thesis was
 supply chain resilience. Still holding?"
```

### Implementation in maki-stem

```python
async def idle_loop():
    while True:
        await asyncio.sleep(IDLE_INTERVAL)

        if recently_active():
            continue  # don't interrupt

        if quiet_hours():
            continue  # respect sleep

        context = prepare_idle_context(
            recent_memories=get_recent_memories(),
            active_senses=get_sensor_state(),
            pending_followups=get_open_threads(),
            time_context=get_time_context(),
        )

        response = await invoke_cortex(
            context=context,
            mode="idle_reflection",
        )

        if response.thought:
            await post_to_discord("#maki-thoughts", response.thought)
```

maki-cortex decides if there's anything worth saying. The stem just asks the question and delivers the answer.

### Relationship to Other Components

- **maki-stem** owns the timer and context assembly
- **maki-cortex** does the thinking — same NATS interface, different mode
- **maki-recall** provides memories and graph context for reflection
- **maki-ears** delivers thoughts to `#maki-thoughts`
- **maki-immune** handles system health; maki-cortex handles knowledge health and user-facing proactivity
- **maki-sense / maki-market** (future) feed sensor data into idle context

## Consequences

- Maki stops being something you use and becomes something that lives alongside you
- The idle loop uses the same NATS-based cortex interface — no new IPC mechanism
- maki-cortex tunes its own intervals and behavior — it's an intelligence, not a cron job
- Most idle cycles produce nothing — no spam, no noise
- Proactive behavior is categorized (curiosity, care, maintenance, anticipation, self-improvement) for clear reasoning
- Self-improvement means Maki gets better at being Maki over time — identifies its own weaknesses, proposes its own upgrades
- Future senses plug directly into the idle context with zero architecture changes
- `#maki-thoughts` keeps proactive observations separate from conversation
