locals {
  machine          = "sushitrash"
  kube_context     = "sushi"
  resource_profile = "primary"
  storage_class    = "csi-rawfile-default"

  # NATS cluster — routes to peer nodes
  nats_url         = "nats://maki-nerve-nats:4222"
  nats_server_name = "nerve-sushi"
  nats_cluster_routes = [
    "nats-route://maki-nerve-ramen.xantu-city.ts.net:6222",
    "nats-route://maki-nerve-inu.xantu-city.ts.net:6222",
  ]

  # Local PostgreSQL
  postgres_host = "maki-vault"

  # Local Neo4j (primary has graph)
  neo4j_uri    = "bolt://maki-graph:7687"
  enable_graph = true

  ears_replicas = 1

  # Claude model
  claude_model = "claude-sonnet-4-6"

  # Image registry
  image_registry = "ghcr.io/adhityaravi"

  # Patroni — 3-node Raft cluster
  patroni_name         = "sushi"
  patroni_connect_host = "maki-vault-sushi.xantu-city.ts.net"
  raft_self_addr       = "maki-vault-sushi.xantu-city.ts.net:2222"
  raft_partner_addrs = [
    "maki-vault-ramen.xantu-city.ts.net:2222",
    "maki-vault-inu.xantu-city.ts.net:2222",
  ]
}
