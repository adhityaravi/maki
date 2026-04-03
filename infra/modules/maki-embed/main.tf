resource "kubernetes_service" "embed" {
  metadata {
    name      = "maki-embed"
    namespace = var.namespace
    labels = {
      app = "maki-embed"
    }
  }
  spec {
    port {
      port        = 11434
      target_port = 11434
    }
    selector = {
      app = "maki-embed"
    }
  }
}

resource "kubernetes_stateful_set" "embed" {
  metadata {
    name      = "maki-embed"
    namespace = var.namespace
    labels = {
      app = "maki-embed"
    }
  }
  spec {
    service_name = "maki-embed"
    replicas     = 1
    selector {
      match_labels = {
        app = "maki-embed"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-embed"
        }
      }
      spec {
        container {
          name  = "ollama"
          image = "ollama/ollama:latest"
          port {
            container_port = 11434
          }
          env {
            name  = "OLLAMA_HOST"
            value = "0.0.0.0"
          }
          volume_mount {
            name       = "models"
            mount_path = "/root/.ollama"
          }
          readiness_probe {
            http_get {
              path = "/"
              port = 11434
            }
            initial_delay_seconds = 5
            period_seconds        = 5
          }
          resources {
            requests = {
              memory = var.resources.requests.memory
              cpu    = var.resources.requests.cpu
            }
            limits = {
              memory = var.resources.limits.memory
              cpu    = var.resources.limits.cpu
            }
          }
        }
      }
    }
    volume_claim_template {
      metadata {
        name = "models"
      }
      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = var.storage_class
        resources {
          requests = {
            storage = var.storage_size
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      spec[0].volume_claim_template,
    ]
  }
}
