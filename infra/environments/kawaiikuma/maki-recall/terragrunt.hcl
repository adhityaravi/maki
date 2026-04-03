include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "../../../modules/maki-recall"
}

dependency "namespace" {
  config_path = "../namespace"
  mock_outputs = {
    name = "maki"
  }
}

dependencies {
  paths = ["../secrets", "../maki-vault", "../maki-embed", "../maki-synapse", "../maki-graph"]
}

inputs = {
  namespace      = dependency.namespace.outputs.name
  image_registry = local.env.locals.image_registry
  postgres_host  = local.env.locals.postgres_host
  neo4j_uri      = local.env.locals.neo4j_uri
  llm_model      = local.env.locals.claude_model
}
