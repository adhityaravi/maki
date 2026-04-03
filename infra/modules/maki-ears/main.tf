resource "kubernetes_deployment" "ears" {
  metadata {
    name      = "maki-ears"
    namespace = var.namespace
    labels = {
      app = "maki-ears"
    }
  }
  spec {
    replicas = var.replicas
    selector {
      match_labels = {
        app = "maki-ears"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-ears"
        }
      }
      spec {
        container {
          name  = "ears"
          image = "${var.image_registry}/maki-ears:latest"
          image_pull_policy = "Always"
          env {
            name  = "NATS_URL"
            value = var.nats_url
          }
          env {
            name = "NATS_TOKEN"
            value_from {
              secret_key_ref {
                name = "maki-nats-auth"
                key  = "token"
              }
            }
          }
          env {
            name = "DISCORD_BOT_TOKEN"
            value_from {
              secret_key_ref {
                name = "maki-discord-auth"
                key  = "token"
              }
            }
          }
          resources {
            requests = {
              memory = "64Mi"
              cpu    = "50m"
            }
            limits = {
              memory = "128Mi"
              cpu    = "200m"
            }
          }
        }
      }
    }
  }
}
