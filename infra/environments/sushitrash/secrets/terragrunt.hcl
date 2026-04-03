include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "../../../modules/secrets"
}

dependency "namespace" {
  config_path = "../namespace"
  mock_outputs = {
    name = "maki"
  }
}

inputs = {
  namespace    = dependency.namespace.outputs.name
  secrets_file = "${get_repo_root()}/infra/secrets.enc.yaml"
  enable_graph = local.env.locals.enable_graph
}
