terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Narrow minor-line constraint; the committed .terraform.lock.hcl pins
      # the exact build. See README.md before changing either.
      version = "~> 5.100.0"
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
    filesha256("${path.module}/../lambda_function.py"),
    sha256(join("", [
      for rel in sort(fileset("${path.module}/../eoltracker", "**/*.py")) :
      filesha256("${path.module}/../eoltracker/${rel}")
    ])),
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
  # every *.py under eoltracker/. Hidden Python files remain in this set so a
  # stale manifest cannot hide them; the builder itself rejects them. The
  # second list mirrors the builder's fail-closed handling of hidden and
  # unexpected non-Python files (apart from documented compiled junk).
  runtime_tree_files = fileset("${path.module}/../eoltracker", "**")
  expected_runtime_files = sort(concat(
    ["lambda_function.py"],
    formatlist("eoltracker/%s", [
      for f in fileset("${path.module}/../eoltracker", "**/*.py") : f
      if length(regexall("(?i)(^|/)__pycache__/", f)) == 0
    ]),
  ))
  unexpected_runtime_files = sort([
    for f in local.runtime_tree_files : f
    if length(regexall("(?i)(^|/)__pycache__/", f)) == 0 &&
    length(regexall("(?i)\\.(pyc|pyo)$", f)) == 0 &&
    (
      length(regexall("(^|/)\\.", f)) > 0 ||
      length(regexall("\\.py$", f)) == 0
    )
  ])
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
      condition     = length(local.unexpected_runtime_files) == 0
      error_message = "Unexpected or hidden files exist under eoltracker/: ${join(", ", local.unexpected_runtime_files)}. Remove them before rebuilding or applying."
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

resource "aws_cloudwatch_metric_alarm" "required_delivery_failures" {
  alarm_name          = "${var.project_name}-required-delivery-failures"
  alarm_description   = "At least one required report channel was undelivered. Another channel may have succeeded, so the Lambda invocation itself may still be successful."
  namespace           = "EOLTracker"
  metric_name         = "RequiredChannelsUndelivered"
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
