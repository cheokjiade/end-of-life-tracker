variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "eu-west-1"
}

variable "project_name" {
  description = "Name prefix for shared resources (Lambda, IAM, S3 bucket)"
  type        = string
  default     = "eol-checker"
}

variable "config_bucket_name" {
  description = "S3 bucket name for the EOL config files (must be globally unique)"
  type        = string
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 60
}

variable "lambda_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 128
}

variable "eol_max_workers" {
  description = "Maximum concurrent provider checks per Lambda invocation"
  type        = number
  default     = 4

  validation {
    condition     = var.eol_max_workers >= 1 && var.eol_max_workers <= 32
    error_message = "eol_max_workers must be between 1 and 32."
  }
}

variable "eol_time_reserve_ms" {
  description = "Lambda time reserved for rendering and notification delivery"
  type        = number
  default     = 15000

  validation {
    condition     = var.eol_time_reserve_ms >= 1000
    error_message = "eol_time_reserve_ms must be at least 1000."
  }
}

variable "eol_check_start_guard_ms" {
  description = "Extra time, above the reserve, required to start a provider check"
  type        = number
  default     = 18000

  validation {
    condition     = var.eol_check_start_guard_ms >= 15000
    error_message = "eol_check_start_guard_ms must be at least the longest built-in provider timeout (15000 ms)."
  }
}

variable "ses_from_email" {
  description = "SES sender (must be verified in SES). Shared across all projects. Leave empty to skip SES."
  type        = string
  default     = ""
}

variable "ops_notification_email" {
  description = "Email subscribed to operational alarms (Lambda failures, dead-letter queue). Leave empty to create the ops topic without a subscription."
  type        = string
  default     = ""
}

# ──────────────────────────────────────────────
# Per-project settings
#
# One Lambda services every project; each entry below produces its own
# S3 config object, SNS topic + email subscription, and EventBridge
# schedule. The schedule's input payload tells the Lambda which config
# to load and which topic to publish to.
# ──────────────────────────────────────────────

variable "projects" {
  description = "Map of project name to per-project EOL checker settings"
  type = map(object({
    config_path         = string                                # local file relative to repo root
    notification_email  = string                                # SNS subscriber
    schedule_expression = optional(string, "cron(0 8 * * ? *)") # daily 8:00 UTC default
    ses_to_emails       = optional(string, "")                  # comma-separated; empty = skip SES
  }))
}
