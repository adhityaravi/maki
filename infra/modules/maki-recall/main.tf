resource "kubernetes_service" "recall" {
  metadata {
    name      = "maki-recall"
    namespace = var.namespace
    labels = {
      app = "maki-recall"
    }
  }
  spec {
    port {
      port        = 8000
      target_port = 8000
    }
    selector = {
      app = "maki-recall"
    }
  }
}

resource "kubernetes_deployment" "recall" {
  metadata {
    name      = "maki-recall"
    namespace = var.namespace
    labels = {
      app = "maki-recall"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "maki-recall"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-recall"
        }
      }
      spec {
        container {
          name  = "mem0"
          image = "${var.image_registry}/maki-recall:latest"
          image_pull_policy = "Always"
          port {
            container_port = 8000
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
            name  = "NEO4J_URI"
            value = var.neo4j_uri
          }
          env {
            name  = "NEO4J_USERNAME"
            value = "neo4j"
          }
          env {
            name = "NEO4J_PASSWORD"
            value_from {
              secret_key_ref {
                name = "maki-graph-auth"
                key  = "password"
              }
            }
          }
          env {
            name  = "OLLAMA_URL"
            value = var.ollama_url
          }
          env {
            name  = "LLM_PROVIDER"
            value = "openai"
          }
          env {
            name  = "LLM_URL"
            value = var.synapse_url
          }
          env {
            name  = "LLM_MODEL"
            value = var.llm_model
          }
          env {
            name  = "EMBEDDER_MODEL"
            value = "nomic-embed-text"
          }
          env {
            name  = "EMBEDDING_DIMS"
            value = "768"
          }
          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            initial_delay_seconds = 10
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
        volume {
          name = "data"
          empty_dir {}
        }
      }
    }
  }
}
