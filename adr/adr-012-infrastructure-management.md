# ADR-012: Infrastructure Management (OpenTofu + Terragrunt)

**Status:** Accepted
**Date:** 2026-03-27

## Context

Maki needs a repeatable, declarative way to manage its infrastructure across multiple Kubernetes clusters and edge devices. The system must support adding nodes, self-healing, and autonomous infrastructure management by Maki itself.

### Alternatives Considered

**Juju + Charms:** Rejected. Persistent controller is a SPOF (~500MB-1GB RAM per site). No K8s charms exist for maki-nerve, maki-graph, maki-embed, or maki-recall — would require writing 5+ custom charms for zero capability gain. A human-operated orchestration layer between Maki and its infrastructure contradicts the self-management principle.

**Plain Helm + scripts:** Repeatable but no state tracking, no drift detection, no dependency ordering, no self-management story.

**ArgoCD/Flux (GitOps):** Viable but adds a persistent controller (RAM cost) and is focused on K8s-only — doesn't cover edge device provisioning or Tailscale management.

## Decision

### OpenTofu + Terragrunt

**OpenTofu** (open-source Terraform fork) provides declarative infrastructure management with no persistent controller. `tofu plan` and `tofu apply` run, converge state, exit. Zero RAM between runs.

**Terragrunt** provides the DRY layer: shared modules, per-environment overrides, dependency ordering between components, and remote state management.

### Why This Fits Maki

- **No persistent controller.** No SPOF, no RAM cost between runs.
- **Declarative.** The desired state lives in code. Drift is detectable. Recovery is `tofu apply`.
- **Multi-cluster native.** Multiple Kubernetes provider configurations, one per site. Adding a node = adding an environment directory.
- **Maki can run it.** Tofu's declarative model is simple enough for an LLM to reason about. Maki can detect drift, generate plans, and propose fixes.
- **Beyond K8s.** Tailscale provider for edge device enrollment, DNS providers, cloud providers — all in the same tool.

### Repository Structure

```
maki/
├── infra/
│   ├── terragrunt.hcl                     root config (provider versions, state backend)
│   │
│   ├── modules/                            reusable Terraform modules
│   │   ├── namespace/                      K8s namespace + base RBAC
│   │   ├── maki-nerve/                     NATS helm release + JetStream config
│   │   ├── maki-vault/                     Postgres helm release + pgvector
│   │   ├── maki-graph/                     Neo4j CE helm release
│   │   ├── maki-embed/                     Ollama deployment + model pull job
│   │   ├── maki-recall/                    Mem0 deployment + connections
│   │   ├── maki-synapse/                   LLM proxy for Mem0
│   │   ├── maki-cortex/                    cortex deployment
│   │   ├── maki-stem/                      brainstem deployment
│   │   ├── maki-ears/                      Discord bot deployment
│   │   ├── maki-gate/                      gatekeeper deployment + RBAC
│   │   ├── maki-immune/                    immune system deployment
│   │   ├── maki-mirror/                    dashboard deployment
│   │   └── maki-edge/                      edge device provisioning
│   │
│   └── environments/                       per-site configurations
│       ├── dev/                            microk8s dev machine
│       │   ├── terragrunt.hcl
│       │   ├── maki-nerve/
│       │   │   └── terragrunt.hcl
│       │   ├── maki-vault/
│       │   │   └── terragrunt.hcl
│       │   └── ...
│       ├── nuc/                            production primary brain
│       │   └── ...
│       ├── hetzner/                        geographic peer
│       │   └── ...
│       └── jonsbo/                         quorum services only
│           ├── maki-nerve/                 quorum voter
│           ├── maki-vault/                 Patroni witness
│           └── ...
```

### Module Dependencies (Terragrunt)

```
namespace
  └── maki-nerve
  └── maki-vault
  └── maki-graph
  └── maki-embed
        └── maki-recall (depends on: maki-vault, maki-embed, maki-graph, maki-synapse)
              └── maki-stem (depends on: maki-nerve, maki-recall)
              └── maki-cortex (depends on: maki-nerve)
                    └── maki-ears (depends on: maki-nerve)
                    └── maki-gate (depends on: maki-nerve)
                    └── maki-immune (depends on: maki-nerve)
                    └── maki-mirror (depends on: maki-nerve)
```

### State Management

**v0 (dev):** Local state file in the repo (`.gitignore`'d, backed up).

**Production:** State stored in a NATS KV bucket (`maki.infra.state`) or S3-compatible backend (MinIO on NUC). Accessible to Maki for self-management.

### Secrets Management

- K8s Secrets created by Terraform's kubernetes provider
- Sensitive values passed as Terragrunt inputs from environment variables or encrypted files (SOPS/age)
- `sensitive = true` on all secret variables — no secrets in state
- OAuth tokens, API keys stored as K8s Secrets, referenced by pods via `secretKeyRef`

### Providers

```hcl
provider "kubernetes" {
  config_path    = "~/.kube/config"
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = "~/.kube/config"
    config_context = var.kube_context
  }
}

provider "tailscale" {
  api_key = var.tailscale_api_key
}
```

### Self-Management by Maki

maki-immune (ADR-013) uses OpenTofu for self-healing:

```
Tier 1 (Autonomous):
  tofu plan — detect drift, read-only, no approval needed
  Report findings to #maki-vitals

Tier 2 (Approve once):
  tofu apply — fix detected drift
  Proposed as a mission, approved via Discord

Tier 3 (Always approve):
  Changes to security policies, RBAC, network policies
  Changes to maki-immune itself
  Adding/removing nodes
```

### Adding a New Node

```
1. Provision machine (physical or VPS)
2. Install Tailscale, join Tailnet
3. Bootstrap Kubernetes
4. Create environment directory: infra/environments/<site>/
5. Configure terragrunt.hcl with site-specific vars
6. Run: cd infra/environments/<site> && terragrunt run-all apply
7. maki-nerve joins cluster, maki-vault starts replicating
8. Maki syncs identity and history, immediately operational
```

Steps 4-6 could be performed by Maki itself as a Tier 2 mission.

### Edge Device Provisioning

Edge devices don't run Kubernetes. Terraform manages their enrollment:

```hcl
# modules/maki-edge/main.tf

resource "tailscale_device_authorization" "edge" {
  device_id  = var.device_id
  authorized = true
}

resource "kubernetes_secret" "edge_nats_creds" {
  # NATS credentials for the edge device
  # Pushed via Tailscale SSH or pulled by edge daemon
}
```

The edge device runs a lightweight daemon (Python/Go) that connects to maki-nerve over Tailscale. No Terraform on the device — just a NATS client.

## Consequences

- No persistent controller consuming RAM — Tofu runs on demand
- Infrastructure is code — repeatable, version-controlled, reviewable
- Multi-cluster deployment is a directory structure, not a complex federation
- Maki can manage its own infrastructure via Tofu (self-healing, extensibility)
- Adding nodes follows the same pattern forever
- Edge devices enrolled via Tailscale provider, no K8s required on device
- State drift is detectable and fixable
