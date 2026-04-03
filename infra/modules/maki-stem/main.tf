resource "kubernetes_service" "stem" {
  metadata {
    name      = "maki-stem"
    namespace = var.namespace
    labels = {
      app = "maki-stem"
    }
  }
  spec {
    port {
      port        = 8000
      target_port = 8000
    }
    selector = {
      app = "maki-stem"
    }
  }
}

resource "kubernetes_deployment" "stem" {
  metadata {
    name      = "maki-stem"
    namespace = var.namespace
    labels = {
      app = "maki-stem"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "maki-stem"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-stem"
        }
      }
      spec {
        volume {
          name = "github-key"
          secret {
            secret_name = "maki-github-app"
            items {
              key  = "private-key.pem"
              path = "private-key.pem"
            }
          }
        }
        container {
          name  = "stem"
          image = "${var.image_registry}/maki-stem:latest"
          image_pull_policy = "Always"
          port {
            container_port = 8000
          }
          volume_mount {
            name       = "github-key"
            mount_path = "/etc/maki-github"
            read_only  = true
          }
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
            name  = "TURN_TIMEOUT"
            value = "1800"
          }
          env {
            name = "GITHUB_APP_ID"
            value_from {
              secret_key_ref {
                name = "maki-github-app"
                key  = "app-id"
              }
            }
          }
          env {
            name = "GITHUB_INSTALLATION_ID"
            value_from {
              secret_key_ref {
                name = "maki-github-app"
                key  = "installation-id"
              }
            }
          }
          env {
            name  = "GITHUB_PRIVATE_KEY_PATH"
            value = "/etc/maki-github/private-key.pem"
          }
          env {
            name  = "REPO_OWNER"
            value = "adhityaravi"
          }
          env {
            name  = "REPO_NAME"
            value = "maki"
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            initial_delay_seconds = 5
            period_seconds        = 10
            timeout_seconds       = 5
          }
          resources {
            requests = {
              memory = "128Mi"
              cpu    = "100m"
            }
            limits = {
              memory = "256Mi"
              cpu    = "500m"
            }
          }
        }
      }
    }
  }
}
