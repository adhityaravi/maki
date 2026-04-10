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
          image            = "${var.image_registry}/maki-finbert:latest"
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
