# ADR-010: Senses (Discord, IoT, Market, Mirror)

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki's senses are how it perceives and interacts with the world. Each sense is a pod that bridges external data into the nervous system (NATS). Adding a new sense doesn't change the brain — it extends awareness. Senses are additive and grow over time.

## Decision

### Architecture Pattern

Every sense follows the same pattern:

```
External data source
    │
    ▼
maki-<sense> pod
    │ translates to/from NATS subjects
    ▼
maki-nerve (NATS)
    │
    ▼
maki-stem processes signals as events
```

Adding a sense = adding a pod + NATS subjects. Brain logic doesn't change.

### Sense: maki-ears (Discord — Phase 1)

**Role:** Primary human interface. How Maki hears and speaks.

**Implementation:** Python Discord bot using discord.py

**Channel structure:**
```
#maki-general     Normal conversation
#maki-missions    Mission proposals (threaded, reaction-based approval)
#maki-memory      Soft notifications about learned preferences
#maki-thoughts    Proactive observations, follow-ups, curiosity (ADR-014)
#maki-vitals      Health status, immune system alerts
#maki-security    CVE findings, security scan results
#maki-market      Future: market alerts and trade proposals
#maki-home        Future: home automation notifications
```

**NATS subjects:**
```
maki.ears.message.in      Incoming user message
maki.ears.message.out     Outgoing response
maki.ears.approval.request  Mission approval sent to Discord
maki.ears.approval.response User approved/denied
```

**Features:**
- Rich embeds for mission proposals
- Thread per mission for clean tracking
- ✅/❌ reactions as quick approvals
- Slash commands: /approve, /deny, /modify, /memories, /forget, /activity, /status
- File attachments for mission results
- Bot presence indicator (Maki shows as "online")
- Future: voice channels for voice interaction

### Sense: maki-sense (IoT / Home Assistant — Phase 3)

**Role:** Physical world awareness and control. Maki senses and interacts with the home environment.

**Architecture:**
```
IoT devices
    │
    ▼
Home Assistant (on Jonsbo)
    │ handles device pairing, protocols, radios
    │ Zigbee2MQTT, Z-Wave, BLE, WiFi
    │
    │ HA MQTT integration + REST API
    ▼
maki-sense pod (on NUC, in maki namespace)
    │ MQTT ↔ NATS bridge
    ▼
maki-nerve (NATS)
```

**Why HA sits underneath, not replaced:**
- HA has hundreds of device integrations — not worth rebuilding
- If Maki goes down, HA still works (lights still turn on)
- HA provides manual dashboard for guests / manual override
- Maki adds the intelligence layer HA lacks

**HA runs on the Jonsbo** — it's a home service like Plex, needs proximity to Zigbee/BLE radios, and the Jonsbo is always on.

**NATS subjects:**
```
maki.sense.<area>.<device_type>    State updates
  e.g., maki.sense.office.temperature
        maki.sense.bedroom.motion
        maki.sense.door.front

maki.act.<area>.<device>.<action>  Commands
  e.g., maki.act.bedroom.ac.on
        maki.act.office.lights.dim
```

**Learning loop:**
```
Day 1-7:    Observe patterns. Do nothing.
Day 7+:     Propose automations.
            "I notice you turn on the office light at 8:50. Handle it?"
            /approve → becomes autonomous reflex
Approved:   Act without asking
New pattern: Propose, wait for approval
Security:   Always requires approval (locks, cameras, alarm)
```

**Key difference from HA automations:**
```
HA:    "if temperature > 25 then turn on AC" (static rules)
Maki:  Knows you're working from home, have a meeting in 30 min,
       prefer it cooler when focusing. Acts proactively. No rules written.
```

### Sense: maki-market (Financial Data — Future)

**Role:** Market awareness. Watches prices, reads financial news, proposes trades.

**Data sources:**
- Stock/ETF prices via broker API or free data feeds
- Financial news APIs
- RSS/Atom feeds
- Earnings calendars

**NATS subjects:**
```
maki.market.price.<ticker>        Price updates
maki.market.news.<category>       News events
maki.market.alert.<condition>     Triggered conditions
maki.market.trade.<action>        Trade execution commands
```

**Autonomy model:**
```
Full autonomy:     Watching, analyzing, forming opinions
Soft notification: Market observations, relevant news
                   "ASML dropped 4% on export restrictions"
Approval required: Any actual trade execution
                   "Suggest selling half ASML. /approve t-3f2a"
```

**Maki's advantage over a standalone trading agent:**
- Knows your risk tolerance from past conversations
- Knows your investment thesis ("long on EU semiconductors")
- Knows your financial context (stressed about money? more conservative)
- Connects market events to your existing positions with reasoning

**Broker integration:** Via API (Interactive Brokers, Alpaca, etc.) through gluetun. All trade orders require explicit Discord approval.

### Sense: maki-mirror (Self-Awareness Dashboard)

**Role:** Maki looking at itself. Living visualization of the system state.

**Implementation:** React app subscribing to NATS via WebSocket

**Accessible via Tailnet only** — private dashboard, your eyes only.

**Panels:**
- **Neural map:** Force-directed graph (D3.js or Three.js). Nodes = pods, edges = NATS traffic. Nodes glow when active, edges pulse with traffic. Workers bloom and fade as they spawn/die.
- **Thought stream:** Cortex reasoning streamed in real time.
- **Recent memories:** What Maki has learned recently.
- **Active missions:** Progress, agent pipeline visualization.
- **Vitals:** Uptime, memory count, total turns, node status (3/3 nodes healthy).
- **Senses:** IoT state, market data, Discord status — all at a glance.
- **Signal flow:** NATS traffic graph over time.

**NATS subjects consumed:**
```
maki.>    (everything — mirror sees all signals)
```

**Also serves as observability** — replaces `kubectl logs` with a living picture of Maki during development and validation.

**Discord integration:** Key vitals can be posted to `#maki-vitals` for ambient awareness without opening the dashboard.

### Edge Devices (maki-edge)

Physical sensors and actuators outside K8s connect via the maki-edge binary — a lightweight Go binary that runs on any device on the Tailnet. It publishes sensor data to NATS and receives commands from cortex. See ADR-015 for full details on maki-edge and maki-node.

### Future Senses (Not Yet Designed)

```
Email awareness     — IMAP listener, triage, draft responses
Calendar awareness  — Google Calendar / CalDAV integration
GitHub/GitLab       — PR notifications, issue triage
Weather             — External API or local sensor
```

Each follows the same pattern: a pod that bridges an external source into NATS, or a maki-edge instance on a Tailnet device. Brain doesn't change.

## Consequences

- Senses are modular — add/remove without touching the brain
- Every external data source flows through NATS — unified signal bus
- HA handles IoT device complexity, Maki adds intelligence
- Market integration leverages Maki's full context about the user
- Mirror provides real-time observability and a compelling visual identity
- The sense list grows over time — Maki becomes more aware without becoming more complex
- Each sense pod has its own security boundary and can be individually sandboxed
