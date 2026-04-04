# --- Patroni config ---

resource "kubernetes_config_map" "patroni" {
  metadata {
    name      = "maki-vault-patroni"
    namespace = var.namespace
  }
  data = {
    "patroni.yml" = yamlencode({
      scope = var.patroni_scope
      name  = var.patroni_name

      raft = {
        data_dir      = "/var/lib/raft"
        self_addr     = var.raft_self_addr != "" ? var.raft_self_addr : "127.0.0.1:2222"
        partner_addrs = var.raft_partner_addrs
      }

      restapi = {
        listen          = "0.0.0.0:8008"
        connect_address = "${var.patroni_name}:8008"
      }

      bootstrap = {
        dcs = {
          ttl                     = 30
          loop_wait               = 10
          retry_timeout           = 10
          maximum_lag_on_failover = 1048576
        }
        initdb = [
          { encoding = "UTF8" },
          "data-checksums",
          { "username" = "maki" }
        ]
        pg_hba = [
          "local all all trust",
          "host all all 0.0.0.0/0 md5",
          "host replication replicator 0.0.0.0/0 md5",
        ]
        post_bootstrap = "sh /etc/patroni/post-bootstrap.sh"
      }

      postgresql = {
        listen          = "0.0.0.0:5432"
        connect_address = "${var.patroni_name}:5432"
        data_dir        = "/var/lib/postgresql/data/pgdata"
        pgpass          = "/tmp/pgpass"
        authentication = {
          superuser = {
            username = "maki"
          }
          replication = {
            username = "replicator"
          }
        }
        parameters = {
          max_connections       = 100
          shared_buffers        = "128MB"
          wal_level             = "replica"
          max_wal_senders       = 5
          max_replication_slots = 5
        }
      }
    })

    "post-bootstrap.sh" = <<-EOT
      #!/bin/bash
      set -e
      psql -U maki -d postgres -c "CREATE DATABASE maki OWNER maki;"
      psql -U maki -d maki -c "CREATE EXTENSION IF NOT EXISTS vector;"
      psql -U maki -d maki -c "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '$PATRONI_REPLICATION_PASSWORD';"
    EOT
  }
}

# --- Headless service ---

resource "kubernetes_service" "vault" {
  metadata {
    name      = "maki-vault"
    namespace = var.namespace
    labels = {
      app = "maki-vault"
    }
  }
  spec {
    port {
      name        = "postgres"
      port        = 5432
      target_port = 5432
    }
    port {
      name        = "patroni"
      port        = 8008
      target_port = 8008
    }
    port {
      name        = "raft"
      port        = 2222
      target_port = 2222
    }
    selector = {
      app = "maki-vault"
    }
    cluster_ip = "None"
  }
}

# --- StatefulSet ---

resource "kubernetes_stateful_set" "vault" {
  metadata {
    name      = "maki-vault"
    namespace = var.namespace
    labels = {
      app = "maki-vault"
    }
  }
  spec {
    service_name = "maki-vault"
    replicas     = 1
    selector {
      match_labels = {
        app = "maki-vault"
      }
    }
    template {
      metadata {
        labels = {
          app = "maki-vault"
        }
      }
      spec {
        init_container {
          name    = "fix-permissions"
          image   = "busybox:1.36"
          command = ["sh", "-c", "chown -R 999:999 /var/lib/postgresql/data /var/lib/raft && chmod -R 0700 /var/lib/postgresql/data"]
          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "raft-data"
            mount_path = "/var/lib/raft"
          }
        }
        container {
          name              = "patroni"
          image             = "${var.image_registry}/maki-vault:latest"
          image_pull_policy = "Always"
          command           = ["patroni", "/etc/patroni/patroni.yml"]
          security_context {
            run_as_user = 999
          }

          port {
            name           = "postgres"
            container_port = 5432
          }
          port {
            name           = "patroni"
            container_port = 8008
          }
          port {
            name           = "raft"
            container_port = 2222
          }

          env {
            name = "PATRONI_SUPERUSER_PASSWORD"
            value_from {
              secret_key_ref {
                name = "maki-vault-secret"
                key  = "password"
              }
            }
          }
          env {
            name = "PATRONI_REPLICATION_PASSWORD"
            value_from {
              secret_key_ref {
                name = "maki-vault-secret"
                key  = "replication-password"
              }
            }
          }
          env {
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
          }

          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "patroni-config"
            mount_path = "/etc/patroni"
          }
          volume_mount {
            name       = "raft-data"
            mount_path = "/var/lib/raft"
          }

          readiness_probe {
            http_get {
              path = "/health"
              port = 8008
            }
            initial_delay_seconds = 10
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

        volume {
          name = "patroni-config"
          config_map {
            name         = kubernetes_config_map.patroni.metadata[0].name
            default_mode = "0755"
          }
        }
        volume {
          name = "raft-data"
          empty_dir {}
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
