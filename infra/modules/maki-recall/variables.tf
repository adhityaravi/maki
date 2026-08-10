variable "namespace" {
  type    = string
  default = "maki"
}

variable "image_registry" {
  type    = string
  default = "ghcr.io/adhityaravi"
}

variable "postgres_host" {
  description = "PostgreSQL host (multi-host for HA: host1,host2,host3)"
  type        = string
  default     = "maki-vault"
}

variable "neo4j_uri" {
  description = "Neo4j bolt URI"
  type        = string
  default     = "bolt://maki-graph:7687"
}

variable "ollama_url" {
  type    = string
  default = "http://maki-embed:11434"
}

variable "synapse_url" {
  type    = string
  default = "http://maki-synapse:8080/v1"
}

variable "llm_model" {
  type    = string
  default = "claude-sonnet-4-6"
}

# Kept in sync with infra/modules/maki-embed/variables.tf `embed_model`.
# If maki-embed preloads model X but maki-recall requests model Y, every
# recall embed call 404s. Keep both defaults identical.
variable "embed_model" {
  description = "Embedding model to request from maki-embed. Must match maki-embed's preloaded model."
  type        = string
  default     = "nomic-embed-text"
}
