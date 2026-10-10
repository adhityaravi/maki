resource "kubernetes_persistent_volume_claim" "recall_data" {
  metadata {
    name      = "maki-recall-data"
    namespace = var.namespace
  }
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = {
        storage = "1Gi"
      }
    }
  }
  # Don't block apply on PVC binding (no PVs available until a pod claims it).
  wait_until_bound = false
}

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
  lifecycle {
    ignore_changes = [
      spec[0].template[0].spec[0].container[0].image,
    ]
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
        # PVC mounted at /data needs fsGroup so the non-root container can write.
        security_context {
          fs_group = 1000
        }
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
          # Three-probe split per #253 — previously a single readiness_probe on
          # /health meant a slow Mem0 boot left the pod unready but the cluster
          # had no way to distinguish "still warming up" from "wedged forever".
          # See manifests/maki-recall.yaml for the parallel definition that
          # documented this split before terraform caught up (#263).
          #
          #   /live   — process-only; FastAPI loop responsive. Used by
          #             liveness_probe. Never blocks on Mem0/deps.
          #   /health — Mem0 + pgvector + neo4j probes. Used by
          #             readiness_probe (routes traffic only when ready) and
          #             startup_probe (gates liveness until init done).
          startup_probe {
            # 60 × 10s = 10min budget for Mem0 init. Memory.from_config blocks
            # synchronously on pgvector + neo4j + synapse; any one being slow
            # extends boot. Once /health passes once, liveness_probe takes over.
            http_get {
              path = "/health"
              port = 8000
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            timeout_seconds       = 5
            failure_threshold     = 60
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
          liveness_probe {
            # Process-only probe. Restarts the pod only when the FastAPI event
            # loop itself stops responding — independent of Mem0 init state or
            # backend dep health.
            http_get {
              path = "/live"
              port = 8000
            }
            initial_delay_seconds = 0
            period_seconds        = 30
            timeout_seconds       = 5
            failure_threshold     = 3
          }
          resources {
            # Bumped from 256Mi/512Mi after #251: mem0 fact-extraction runs
            # the LLM add() path synchronously and accumulates messages +
            # extracted facts in-process; at 512Mi a long conversation can
            # OOM the pod mid-extract and CrashLoopBackOff. 512Mi/1Gi gives
            # headroom for the in-flight extract without making recall a
            # noticeably heavy tenant. Revisit if we move fact-extraction
            # off the request thread.
            requests = {
              memory = "512Mi"
              cpu    = "100m"
            }
            limits = {
              memory = "1Gi"
              cpu    = "500m"
            }
          }
        }
        volume {
          name = "data"
          # PVC per #176 — emptyDir lost mem0's history.db on every pod
          # restart, which silently regenerated user/run history from scratch.
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.recall_data.metadata[0].name
          }
        }
      }
    }
  }
}
