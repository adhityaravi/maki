locals {
  machine          = "ramenslurp"
  kube_context     = "ramen"
  resource_profile = "primary"
  storage_class    = "csi-rawfile-default"

  # Single-node until Tailscale connects clusters
  nats_url            = "nats://maki-nerve-nats:4222"
  nats_cluster_routes = []

  # Local PostgreSQL
  postgres_host = "maki-vault"

  # Neo4j over tailnet (lives on sushitrash, accessed remotely)
  neo4j_uri    = "bolt://maki-graph-sushi:7687"
  enable_graph = false

  # Ears OFF until NATS quorum established
  ears_replicas = 0

  # Claude model
  claude_model = "claude-sonnet-4-6"

  # Image registry
  image_registry = "ghcr.io/adhityaravi"

  # Patroni
  patroni_name       = "ramen"
  raft_self_addr     = ""
  raft_partner_addrs = []
}
