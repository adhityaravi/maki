variable "namespace" {
  type    = string
  default = "maki"
}

variable "image_registry" {
  type    = string
  default = "ghcr.io/adhityaravi"
}

variable "claude_model" {
  type    = string
  default = "claude-sonnet-4-6"
}
