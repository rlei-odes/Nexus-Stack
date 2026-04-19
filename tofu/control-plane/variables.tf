# =============================================================================
# Control Plane Variables
# =============================================================================
# Only the variables needed for the Control Plane infrastructure.
# =============================================================================

variable "cloudflare_api_token" {
  description = "Cloudflare API token (set via TF_VAR_cloudflare_api_token)"
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare Account ID (set via TF_VAR_cloudflare_account_id)"
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for your domain"
  type        = string
}

variable "domain" {
  description = "Your domain name (e.g., example.com)"
  type        = string
}

variable "base_domain" {
  description = "Parent domain used as the Resend sender domain. For single-stack installs this is the same as `domain`. For multi-tenant deployments where `domain` is a per-user subdomain (e.g. `user.base.com`), set this to the verified parent (`base.com`) so outgoing emails don't fail with 'domain not verified'. Defaults to an empty string, which falls back to `domain`."
  type        = string
  default     = ""

  validation {
    condition = (
      var.base_domain == "" ||
      can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", var.base_domain))
    )
    error_message = "base_domain must be empty or a valid domain name (e.g., example.com)."
  }
}

variable "subdomain_separator" {
  description = "Separator between service subdomain and base domain. '.' for standard dot-subdomains (default, requires wildcard cert at 3rd level), '-' for flat subdomains used when provisioning tenants under a shared base domain."
  type        = string
  default     = "."
  validation {
    condition     = contains([".", "-"], var.subdomain_separator)
    error_message = "subdomain_separator must be '.' or '-'."
  }
}

variable "admin_email" {
  description = "Admin email for Cloudflare Access (full access including SSH)"
  type        = string
}

variable "user_email" {
  description = "Regular user email for Cloudflare Access (all services except SSH). Optional."
  type        = string
  default     = ""
}

variable "server_type" {
  description = "Hetzner server type (passed to Control Plane for display)"
  type        = string
  default     = "cax31"
}

variable "server_location" {
  description = "Hetzner datacenter location (passed to Control Plane for display)"
  type        = string
  default     = "fsn1"
}

variable "github_owner" {
  description = "GitHub repository owner"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "Nexus-Stack"
}

variable "allow_disable_auto_shutdown" {
  description = "Allow users to disable automatic daily teardown. When false, users can only delay teardown but cannot disable it."
  type        = bool
  default     = false
}

variable "max_extensions_per_day" {
  description = "Maximum number of teardown delay extensions a user can request per UTC day"
  type        = number
  default     = 3
}

variable "max_delay_hours" {
  description = "Maximum hours per teardown delay extension request"
  type        = number
  default     = 4
}

variable "notification_cron" {
  description = "Cron schedule for teardown notification email (UTC). Default: 20:45 UTC (21:45 CET / 22:45 CEST)"
  type        = string
  default     = "45 20 * * *"
}

variable "teardown_cron" {
  description = "Cron schedule for teardown execution (UTC). Default: 21:00 UTC (22:00 CET / 23:00 CEST)"
  type        = string
  default     = "0 21 * * *"
}

# =============================================================================
# Hetzner Cloud (for persistent volumes)
# =============================================================================

variable "hcloud_token" {
  description = "Hetzner Cloud API token (set via TF_VAR_hcloud_token)"
  type        = string
  sensitive   = true
}

variable "persistent_volume_size" {
  description = "Size of the persistent Hetzner Cloud Volume in GB (minimum 10)"
  type        = number
  default     = 10

  validation {
    condition     = var.persistent_volume_size >= 10
    error_message = "Volume size must be at least 10 GB (Hetzner minimum)."
  }
}

# =============================================================================
# Hetzner Object Storage (for LakeFS)
# =============================================================================
# Server and region are not secrets - stored here with sensible defaults.
# Access key and secret key are secrets - stored in GitHub Secrets.

variable "hetzner_object_storage_server" {
  description = "Hetzner Object Storage S3 endpoint (e.g., fsn1.your-objectstorage.com)"
  type        = string
  default     = "fsn1.your-objectstorage.com"
}

variable "hetzner_object_storage_region" {
  description = "Hetzner Object Storage region (e.g., fsn1, nbg1, hel1)"
  type        = string
  default     = "fsn1"
}

variable "hetzner_object_storage_access_key" {
  description = "Hetzner Object Storage access key (set via TF_VAR_hetzner_object_storage_access_key)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "hetzner_object_storage_secret_key" {
  description = "Hetzner Object Storage secret key (set via TF_VAR_hetzner_object_storage_secret_key)"
  type        = string
  default     = ""
  sensitive   = true
}
