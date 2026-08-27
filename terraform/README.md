# Terraform deployment guide

Deployment for the EOL tracker Lambda: S3 config bucket, per-project SNS
topics and EventBridge schedules, SES wiring, and the function package itself.
Run all commands from this `terraform/` directory unless stated otherwise.

This file covers two operational topics that depend on tracked infrastructure
state:

- **Provider pinning and the dependency lock file** (`.terraform.lock.hcl`
  policy and update procedure),
- **S3 config upload discipline and point-in-time rollback** (the bucket has
  versioning enabled on purpose).

## Provider pinning

Providers are declared and narrowly constrained in `main.tf`. The narrow
three-component pessimistic form (`~> X.Y.Z`) allows patch releases within one
minor line only; the committed `.terraform.lock.hcl` additionally pins the
exact build via checksums. Together they make `terraform init` reproducible:
a fresh clone installs exactly what was last reviewed instead of drifting to
whatever the registry serves that day.

| Provider | Source | Constraint |
|---|---|---|
| AWS | `hashicorp/aws` | `~> 5.100.0` |

Rules of thumb:

- Every provider used by any `resource` or `data` type must appear under
  `required_providers` with a narrow constraint. `tests/check_terraform_infra.py`
  enforces this statically - run it after every Terraform change.
- The lock file **is committed** (only `.terraform/`, state, tfvars, and the
  built ZIP are ignored). Never commit `.terraform/` contents or provider
  binaries themselves.
- Version constraints change only through reviewed dependency updates (below).

### Generating / refreshing the lock file

The lock file records `h1:` hashes for provider ZIPs. The deployed Lambda
target runs on Linux x86_64 (`linux_amd64`), so make sure at least that
platform's checksums are recorded no matter which OS you develop on:

```bash
cd terraform

# One-time bootstrap on a clean checkout (or after changing constraints):
terraform init

# Ensure deployment-relevant platform checksums exist even when developing
# off-Linux (no provider execution here - just registry hash collection):
terraform providers lock -platform=linux_amd64 -platform=linux_arm64

# On the actual Linux deployment runner (or an equivalent CI job), prove that
# the committed lock can install without being rewritten:
terraform init -backend=false -lockfile=readonly

# Commit the resulting terraform/.terraform.lock.hcl alongside whatever
# constraint change triggered it.
```

### Updating a provider (reviewed procedure)

1. Edit the constraint in `main.tf` deliberately (e.g. `~> 5.101.0`). Moving
   across a major boundary always deserves its own change.
2. `terraform init -upgrade`, then
   `terraform providers lock -platform=linux_amd64 -platform=linux_arm64`.
   Verify with `terraform init -backend=false -lockfile=readonly` on the Linux
   deployment runner; the static test cannot attribute each `h1:` to a platform.
3. Inspect the `.terraform.lock.hcl` diff line by line: each removed `h1:`
   entry means a different binary will execute during plan/apply. Verify the
   bumped version corresponds to the release you intended.
4. Run `terraform plan` and read it end-to-end before applying anything;
   provider majors can rewrite resources entirely.
5. Checks: `terraform fmt -check`, `python tests/check_terraform_infra.py`.
6. Commit `main.tf` and `.terraform.lock.hcl` together - neither is
   meaningful without the other.

## Config uploads and validation

Terraform uploads each project's config (`projects/<name>/eol_config.json`)
on apply, and scheduled runs reload it from S3. Each object depends on a
`terraform_data.validate_eol_config` preflight, so a validation error stops
the apply before S3 replacement. Run the same command directly for faster
feedback:

```bash
# Network-free structural lint (exit 0 = valid, 1 = invalid):
python ../lambda_function.py --validate ../eol_config.<project>.json
```

Warnings (env-var fallbacks, duplicate labels, unknown keys) should be treated
as prompts to tighten the config, not noise. The static test suite also keeps
the shipped template clean: `python tests/test_config_validation.py`.

## S3 config rollback runbook

Bucket versioning is enabled (`aws_s3_bucket_versioning.config`); every write
to `projects/<name>/eol_config.json` adds a version. Do **not** add lifecycle
rules expiring noncurrent versions - they would silently destroy recovery
points.

Roll back by copying an old version forward (this creates a *new* current
version; history stays intact):

```bash
# 1. Find candidate versions. Stack outputs give the version produced by the
#    last successful apply (the known-good baseline); the API lists everything.
terraform output config_object_version_ids
aws s3api list-object-versions \
  --bucket <config-bucket> \
  --prefix projects/<project>/eol_config.json

# 2. Download the candidate and validate it BEFORE promoting it:
aws s3api get-object \
  --bucket <config-bucket> \
  --key projects/<project>/eol_config.json \
  --version-id <VERSION_ID> \
  candidate.json
python ../lambda_function.py --validate candidate.json

# 3. Promote the validated candidate as the current object:
aws s3api copy-object \
  --copy-source "<config-bucket>/projects/<project>/eol_config.json?versionId=<VERSION_ID>" \
  --bucket <config-bucket> \
  --key projects/<project>/eol_config.json

# 4. Confirm the active version changed (head-object prints VersionId):
aws s3api head-object \
  --bucket <config-bucket> \
  --key projects/<project>/eol_config.json
```

Notes:

- `VersionId` appears in `head-object` output only once versioning is on.
- Use the Terraform `config_object_version_ids` output as the last-applied
  baseline, then correlate later manual uploads with the S3 versions listing.
- To trigger a manual re-check after restoring, invoke the Lambda with the
  EventBridge-shaped payload (`{"project": "<project>", "config_key":
  "projects/<project>/eol_config.json", "sns_topic_arn": "...",
  "ses_to_emails": ""}`); otherwise wait for the daily schedule.
- Permanently deleting individual versions removes those recovery points -
  reserve that for true hygiene tasks with a retention decision behind them,
  not for routine rollback.
