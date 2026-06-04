resource "kubernetes_service" "cortex" {
  metadata {
    name      = "maki-cortex"
    namespace = var.namespace
    labels = {
      app = "maki-cortex"
    }
  }
  spec {
    port {
      port        = 8080
      target_port = 8080
    }
    selector = {
      app = "maki-cortex"
    }
  }
}

resource "kubernetes_deployment" "cortex" {
  metadata {
    name      = "maki-cortex"
    namespace = var.namespace
    labels = {
      app = "maki-cortex"
    }
  }
  lifecycle {
    ignore_changes = [
      spec[0].template[0].spec[0].container[0].image,
    ]
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "maki-cortex"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-cortex"
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
        volume {
          name = "repo"
          empty_dir {}
        }
        container {
          name  = "cortex"
          image = "${var.image_registry}/maki-cortex:latest"
          image_pull_policy = "Always"
          port {
            container_port = 8080
          }
          volume_mount {
            name       = "github-key"
            mount_path = "/etc/maki-github"
            read_only  = true
          }
          volume_mount {
            name       = "repo"
            mount_path = "/repo"
          }
          env {
            name  = "CLAUDE_MODEL"
            value = var.claude_model
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
            name = "CLAUDE_CODE_OAUTH_TOKEN"
            value_from {
              secret_key_ref {
                name = "maki-claude-auth"
                key  = "token"
              }
            }
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
          env {
            name  = "REPO_PATH"
            value = "/repo/maki"
          }
          env {
            name  = "CORTEX_MAX_TURNS"
            value = "50"
          }
          env {
            name  = "CORTEX_WORK_MAX_TURNS"
            value = "100"
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8080
            }
            initial_delay_seconds = 5
            period_seconds        = 10
            timeout_seconds       = 5
          }
          # Liveness points at /live (process-only) per #276. The TCP health
          # server in maki-common now routes /live to an always-200 response
          # regardless of the registered check, so a NATS reconnect or a
          # wedged turn flipping /health red won't trigger a kubelet kill
          # before _health_check's CORTEX_LIVENESS_TURN_MULTIPLIER escape
          # hatch (#185) decides on its own that the pod is unrecoverable.
          liveness_probe {
            http_get {
              path = "/live"
              port = 8080
            }
            initial_delay_seconds = 30
            period_seconds        = 30
            timeout_seconds       = 3
            failure_threshold     = 3
          }
          resources {
            requests = {
              memory = "256Mi"
              cpu    = "100m"
            }
            limits = {
              memory = "512Mi"
              cpu    = "500m"
            }
          }
        }
      }
    }
  }
}
