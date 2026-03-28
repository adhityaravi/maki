# Maki — Architecture Decision Records

Maki is a persistent, distributed, self-evolving AI presence that spans infrastructure. Not a tool, not a chatbot, not an automation layer — an entity that thinks, remembers, senses, learns, and acts.

## ADR Index

| ADR | Title | Status |
|-----|-------|--------|
| [001](./adr-001-identity-and-philosophy.md) | Identity and Philosophy | Accepted |
| [002](./adr-002-brain-stack.md) | Brain Stack (NATS + Mem0 + Postgres) | Accepted |
| [003](./adr-003-component-architecture.md) | Component Architecture | Accepted |
| [004](./adr-004-sandboxing-and-security.md) | Sandboxing and Security Model | Accepted |
| [005](./adr-005-autonomy-and-approval.md) | Autonomy and Approval Model | Accepted |
| [006](./adr-006-mission-system.md) | Mission System (Ephemeral Workers) | Accepted |
| [007](./adr-007-cortex-interface.md) | Cortex NATS-Based Interface | Accepted |
| [008](./adr-008-memory-and-identity-continuity.md) | Memory and Identity Continuity | Accepted |
| [009](./adr-009-infrastructure-and-ha.md) | Infrastructure and High Availability | Accepted |
| [010](./adr-010-senses.md) | Senses (Discord, IoT, Market, Mirror) | Accepted |
| [011](./adr-011-validation-phase.md) | Validation Phase (Maki v0) | Accepted |
| [012](./adr-012-infrastructure-management.md) | Infrastructure Management (OpenTofu + Terragrunt) | Accepted |
| [013](./adr-013-immune-system.md) | Immune System (maki-immune) | Accepted |

## Build Phases

**Phase 1:** NUC only. Full brain. Get Maki thinking and remembering.
Dev happens on existing dev machine (microk8s). Deploy to NUC when validated.

**Phase 2:** Hetzner + Jonsbo join simultaneously. Full HA. 3-node maki-nerve quorum. maki-vault replication with Patroni. True geographic redundancy.

**Phase 3+:** Senses. IoT/Home Assistant, markets, edge devices. Maki grows.
