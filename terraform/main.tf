terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ──────────────────────────────────────────────
# S3 — config bucket (one bucket, one object per project)
# ──────────────────────────────────────────────

resource "aws_s3_bucket" "config" {
  bucket = var.config_bucket_name
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket = aws_s3_bucket.config.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "eol_config" {
  for_each     = var.projects
  bucket       = aws_s3_bucket.config.id
  key          = "projects/${each.key}/eol_config.json"
  source       = "${path.module}/../${each.value.config_path}"
  etag         = filemd5("${path.module}/../${each.value.config_path}")
  content_type = "application/json"
}

# ──────────────────────────────────────────────
# SNS — one topic + email subscription per project so alerts route
# to the right team.
# ──────────────────────────────────────────────

resource "aws_sns_topic" "eol_alerts" {
  for_each = var.projects
  name     = "${var.project_name}-${each.key}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = var.projects
  topic_arn = aws_sns_topic.eol_alerts[each.key].arn
  protocol  = "email"
  endpoint  = each.value.notification_email
}

# ──────────────────────────────────────────────
# IAM — single Lambda execution role; resource lists are derived from
# the projects map so every per-project S3 key and SNS topic is covered.
# ──────────────────────────────────────────────

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/${var.project_name}:*"
      },
      {
        Sid      = "S3ReadConfig"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = [for o in aws_s3_object.eol_config : "${aws_s3_bucket.config.arn}/${o.key}"]
      },
      {
        Sid      = "SNSPublish"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [for t in aws_sns_topic.eol_alerts : t.arn]
      },
      {
        Sid      = "SESSendEmail"
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = "*"
      },
    ]
  })
}

# ──────────────────────────────────────────────
# Lambda function (single, multi-tenant)
#
# Per-project routing values arrive via the EventBridge invocation event,
# not env vars — the env vars below are shared across every project.
#
# The deployment artifact is built OUTSIDE Terraform by
# build_lambda_package.py from a strict allowlist (lambda_function.py +
# eoltracker/**.py), so untracked secrets or unrelated repository files can
# never enter the deployed ZIP (audit finding S-01). Configs are loaded from
# S3 at runtime, not the zip.
#
# Rebuild before deploying whenever runtime code changes:
#
#   python build_lambda_package.py build
#
# The lifecycle preconditions below fail plan/apply unless the on-disk ZIP is
# byte-for-byte the one recorded by the manifest AND that manifest reflects
# the currently checked-out runtime sources. See docs/packaging.md.
# ──────────────────────────────────────────────

locals {
  package_zip_path      = "${path.module}/build/lambda.zip"
  package_manifest_path = "${path.module}/build/manifest.json"

  package_manifest        = try(jsondecode(file(local.package_manifest_path)), null)
  package_manifest_schema = try(local.package_manifest.schema, 0)
  package_manifest_inputs = try(local.package_manifest.inputs, {})
  package_manifest_sha256 = try(local.package_manifest.artifact.sha256, "")

  # Allowlist expectations derived from the working tree itself: the shim plus
  # every *.py under eoltracker/. Must stay in lockstep with the allowlist in
  # build_lambda_package.py; a drift between the two fails plan loudly here.
  expected_runtime_files = sort(concat(
    ["lambda_function.py"],
    formatlist("eoltracker/%s", [
      for f in fileset("${path.module}/../eoltracker", "**/*.py") : f
      if length(regexall("(^|/)__pycache__/", f)) == 0
    ]),
  ))
}

resource "aws_lambda_function" "eol_checker" {
  function_name    = var.project_name
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory
  filename         = local.package_zip_path
  source_code_hash = try(filebase64sha256(local.package_zip_path), "")

  lifecycle {
    precondition {
      condition     = local.package_manifest != null
      error_message = "Missing ${local.package_manifest_path}: run 'python build_lambda_package.py build' from the repository root before applying Terraform."
    }

    precondition {
      condition     = local.package_manifest_schema == 1
      error_message = "Unrecognized manifest schema in ${local.package_manifest_path}: rebuild the artifact with 'python build_lambda_package.py build'."
    }

    precondition {
      condition     = sort(keys(local.package_manifest_inputs)) == local.expected_runtime_files
      error_message = "Runtime file set differs from the built manifest: sources were added or removed since 'python build_lambda_package.py build' last ran. Rebuild the artifact."
    }

    precondition {
      condition = alltrue([
        for rel in local.expected_runtime_files :
        try(filesha256("${path.module}/../${rel}") == local.package_manifest_inputs[rel], false)
      ])
      error_message = "Runtime source contents changed since ${local.package_manifest_path} was written: rebuild the artifact with 'python build_lambda_package.py build'."
    }

    precondition {
      condition     = fileexists(local.package_zip_path) && try(filesha256(local.package_zip_path) == local.package_manifest_sha256, false)
      error_message = "${local.package_zip_path} is missing or does not match the verified manifest: rebuild with 'python build_lambda_package.py build' (or run 'python build_lambda_package.py verify' to diagnose)."
    }
  }

  environment {
    variables = {
      CONFIG_BUCKET  = aws_s3_bucket.config.id
      SES_FROM_EMAIL = var.ses_from_email
    }
  }
}

# ──────────────────────────────────────────────
# CloudWatch Events — one schedule per project, each carrying the
# project's config key and SNS topic in its input payload.
# ──────────────────────────────────────────────

resource "aws_cloudwatch_event_rule" "daily" {
  for_each            = var.projects
  name                = "${var.project_name}-${each.key}-daily"
  description         = "Trigger EOL checker for project ${each.key}"
  schedule_expression = each.value.schedule_expression
}

resource "aws_cloudwatch_event_target" "lambda" {
  for_each  = var.projects
  rule      = aws_cloudwatch_event_rule.daily[each.key].name
  target_id = "${var.project_name}-${each.key}"
  arn       = aws_lambda_function.eol_checker.arn

  input = jsonencode({
    project       = each.key
    config_key    = aws_s3_object.eol_config[each.key].key
    sns_topic_arn = aws_sns_topic.eol_alerts[each.key].arn
    ses_to_emails = each.value.ses_to_emails
  })
}

resource "aws_lambda_permission" "cloudwatch" {
  for_each      = var.projects
  statement_id  = "AllowCloudWatchInvoke-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.eol_checker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily[each.key].arn
}
