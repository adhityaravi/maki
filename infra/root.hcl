locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

remote_state {
  backend = "s3"
  config = {
    bucket  = "maki-state"
    key     = "${path_relative_to_include()}/terraform.tfstate"
    region  = "eu-central-1"
    encrypt = true

    # No DynamoDB locking — single operator per cluster
    skip_bucket_enforced_tls           = true
    skip_bucket_root_access            = true
    skip_bucket_public_access_blocking = true
    skip_bucket_ssencryption           = true
    skip_metadata_api_check            = true
  }
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
}

generate "providers" {
  path      = "providers.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    terraform {
      required_providers {
        kubernetes = {
          source  = "hashicorp/kubernetes"
          version = "~> 2.35"
        }
        helm = {
          source  = "hashicorp/helm"
          version = "~> 2.17"
        }
        sops = {
          source  = "carlpett/sops"
          version = "~> 1.1"
        }
      }
    }

    provider "kubernetes" {
      config_path    = "~/.kube/config"
      config_context = "${local.env.locals.kube_context}"
    }

    provider "helm" {
      kubernetes {
        config_path    = "~/.kube/config"
        config_context = "${local.env.locals.kube_context}"
      }
    }
  EOF
}
