include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "../../../modules/maki-stem"
}

dependency "namespace" {
  config_path = "../namespace"
  mock_outputs = {
    name = "maki"
  }
}

dependencies {
  paths = ["../secrets", "../maki-nerve", "../maki-recall"]
}

inputs = {
  namespace      = dependency.namespace.outputs.name
  image_registry = local.env.locals.image_registry
  nats_url       = local.env.locals.nats_url
}
