variable "namespace" {
  type    = string
  default = "maki"
}

variable "storage_class" {
  type = string
}

variable "storage_size" {
  type    = string
  default = "5Gi"
}

variable "image_registry" {
  type = string
}

variable "patroni_scope" {
  type    = string
  default = "maki-vault"
}

variable "patroni_name" {
  type        = string
  description = "Unique Patroni node name per site (e.g. kuma, sushi)"
}

variable "raft_self_addr" {
  type        = string
  default     = ""
  description = "This node's Tailscale hostname:2222 for Raft. Empty = standalone."
}

variable "raft_partner_addrs" {
  type        = list(string)
  default     = []
  description = "List of partner hostname:2222 for Raft. Empty = standalone."
}

variable "resources" {
  type = object({
    requests = object({
      memory = string
      cpu    = string
    })
    limits = object({
      memory = string
      cpu    = string
    })
  })
  default = {
    requests = {
      memory = "256Mi"
      cpu    = "100m"
    }
    limits = {
      memory = "1Gi"
      cpu    = "500m"
    }
  }
}
