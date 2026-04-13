# EOL Tracker

A Python AWS Lambda that checks software end-of-life status using the [endoflife.date](https://endoflife.date) API and sends alerts via email (SNS/SES), HTML report, or console output.

Track your stack — Spring Boot, Java, Nginx, Alpine, PostgreSQL, React, and [300+ more products](https://endoflife.date/) — and get notified before anything falls out of support.

## Features

- Checks EOL dates for any product tracked by endoflife.date
- Reports latest patch version and release date for each tracked product
- Shows latest available major/minor cycle so you know when a newer version exists
- Configurable alert thresholds (e.g. warn at 30, 60, 90 days before EOL)
- Multiple output channels: console, HTML file, SNS (plain text email), SES (HTML email)
- Product list is stored in S3 — update what you track without redeploying the Lambda
- Runs daily via CloudWatch Events (schedule is configurable)

## Quick Start — Run Locally

**Prerequisites:** Python 3.9+

```bash
# 1. Clone the repo
git clone https://github.com/cheokjiade/endoflife-tracker.git
cd endoflife-tracker

# 2. Copy the sample config and edit it
cp eol_config.sample.json eol_config.json

# 3. Run it
python lambda_function.py
```

No AWS credentials or external dependencies are needed for local testing. The script reads from the local config file and outputs to the channels listed in `notifications` (defaults to console + HTML file).

### Sample output

#### Console (plain text)

![Console output](docs/sample_console_output.png)

#### HTML report

The HTML report (`eol_report.html`) is generated alongside the console output if configured. It uses colour-coded rows and status badges, and is also the format sent via SES email.

![HTML report](docs/sample_html_report.png)

## Configuration

The config file (`eol_config.json`) controls everything. In Lambda, it lives in S3; locally, it's read from the filesystem.

### Products

Each entry needs three fields:

| Field | Description | Example |
|-------|-------------|---------|
| `product` | Product name as it appears in the endoflife.date API | `spring-boot` |
| `version` | Release cycle identifier (usually `major.minor`) | `4.0` |
| `label` | Display name in reports | `Spring Boot 4.0` |

**Finding product names and cycles:**

```bash
# List all available products
curl https://endoflife.date/api/all.json | python -m json.tool

# List all cycles for a product
curl https://endoflife.date/api/spring-boot.json | python -m json.tool
```

Or browse [endoflife.date](https://endoflife.date/) directly.

### Alert thresholds

```json
"alert_thresholds_days": [30, 60, 90]
```

Products within the **largest** threshold (90 days) of their EOL date are flagged as "approaching end of life". Products past their EOL date are flagged as "already end of life".

### Notification frequency

```json
"notify_when": "always"
```

| Value | Behaviour |
|-------|-----------|
| `always` | Send a report every run, even if nothing needs attention |
| `alerts_only` | Only send when at least one product is EOL or approaching |

### Notification channels

```json
"notifications": [
  {"type": "console"},
  {"type": "html_file", "path": "eol_report.html"},
  {"type": "sns", "topic_arn": "arn:aws:sns:eu-west-1:123456789:eol-alerts"},
  {"type": "ses", "from_email": "noreply@example.com", "to_emails": ["team@example.com"]}
]
```

| Type | Format | Notes |
|------|--------|-------|
| `console` | Plain text to stdout | No config needed |
| `html_file` | HTML file | `path` defaults to `eol_report.html` |
| `sns` | Plain text email via SNS | `topic_arn` or `SNS_TOPIC_ARN` env var |
| `ses` | HTML email via SES | `from_email`/`to_emails` or `SES_FROM_EMAIL`/`SES_TO_EMAILS` env vars. Sender must be [verified in SES](https://docs.aws.amazon.com/ses/latest/dg/creating-identities.html) |

You can enable multiple channels simultaneously.

## Deploy to AWS Lambda

### Prerequisites

- [Terraform](https://www.terraform.io/downloads) >= 1.0
- AWS CLI configured with appropriate credentials
- An email address to receive SNS alerts

### Steps

```bash
cd terraform

# 1. Create your variables file
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — set your bucket name and notification email

# 2. Deploy
terraform init
terraform apply
```

After deployment:
- **Confirm the SNS subscription** — check your email for a confirmation link from AWS
- The Lambda runs daily at 8:00 AM UTC by default (configurable via `schedule_expression`)
- The config file is uploaded to S3 automatically from `eol_config.json`

### Updating tracked products

Update the config in S3 — no redeployment needed:

```bash
# Download current config
aws s3 cp s3://your-bucket-name/eol_config.json .

# Edit it...

# Upload
aws s3 cp eol_config.json s3://your-bucket-name/eol_config.json
```

Or update it via the S3 console.

### Terraform variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `config_bucket_name` | Yes | - | S3 bucket name (globally unique) |
| `notification_email` | Yes | - | Email for SNS alerts |
| `aws_region` | No | `eu-west-1` | AWS region |
| `schedule_expression` | No | `cron(0 8 * * ? *)` | CloudWatch cron schedule |
| `ses_from_email` | No | `""` | SES sender address (if using SES) |
| `ses_to_emails` | No | `""` | Comma-separated SES recipients |

### Environment variables (set by Terraform)

| Variable | Purpose |
|----------|---------|
| `CONFIG_BUCKET` | S3 bucket containing the config file |
| `CONFIG_KEY` | S3 key for the config file |
| `SNS_TOPIC_ARN` | SNS topic ARN for plain-text alerts |
| `SES_FROM_EMAIL` | SES sender (optional, can also be set in config) |
| `SES_TO_EMAILS` | SES recipients (optional, can also be set in config) |

### Manual invocation

Trigger the Lambda outside its schedule:

```bash
aws lambda invoke \
  --function-name eol-checker \
  --payload '{}' \
  response.json && cat response.json
```

## Architecture

```
CloudWatch Events (daily cron)
        |
        v
  AWS Lambda (Python 3.12)
        |
        +-- reads config from S3
        +-- calls endoflife.date API for each product
        +-- categorises: EOL / Approaching / OK
        |
        +---> SNS (plain-text email)
        +---> SES (HTML email)
        +---> HTML file (to /tmp or S3)
        +---> Console (CloudWatch Logs)
```

## Data source

All EOL data comes from [endoflife.date](https://endoflife.date), a community-maintained, open-source project that tracks end-of-life dates for 450+ products. The API is free, requires no authentication, and is updated regularly.

## License

MIT
