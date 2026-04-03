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
      cpu    = "200m"
    }
    limits = {
      memory = "1536Mi"
      cpu    = "1"
    }
  }
}
