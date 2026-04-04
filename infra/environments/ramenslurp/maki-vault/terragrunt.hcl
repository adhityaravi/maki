include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

terraform {
  source = "../../../modules/maki-vault"
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
  namespace          = dependency.namespace.outputs.name
  storage_class      = local.env.locals.storage_class
  storage_size       = local.env.locals.vault_storage_size
  image_registry     = local.env.locals.image_registry
  patroni_name         = local.env.locals.patroni_name
  patroni_connect_host = local.env.locals.patroni_connect_host
  raft_self_addr       = local.env.locals.raft_self_addr
  raft_partner_addrs   = local.env.locals.raft_partner_addrs
}
