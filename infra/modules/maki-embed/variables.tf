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

# Pinned per #383 — previously `ollama/ollama:latest`, which Renovate can't
# bump and which lets `imagePullPolicy: IfNotPresent` freeze different
# versions on different nodes with no signal. See docs/2026-04-18 immune
# rollback: "always use pinned SHA tags for deployments, never use 'latest'".
# Renovate managed via the customManager block in renovate.json (regex looks
# for the `# renovate:` marker below).
variable "image_tag" {
  description = "ollama/ollama image tag. Renovate updates this via the marker in variables.tf."
  type        = string
  # renovate: datasource=docker depName=ollama/ollama
  default = "0.5.13"
}

# Shared with maki-recall (var.embed_model there defaults to the same value)
# so the two modules can't drift apart. Whatever model this pod pulls MUST
# match the model maki-recall requests through /api/embeddings, or every
# recall embed call 404s.
variable "embed_model" {
  description = "Embedding model preloaded into ollama and required by maki-recall."
  type        = string
  default     = "nomic-embed-text"
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
