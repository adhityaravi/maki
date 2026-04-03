resource "kubernetes_service_account" "immune" {
  metadata {
    name      = "maki-immune"
    namespace = var.namespace
  }
}

resource "kubernetes_role" "immune" {
  metadata {
    name      = "maki-immune-role"
    namespace = var.namespace
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list", "watch", "delete"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get"]
  }
  rule {
    api_groups = [""]
    resources  = ["events"]
    verbs      = ["get", "list", "watch"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list", "patch"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments/scale"]
    verbs      = ["get", "patch"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_role_binding" "immune" {
  metadata {
    name      = "maki-immune-binding"
    namespace = var.namespace
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.immune.metadata[0].name
    namespace = var.namespace
  }
  role_ref {
    kind      = "Role"
    name      = kubernetes_role.immune.metadata[0].name
    api_group = "rbac.authorization.k8s.io"
  }
}

resource "kubernetes_service" "immune" {
  metadata {
    name      = "maki-immune"
    namespace = var.namespace
    labels = {
      app = "maki-immune"
    }
  }
  spec {
    port {
      port        = 8080
      target_port = 8080
    }
    selector = {
      app = "maki-immune"
    }
  }
}

resource "kubernetes_deployment" "immune" {
  metadata {
    name      = "maki-immune"
    namespace = var.namespace
    labels = {
      app = "maki-immune"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "maki-immune"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-immune"
        }
      }
      spec {
        service_account_name = kubernetes_service_account.immune.metadata[0].name
        volume {
          name = "repo"
          empty_dir {}
        }
        container {
          name  = "immune"
          image = "${var.image_registry}/maki-immune:latest"
          image_pull_policy = "Always"
          port {
            container_port = 8080
          }
          volume_mount {
            name       = "repo"
            mount_path = "/repo"
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
            name  = "CLAUDE_MODEL"
            value = var.claude_model
          }
          env {
            name  = "REPO_PATH"
            value = "/repo/maki"
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8080
            }
            initial_delay_seconds = 10
            period_seconds        = 15
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
