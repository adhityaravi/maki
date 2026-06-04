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
  lifecycle {
    ignore_changes = [
      spec[0].template[0].spec[0].container[0].image,
    ]
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
        volume {
          name = "maki-loops"
          empty_dir {}
        }
        init_container {
          name  = "install-loops"
          image = "python:3.12-slim"
          command = [
            "/bin/sh",
            "-c",
            <<-EOT
              set -e
              apt-get update && apt-get install -y git
              pip install --no-deps --target=/maki-loops "git+https://$GITHUB_PAT@github.com/adhityaravi/maki-loops.git"
              pip install --target=/maki-loops pandas tenacity
              echo "maki-loops installed successfully"
            EOT
          ]
          env {
            name = "GITHUB_PAT"
            value_from {
              secret_key_ref {
                name = "maki-github-pat"
                key  = "token"
              }
            }
          }
          volume_mount {
            name       = "maki-loops"
            mount_path = "/maki-loops"
          }
        }
        container {
          name              = "stem"
          image             = "${var.image_registry}/maki-stem:latest"
          image_pull_policy = "Always"
          port {
            container_port = 8000
          }
          volume_mount {
            name       = "github-key"
            mount_path = "/etc/maki-github"
            read_only  = true
          }
          volume_mount {
            name       = "maki-loops"
            mount_path = "/maki-loops"
            read_only  = true
          }
          env {
            name  = "PYTHONPATH"
            value = "/maki-loops"
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
            name = "TWELVE_DATA_API_KEY"
            value_from {
              secret_key_ref {
                name = "maki-trading-keys"
                key  = "twelve-data-api-key"
              }
            }
          }
          env {
            name = "FINNHUB_API_TOKEN"
            value_from {
              secret_key_ref {
                name = "maki-trading-keys"
                key  = "finnhub-api-key"
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
            name  = "POSTGRES_HOST"
            value = var.postgres_host
          }
          env {
            name  = "POSTGRES_PORT"
            value = "5432"
          }
          env {
            name  = "POSTGRES_DB"
            value = "maki"
          }
          env {
            name  = "POSTGRES_USER"
            value = "maki"
          }
          env {
            name = "POSTGRES_PASSWORD"
            value_from {
              secret_key_ref {
                name = "maki-vault-secret"
                key  = "password"
              }
            }
          }
          env {
            # Pod-local TZ drives croniter evaluation; Europe/Berlin is DST-safe
            # so the trading cron (30 7,22 * * 1-5) lands at TR open and US close
            # regardless of season. Appended to the end of the env list so the
            # tofu plan shows only an add, not a shift of every subsequent env.
            name  = "TZ"
            value = "Europe/Berlin"
          }
          env {
            # Trade proposal cards wait 3h for Adi's decision before timing out.
            # Default in code is 60 min — too short for the 07:30 CET morning
            # card to survive Adi's morning routine.
            name  = "TRADE_PROPOSAL_TIMEOUT"
            value = "10800"
          }
          startup_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            initial_delay_seconds = 10
            period_seconds        = 5
            failure_threshold     = 12 # 10 + 12*5 = 70s max startup
            timeout_seconds       = 3
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            period_seconds  = 10
            timeout_seconds = 5
          }
          # Liveness points at /live (process-only, per #276) so a transient
          # NATS reconnect or stuck turn — both of which legitimately flip
          # /health to 503 — cannot cause kubelet to SIGKILL the pod. The
          # startup_probe above already gates "is it up yet"; this probe is
          # the catch for "is the event loop wedged after the fact".
          liveness_probe {
            http_get {
              path = "/live"
              port = 8000
            }
            initial_delay_seconds = 30
            period_seconds        = 30
            timeout_seconds       = 3
            failure_threshold     = 3
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
