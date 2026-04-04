locals {
  machine          = "ramenslurp"
  kube_context     = "ramen"
  resource_profile = "primary"
  storage_class    = "csi-rawfile-default"
  vault_storage_size = "50Gi"

  # NATS cluster — routes to peer nodes
  nats_url         = "nats://maki-nerve-nats:4222"
  nats_server_name = "nerve-ramen"
  nats_cluster_routes = [
    "nats-route://maki-nerve-sushi.xantu-city.ts.net:6222",
    "nats-route://maki-nerve-inu.xantu-city.ts.net:6222",
  ]

  # PostgreSQL — local first, then peers for leader failover
  postgres_host = "maki-vault,maki-vault-sushi.xantu-city.ts.net,maki-vault-inu.xantu-city.ts.net"

  # Neo4j over tailnet (lives on sushitrash, accessed remotely)
  neo4j_uri    = "bolt://maki-graph-sushi.xantu-city.ts.net:7687"
  enable_graph = false

  ears_replicas = 1

  # Claude model
  claude_model = "claude-sonnet-4-6"

  # Image registry
  image_registry = "ghcr.io/adhityaravi"

  # Patroni — 3-node Raft cluster
  patroni_name         = "ramen"
  patroni_connect_host = "maki-vault-ramen.xantu-city.ts.net"
  raft_self_addr       = "maki-vault-ramen.xantu-city.ts.net:2222"
  raft_partner_addrs = [
    "maki-vault-sushi.xantu-city.ts.net:2222",
    "maki-vault-inu.xantu-city.ts.net:2222",
  ]
}
