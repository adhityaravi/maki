output "service_name" {
  value = "${helm_release.nats.name}-nats"
}

output "nats_url" {
  value = "nats://${helm_release.nats.name}-nats:4222"
}
