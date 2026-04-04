locals {
  machine          = "ikiikiinu"
  kube_context     = "inu"
  resource_profile = "primary"
  storage_class    = "microk8s-hostpath"

  # NATS cluster — routes to peer nodes
  nats_url         = "nats://maki-nerve-nats:4222"
  nats_server_name = "nerve-inu"
  nats_cluster_routes = [
    "nats-route://maki-nerve-sushi.xantu-city.ts.net:6222",
    "nats-route://maki-nerve-ramen.xantu-city.ts.net:6222",
  ]

  # PostgreSQL — local first, then peers for leader failover
  postgres_host = "maki-vault,maki-vault-sushi.xantu-city.ts.net,maki-vault-ramen.xantu-city.ts.net"

  # Neo4j over tailnet (lives on sushitrash, accessed remotely)
  neo4j_uri    = "bolt://maki-graph-sushi.xantu-city.ts.net:7687"
  enable_graph = false

  ears_replicas = 1

  # Claude model
  claude_model = "claude-sonnet-4-6"

  # Image registry
  image_registry = "ghcr.io/adhityaravi"

  # Patroni — join existing Raft cluster
  patroni_name         = "inu"
  patroni_connect_host = "maki-vault-inu.xantu-city.ts.net"
  raft_self_addr       = "maki-vault-inu.xantu-city.ts.net:2222"
  raft_partner_addrs = [
    "maki-vault-sushi.xantu-city.ts.net:2222",
    "maki-vault-ramen.xantu-city.ts.net:2222",
  ]
}
