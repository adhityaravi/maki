variable "namespace" {
  type    = string
  default = "maki"
}

variable "image_registry" {
  type    = string
  default = "ghcr.io/adhityaravi"
}

variable "nats_url" {
  description = "NATS URL (comma-separated for HA)"
  type        = string
  default     = "nats://maki-nerve-nats:4222"
}

variable "claude_model" {
  type    = string
  default = "claude-sonnet-4-6"
}

variable "site_name" {
  description = "Cluster/site name for hive gossip identification"
  type        = string
}
