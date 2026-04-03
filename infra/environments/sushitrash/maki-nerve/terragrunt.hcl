include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "../../../modules/maki-nerve"
}

dependency "namespace" {
  config_path = "../namespace"
  mock_outputs = {
    name = "maki"
  }
}

dependencies {
  paths = ["../secrets"]
}

inputs = {
  namespace           = dependency.namespace.outputs.name
  storage_class       = local.env.locals.storage_class
  nats_cluster_routes = local.env.locals.nats_cluster_routes
}
