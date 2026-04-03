output "name" {
  value = kubernetes_namespace.maki.metadata[0].name
}
