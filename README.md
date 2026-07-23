# EOL Tracker

A Python AWS Lambda that checks software end-of-life status from multiple data sources and sends alerts via email (SNS/SES), HTML report, or console output.

Track your stack — Spring Boot, Java, Nginx, Alpine, PostgreSQL, React, and [300+ more products](https://endoflife.date/) — and get notified before anything falls out of support. For AWS RDS / Aurora, the tracker can also scrape AWS's own release calendars for minor-version EOL dates that endoflife.date does not provide.

## Features

- **Pluggable data sources.** Each product picks its provider — endoflife.date for the broad catalog, or the AWS-docs scraper for RDS/Aurora minor-version EOL.
- Reports latest patch version and release date for each tracked product
- Shows latest available major/minor cycle so you know when a newer version exists
- Configurable alert thresholds (e.g. warn at 30, 60, 90 days before EOL)
- Multiple output channels: console, HTML file, SNS (plain text email), SES (HTML email)
- HTML reports include per-row source attribution and are written to `reports/<project>/<year>/<month>/<day>/` with a timestamp in the filename (e.g. `reports/a/2026/05/04/eol_report_a_2026-05-04_1132.html`)
- Product list is stored in S3 — update what you track without redeploying the Lambda
- Runs daily via CloudWatch Events (schedule is configurable)
- AWS-docs scraper has built-in defenses against page changes: header-name validation, row-count sanity check, and a runtime canary that fails loudly if the page structure drifts

## Quick Start — Run Locally

**Prerequisites:** Python 3.9+

```bash
# 1. Clone the repo
git clone https://github.com/cheokjiade/endoflife-tracker.git
cd endoflife-tracker

# 2. Copy the sample config and edit it
cp eol_config.sample.json eol_config.json

# 3. Run it
python lambda_function.py eol_config.json
```

No AWS credentials or external dependencies are needed for local testing. The script reads from the local config file and outputs to the channels listed in `notifications` (defaults to console + HTML file).

### Helper scripts

Cross-platform wrappers pick the Python interpreter for you and let you choose which config to run. Use `run.sh` on macOS/Linux (or Git Bash / WSL) and `run.ps1` on native Windows PowerShell.

```bash
# macOS / Linux
./run.sh                      # interactive menu of available configs
./run.sh a                  # shorthand -> eol_config.a.json
./run.sh eol_config.a.json  # explicit file name (a path also works)
./run.sh --list               # list available configs and exit
```

```powershell
# Windows (PowerShell)
.\run.ps1                      # interactive menu of available configs
.\run.ps1 a                  # shorthand -> eol_config.a.json
.\run.ps1 eol_config.a.json  # explicit file name (a path also works)
.\run.ps1 -List                # list available configs and exit
```

Passing a bare name like `a` resolves to `eol_config.a.json`; with no argument you get a numbered menu of every `eol_config.*.json` file in the repo:

![Interactive config menu](docs/sample_interactive_menu.png)

### Sample output

#### Console (plain text)

![Console output](docs/sample_console_output.png)

#### HTML report

The HTML report is generated alongside the console output if configured, written under `reports/<project>/<year>/<month>/<day>/` (where `<project>` is derived from the configured `path` base name — `eol_report_a.html` → `a`, plain `eol_report.html` → `default`). It uses colour-coded rows and status badges, and is also the format sent via SES email.

![HTML report](docs/sample_html_report.png)

## Configuration

The config file (`eol_config.json`) controls everything. In Lambda, it lives in S3; locally, it's read from the filesystem.

### Products

Each entry has a `source` field selecting which data provider to use. If `source` is omitted it defaults to `endoflife_date` (so existing configs keep working unchanged).

#### Source: `endoflife_date` (default)

| Field | Description | Example |
|-------|-------------|---------|
| `source` | Optional — defaults to `endoflife_date` | `"endoflife_date"` |
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

#### Source: `aws_rds_scrape`

For RDS / Aurora PostgreSQL, endoflife.date only tracks the major version EOL (e.g. `17 → 2030-02-28`). AWS, however, also deprecates *minor* versions on a much shorter timeline (e.g. `17.5 → September 2026` for plain RDS, `17.5 → December 2026` for Aurora). This source scrapes AWS's release-calendar pages to surface those minor-version dates.

| Field | Description | Example |
|-------|-------------|---------|
| `source` | `"aws_rds_scrape"` | `"aws_rds_scrape"` |
| `engine` | `"aurora-postgresql"` or `"rds-postgresql"` | `"aurora-postgresql"` |
| `version` | Minor version (`major.minor`) | `17.5` |
| `label` | Display name in reports | `AWS RDS Aurora PostgreSQL 17.5` |

```json
{
  "source": "aws_rds_scrape",
  "engine": "aurora-postgresql",
  "version": "17.5",
  "label": "AWS RDS Aurora PostgreSQL 17.5"
}
```

The scraper validates the page structure on every run. If AWS renames a column, removes the table, or changes the layout, every entry from this source is marked as `error` in the report — you get a loud alert via your existing notification channels rather than silently wrong dates.

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
| `html_file` | HTML file | `path` defaults to `eol_report.html`; the file is written under `reports/<project>/<year>/<month>/<day>/` (`<project>` derived from the `path` base name) |
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
        +-- per product, dispatches to a provider:
        |     +-- endoflife_date  -> https://endoflife.date/api/{product}.json
        |     +-- aws_rds_scrape  -> docs.aws.amazon.com release calendars
        +-- categorises: EOL / Approaching / OK
        |
        +---> SNS (plain-text email)
        +---> SES (HTML email)
        +---> HTML file (to /tmp or S3)
        +---> Console (CloudWatch Logs)
```

### Adding a new data source

Providers are simple functions in `lambda_function.py`:

```python
def _provider_my_source(entry, today):
    # ... fetch + transform ...
    return {"label": ..., "status": "ok|approaching|eol|error|unknown",
            "message": ..., "eol_date": ..., "source": "my_source", ...}

PROVIDERS["my_source"] = _provider_my_source
```

Once registered, any product entry with `"source": "my_source"` is routed to it. The normalized result dict is consumed by the existing categorizer and report formatters — no other changes needed.

## Data sources

| Source | Coverage | Auth | Granularity |
|--------|----------|------|-------------|
| [endoflife.date](https://endoflife.date) | 450+ products (community-maintained, open-source) | None | Major/cycle |
| AWS docs release calendars | RDS / Aurora PostgreSQL minor versions | None (public HTML pages) | Minor version |

## License

MIT
