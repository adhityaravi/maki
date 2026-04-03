resource "kubernetes_namespace" "maki" {
  metadata {
    name = "maki"
  }
}
