"""Static checks for Lambda delivery-failure infrastructure (issue #7)."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "terraform", "main.tf"), encoding="utf-8") as f:
    main = f.read()
with open(os.path.join(ROOT, "terraform", "outputs.tf"), encoding="utf-8") as f:
    outputs = f.read()

assert 'dead_letter_config {' in main
assert 'target_arn = aws_sqs_queue.lambda_failures.arn' in main
assert 'Sid      = "LambdaFailureDLQ"' in main
assert 'Action   = ["sqs:SendMessage"]' in main
assert 'Resource = aws_sqs_queue.lambda_failures.arn' in main
assert 'depends_on = [aws_iam_role_policy.lambda]' in main

# Lambda sends to a function DLQ with its execution role. A service-principal
# queue policy is neither sufficient nor necessary and can create a false
# sense that the function role has permission.
assert 'data "aws_iam_policy_document" "lambda_failures_queue"' not in main
assert 'resource "aws_sqs_queue_policy" "lambda_failures"' not in main

assert 'resource "aws_cloudwatch_metric_alarm" "lambda_errors"' in main
assert 'resource "aws_cloudwatch_metric_alarm" "required_delivery_failures"' in main
assert 'metric_name         = "RequiredChannelsUndelivered"' in main
assert 'resource "aws_cloudwatch_metric_alarm" "lambda_failure_dlq_not_empty"' in main
assert 'resource "aws_sns_topic" "ops_alerts"' in main
assert 'output "ops_topic_arn"' in outputs

print("OK check_terraform_delivery")
