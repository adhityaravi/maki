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

variable "replicas" {
  description = "Number of ears replicas (0 on new clusters until NATS quorum)"
  type        = number
  default     = 1
}
