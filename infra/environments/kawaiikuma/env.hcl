locals {
  machine          = "kawaiikuma"
  kube_context     = "kuma"
  resource_profile = "dev"
  storage_class    = "microk8s-hostpath"

  # Single-node dev — local NATS only
  nats_url            = "nats://maki-nerve-nats:4222"
  nats_cluster_routes = []

  # Local PostgreSQL
  postgres_host = "maki-vault"

  # Local Neo4j
  neo4j_uri    = "bolt://maki-graph:7687"
  enable_graph = true

  # Dev runs ears (single leader)
  ears_replicas = 1

  # Claude model
  claude_model = "claude-sonnet-4-6"

  # Image registry
  image_registry = "ghcr.io/adhityaravi"

  # Patroni
  patroni_name       = "kuma"
  raft_self_addr     = ""
  raft_partner_addrs = []
}
