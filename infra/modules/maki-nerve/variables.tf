variable "namespace" {
  type    = string
  default = "maki"
}

variable "storage_class" {
  type = string
}

variable "jetstream_storage_size" {
  type    = string
  default = "2Gi"
}

variable "nats_server_name" {
  description = "Unique NATS server name for cross-cluster clustering"
  type        = string
  default     = ""
}

variable "nats_cluster_routes" {
  description = "List of NATS cluster route URLs for cross-cluster peering"
  type        = list(string)
  default     = []
}
