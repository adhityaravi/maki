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
  default = "claude-haiku-4-5-20251001"
}
