resource "kubernetes_service" "graph" {
  metadata {
    name      = "maki-graph"
    namespace = var.namespace
    labels = {
      app = "maki-graph"
    }
  }
  spec {
    port {
      name        = "bolt"
      port        = 7687
      target_port = 7687
    }
    port {
      name        = "http"
      port        = 7474
      target_port = 7474
    }
    selector = {
      app = "maki-graph"
    }
  }
}

resource "kubernetes_stateful_set" "graph" {
  metadata {
    name      = "maki-graph"
    namespace = var.namespace
    labels = {
      app = "maki-graph"
    }
  }
  spec {
    service_name = "maki-graph"
    replicas     = 1
    selector {
      match_labels = {
        app = "maki-graph"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-graph"
        }
      }
      spec {
        container {
          name  = "neo4j"
          image = "neo4j:5-community"
          port {
            container_port = 7687
            name           = "bolt"
          }
          port {
            container_port = 7474
            name           = "http"
          }
          env {
            name = "NEO4J_AUTH"
            value_from {
              secret_key_ref {
                name = "maki-graph-secret"
                key  = "password"
              }
            }
          }
          env {
            name  = "NEO4J_PLUGINS"
            value = "[\"apoc\"]"
          }
          env {
            name  = "NEO4J_apoc_export_file_enabled"
            value = "true"
          }
          env {
            name  = "NEO4J_apoc_import_file_enabled"
            value = "true"
          }
          env {
            name  = "NEO4J_apoc_import_file_use__neo4j__config"
            value = "true"
          }
          env {
            name  = "NEO4J_server_memory_heap_initial__size"
            value = "256m"
          }
          env {
            name  = "NEO4J_server_memory_heap_max__size"
            value = "512m"
          }
          env {
            name  = "NEO4J_server_memory_pagecache_size"
            value = "256m"
          }
          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
          readiness_probe {
            http_get {
              path = "/"
              port = 7474
            }
            initial_delay_seconds = 15
            period_seconds        = 10
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
        name = "data"
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
