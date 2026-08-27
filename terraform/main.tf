terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Narrow minor-line constraint; the committed .terraform.lock.hcl pins
      # the exact build. See README.md before changing either.
      version = "~> 5.100.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8.0"
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

# Versioning gives every config object point-in-time recovery. It is the
# rollback mechanism documented in README.md — do not add lifecycle rules
# that expire noncurrent versions of this bucket.
resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "terraform_data" "validate_eol_config" {
  for_each = var.projects

  triggers_replace = [
    filesha256("${path.module}/../${each.value.config_path}"),
  ]

  provisioner "local-exec" {
    command = "python \"${path.module}/../lambda_function.py\" --validate \"${path.module}/../${each.value.config_path}\""
  }
}

resource "aws_s3_object" "eol_config" {
  for_each     = var.projects
  bucket       = aws_s3_bucket.config.id
  key          = "projects/${each.key}/eol_config.json"
  source       = "${path.module}/../${each.value.config_path}"
  etag         = filemd5("${path.module}/../${each.value.config_path}")
  content_type = "application/json"

  depends_on = [
    aws_s3_bucket_versioning.config,
    terraform_data.validate_eol_config,
  ]
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
# ──────────────────────────────────────────────

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/lambda.zip"
  source_dir  = "${path.module}/.."

  # Package the whole runtime: lambda_function.py (shim) + the eoltracker/
  # package. Everything else in the repo root is excluded so the zip carries
  # ONLY runtime code. Configs are loaded from S3 at runtime, not the zip.
  excludes = [
    # directories (whole subtrees)
    ".git", ".claude", "__pycache__", "terraform", "docs", "inputs", "reports",
    "project-*",
    # non-runtime root files
    ".gitignore", "CLAUDE.md", "README.md", "run.sh", "run.ps1",
    "generate_config.py", "eol_config_generation_prompt.md",
    "*_run*.txt",
    # per-project configs + template (runtime loads config from S3, not the zip)
    "eol_config.sample.json", "eol_config.c.json", "eol_config.d.json",
    "eol_config.e.json", "eol_config.a.json",
    "eol_config.b.json", "eol_config.b-auto.json",
    # belt-and-suspenders globs (compiled artifacts / any future configs)
    "eol_config.*.json", "**/__pycache__", "**/*.pyc",
    # keep ONLY: lambda_function.py + eoltracker/**
  ]
}

resource "aws_lambda_function" "eol_checker" {
  function_name    = var.project_name
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

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
