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
      {
        Sid      = "LambdaFailureDLQ"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.lambda_failures.arn
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

  # Asynchronous failure handling (audit R-03): when the handler raises --
  # including the deliberate "all required delivery channels failed" error --
  # Lambda retries and finally drops the event onto this queue so a missed
  # check is visible instead of silently swallowed.
  dead_letter_config {
    target_arn = aws_sqs_queue.lambda_failures.arn
  }

  # AWS validates the execution role's sqs:SendMessage permission when the
  # function DLQ is configured; avoid a create/update race with the policy.
  depends_on = [aws_iam_role_policy.lambda]

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

# ──────────────────────────────────────────────
# Async failure handling + operational alarms (audit R-03)
#
# The function-level DLQ receives events that exhausted their asynchronous
# retries after the handler raised. It is deliberately NOT an EventBridge
# target DLQ: a target DLQ only captures schedule-to-Lambda delivery failures,
# not failures raised inside the function code.
# ──────────────────────────────────────────────

resource "aws_sqs_queue" "lambda_failures" {
  name                      = "${var.project_name}-lambda-failures"
  message_retention_seconds = 14 * 24 * 60 * 60 # 14 days
}

# ──────────────────────────────────────────────
# Operational alarm topic — delivery/health alerts for operators, separate
# from the per-project report topics. Subscribe via ops_notification_email.
# ──────────────────────────────────────────────

resource "aws_sns_topic" "ops_alerts" {
  name = "${var.project_name}-ops-alerts"
}

resource "aws_sns_topic_subscription" "ops_email" {
  count     = var.ops_notification_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "email"
  endpoint  = var.ops_notification_email
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project_name}-function-errors"
  alarm_description   = "The EOL checker invocation failed (runtime error, or every required notification channel failed to deliver and the handler raised). Check CloudWatch logs and the ${var.project_name}-lambda-failures dead-letter queue."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.eol_checker.function_name
  }

  alarm_actions = [aws_sns_topic.ops_alerts.arn]
  ok_actions    = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_failure_dlq_not_empty" {
  alarm_name          = "${var.project_name}-failure-dlq-not-empty"
  alarm_description   = "An EOL-check event exhausted all asynchronous retries and was parked in the ${var.project_name}-lambda-failures dead-letter queue; that scheduled check did not complete."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.lambda_failures.name
  }

  alarm_actions = [aws_sns_topic.ops_alerts.arn]
}
