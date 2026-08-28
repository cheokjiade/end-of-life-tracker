output "lambda_function_name" {
  description = "Name of the deployed Lambda function (shared across projects)"
  value       = aws_lambda_function.eol_checker.function_name
}

output "lambda_function_arn" {
  description = "ARN of the deployed Lambda function"
  value       = aws_lambda_function.eol_checker.arn
}

output "config_bucket" {
  description = "S3 bucket holding the per-project EOL config files"
  value       = aws_s3_bucket.config.id
}

output "config_file_keys" {
  description = "Per-project S3 keys — update these files to change tracked products"
  value       = { for k, o in aws_s3_object.eol_config : k => o.key }
}

output "config_object_version_ids" {
  description = "Per-project version ID of the config object uploaded by the last apply — the known-good baseline for S3 rollback (see README.md)"
  value       = { for k, o in aws_s3_object.eol_config : k => o.version_id }
}

output "sns_topic_arns" {
  description = "Per-project SNS topic ARNs (subscribers must confirm via email)"
  value       = { for k, t in aws_sns_topic.eol_alerts : k => t.arn }
}

output "ops_topic_arn" {
  description = "Operational alarm SNS topic ARN (subscription must be confirmed)"
  value       = aws_sns_topic.ops_alerts.arn
}

output "schedule_rule_names" {
  description = "Per-project EventBridge rule names — useful for manual `aws events put-events` testing"
  value       = { for k, r in aws_cloudwatch_event_rule.daily : k => r.name }
}
