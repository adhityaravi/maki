data "sops_file" "secrets" {
  source_file = var.secrets_file
}

resource "kubernetes_secret" "claude_auth" {
  metadata {
    name      = "maki-claude-auth"
    namespace = var.namespace
  }
  data = {
    token = data.sops_file.secrets.data["claude_oauth_token"]
  }
}

resource "kubernetes_secret" "discord_auth" {
  metadata {
    name      = "maki-discord-auth"
    namespace = var.namespace
  }
  data = {
    token = data.sops_file.secrets.data["discord_bot_token"]
  }
}

resource "kubernetes_secret" "nats_auth" {
  metadata {
    name      = "maki-nats-auth"
    namespace = var.namespace
  }
  data = {
    token = data.sops_file.secrets.data["nats_token"]
  }
}

resource "kubernetes_secret" "vault_secret" {
  metadata {
    name      = "maki-vault-secret"
    namespace = var.namespace
  }
  data = {
    password              = data.sops_file.secrets.data["vault_password"]
    postgres-password     = data.sops_file.secrets.data["vault_postgres_password"]
    replication-password  = data.sops_file.secrets.data["vault_replication_password"]
  }
}

resource "kubernetes_secret" "graph_auth" {
  count = var.enable_graph ? 1 : 0

  metadata {
    name      = "maki-graph-auth"
    namespace = var.namespace
  }
  data = {
    password = data.sops_file.secrets.data["graph_auth_password"]
  }
}

resource "kubernetes_secret" "graph_secret" {
  count = var.enable_graph ? 1 : 0

  metadata {
    name      = "maki-graph-secret"
    namespace = var.namespace
  }
  data = {
    password = data.sops_file.secrets.data["graph_secret_password"]
  }
}

resource "kubernetes_secret" "github_app" {
  metadata {
    name      = "maki-github-app"
    namespace = var.namespace
  }
  data = {
    app-id          = data.sops_file.secrets.data["github_app_id"]
    installation-id = data.sops_file.secrets.data["github_installation_id"]
    "private-key.pem" = data.sops_file.secrets.data["github_private_key_pem"]
  }
}
