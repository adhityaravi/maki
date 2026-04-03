variable "namespace" {
  type    = string
  default = "maki"
}

variable "storage_class" {
  type = string
}

variable "storage_size" {
  type    = string
  default = "10Gi"
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
      memory = "512Mi"
      cpu    = "100m"
    }
    limits = {
      memory = "2Gi"
      cpu    = "2"
    }
  }
}
