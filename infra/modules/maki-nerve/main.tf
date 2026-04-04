resource "helm_release" "nats" {
  name       = "maki-nerve"
  namespace  = var.namespace
  repository = "https://nats-io.github.io/k8s/helm/charts/"
  chart      = "nats"
  version    = "2.12.6"

  values = [templatefile("${path.module}/values.yaml.tpl", {
    storage_class          = var.storage_class
    jetstream_storage_size = var.jetstream_storage_size
    nats_cluster_routes    = var.nats_cluster_routes
    nats_server_name       = var.nats_server_name
  })]
}
