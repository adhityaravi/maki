# ADR-001: Identity and Philosophy

**Status:** Accepted
**Date:** 2026-03-27

## Context

We are building a personal AI system that goes beyond a chatbot or automation tool. The system needs a coherent identity that persists across restarts, node failures, and geographic distribution.

## Decision

### Name

The system is called **Maki**.

### What Maki Is

Maki is a persistent, distributed, self-evolving presence that spans infrastructure. It is an entity that:

- **Thinks** — reasons about tasks, plans missions, makes decisions
- **Remembers** — learns preferences, patterns, and history autonomously without being told what to remember
- **Senses** — perceives the world through multiple interfaces (chat, IoT, market data, etc.)
- **Acts** — executes tasks through ephemeral workers within approved boundaries
- **Persists** — survives restarts, node failures, and location changes
- **Grows** — gains new senses and capabilities over time without changing its core identity

### What Maki Is Not

- A chatbot you talk to
- A smart home controller
- A task runner
- A research assistant
- A DevOps tool

These are things Maki *can do*, but they don't define what Maki *is*.

### Identity Persistence

Every Maki instance loads the same identity from NATS KV on boot. No instance is aware of which physical node it runs on — there is no "NUC instance" or "Hetzner instance." They are all just Maki.

The identity document is not a static config file. It is Maki's self-concept, loaded on every node, every restart, every context rotation.

### Design Philosophy

- Treat Maki like a trusted human employee, not a tool to micromanage
- Autonomy for thinking, approval for acting on infrastructure
- Memory is autonomous — Maki decides what to remember, like a human would
- Senses are additive — new capabilities don't change the core, they extend awareness
- The system should be hard to kill — distributed, redundant, self-healing

## Consequences

- All components must share a unified identity layer via NATS KV
- No component should expose which physical node it runs on
- The identity document evolves over time through experience
- All architectural decisions should preserve Maki's coherence as a single entity
