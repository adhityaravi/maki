resource "kubernetes_service" "finbert" {
  metadata {
    name      = "maki-finbert"
    namespace = var.namespace
    labels = {
      app = "maki-finbert"
    }
  }
  spec {
    port {
      port        = 8080
      target_port = 8080
    }
    selector = {
      app = "maki-finbert"
    }
  }
}

resource "kubernetes_deployment" "finbert" {
  metadata {
    name      = "maki-finbert"
    namespace = var.namespace
    labels = {
      app = "maki-finbert"
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
        app = "maki-finbert"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-finbert"
        }
      }
      spec {
        container {
          name             = "finbert"
          image            = "${var.image_registry}/maki-finbert:sha-0a5e7ca"
          image_pull_policy = "Always"
          port {
            container_port = 8080
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
          # Liveness points at /live (process-only) per #276. /health 503s
          # while the FinBERT ONNX model is loading (tens of seconds on cold
          # start), so reusing it for liveness would kubelet-kill the pod
          # mid-load. /live answers as soon as the FastAPI event loop is up.
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
              memory = "512Mi"
              cpu    = "100m"
            }
            limits = {
              memory = "1Gi"
              cpu    = "500m"
            }
          }
        }
      }
    }
  }
}
