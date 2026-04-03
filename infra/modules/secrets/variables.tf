variable "namespace" {
  type    = string
  default = "maki"
}

variable "secrets_file" {
  description = "Absolute path to the SOPS-encrypted secrets file"
  type        = string
}

variable "enable_graph" {
  description = "Whether to create graph-related secrets"
  type        = bool
  default     = true
}
