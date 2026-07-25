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

output "sns_topic_arns" {
  description = "Per-project SNS topic ARNs (subscribers must confirm via email)"
  value       = { for k, t in aws_sns_topic.eol_alerts : k => t.arn }
}

output "schedule_rule_names" {
  description = "Per-project EventBridge rule names — useful for manual `aws events put-events` testing"
  value       = { for k, r in aws_cloudwatch_event_rule.daily : k => r.name }
}
