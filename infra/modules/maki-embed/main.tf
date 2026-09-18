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
        # Preload the embed model into the PVC before the main container
        # accepts traffic. Fixes the recall startup race documented in #383:
        # the previous config started an empty ollama, flipped Ready the
        # instant `/` returned 200, and left `nomic-embed-text` to be fetched
        # lazily on the first /api/embeddings call. maki-recall's
        # Memory.from_config() ran against that empty ollama and crash-looped
        # (see 2026-04-05 exit_code=1 restart storm, memory 1a124087).
        #
        # This init container brings up a throwaway ollama serve, waits for
        # the local socket, pulls the model into /root/.ollama (the shared
        # PVC), then exits. Pod Ready is genuinely blocked on the model
        # being on disk.
        init_container {
          name              = "ollama-preload"
          image             = "ollama/ollama:${var.image_tag}"
          image_pull_policy = "IfNotPresent"
          command           = ["/bin/sh", "-c"]
          args = [
            <<-EOT
              set -e
              ollama serve &
              SERVER_PID=$!
              # Wait for the local ollama socket to accept before pulling.
              for i in $(seq 1 30); do
                if ollama list >/dev/null 2>&1; then
                  break
                fi
                sleep 1
              done
              ollama pull ${var.embed_model}
              kill $SERVER_PID 2>/dev/null || true
              wait $SERVER_PID 2>/dev/null || true
            EOT
          ]
          env {
            name  = "OLLAMA_HOST"
            value = "0.0.0.0"
          }
          volume_mount {
            name       = "models"
            mount_path = "/root/.ollama"
          }
        }
        container {
          name              = "ollama"
          image             = "ollama/ollama:${var.image_tag}"
          image_pull_policy = "IfNotPresent"
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
          # Model-aware startup gate. Ollama binds `/` the instant the HTTP
          # listener is up, so an http_get on `/` cannot distinguish
          # "listening" from "actually able to embed". `ollama show <model>`
          # exits 0 only after the server is reachable AND the model exists
          # in the local blob store — exactly what maki-recall needs to be
          # true before it starts embedding.
          startup_probe {
            exec {
              command = ["ollama", "show", var.embed_model]
            }
            initial_delay_seconds = 2
            period_seconds        = 5
            timeout_seconds       = 5
            failure_threshold     = 24 # ~2 min budget for cold server start
          }
          readiness_probe {
            exec {
              command = ["ollama", "show", var.embed_model]
            }
            initial_delay_seconds = 0
            period_seconds        = 10
            timeout_seconds       = 5
            failure_threshold     = 3
          }
          # Liveness probe added per #383 (also covers this pod's slice of
          # the fleet-wide #276). Uses the same model-aware check — if the
          # ollama process wedges or the model disappears from disk, the
          # pod is functionally dead from recall's perspective and should
          # restart, not just be marked NotReady.
          liveness_probe {
            exec {
              command = ["ollama", "show", var.embed_model]
            }
            initial_delay_seconds = 30
            period_seconds        = 30
            timeout_seconds       = 5
            failure_threshold     = 3
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
