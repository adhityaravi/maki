resource "kubernetes_service" "synapse" {
  metadata {
    name      = "maki-synapse"
    namespace = var.namespace
    labels = {
      app = "maki-synapse"
    }
  }
  spec {
    port {
      port        = 8080
      target_port = 8080
    }
    selector = {
      app = "maki-synapse"
    }
  }
}

resource "kubernetes_deployment" "synapse" {
  metadata {
    name      = "maki-synapse"
    namespace = var.namespace
    labels = {
      app = "maki-synapse"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "maki-synapse"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-synapse"
        }
      }
      spec {
        container {
          name  = "synapse"
          image = "${var.image_registry}/maki-synapse:latest"
          image_pull_policy = "Always"
          port {
            container_port = 8080
          }
          env {
            name  = "CLAUDE_MODEL"
            value = var.claude_model
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
            name  = "MAX_CONCURRENT_QUERIES"
            value = "3"
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
