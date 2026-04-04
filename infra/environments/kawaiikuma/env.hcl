locals {
  machine          = "kawaiikuma"
  kube_context     = "kuma"
  resource_profile = "dev"
  storage_class    = "microk8s-hostpath"

  # NATS cluster — routes to peer nodes
  nats_url         = "nats://maki-nerve-nats:4222"
  nats_server_name = "nerve-kuma"
  nats_cluster_routes = [
    "nats-route://maki-nerve-sushi.xantu-city.ts.net:6222",
    "nats-route://maki-nerve-ramen.xantu-city.ts.net:6222",
    "nats-route://maki-nerve-inu.xantu-city.ts.net:6222",
  ]

  # Local PostgreSQL
  postgres_host = "maki-vault"

  # Neo4j over tailnet (lives on sushitrash)
  neo4j_uri    = "bolt://maki-graph-sushi.xantu-city.ts.net:7687"
  enable_graph = false

  # Dev runs ears (single leader)
  ears_replicas = 1

  # Claude model
  claude_model = "claude-sonnet-4-6"

  # Image registry
  image_registry = "ghcr.io/adhityaravi"

  # Patroni — 3-node Raft cluster
  patroni_name         = "kuma"
  patroni_connect_host = "maki-vault-kuma.xantu-city.ts.net"
  raft_self_addr       = "maki-vault-kuma.xantu-city.ts.net:2222"
  raft_partner_addrs = [
    "maki-vault-sushi.xantu-city.ts.net:2222",
    "maki-vault-ramen.xantu-city.ts.net:2222",
    "maki-vault-inu.xantu-city.ts.net:2222",
  ]
}
