# Security and operational risk audit

Date: 2026-08-27

Audited commit: `8154e2467718fc08d7399765ad26e3527b457659`

Scope: tracked repository content at the audited commit

## Executive summary

Two independent read-only subagents audited the repository: one for security
and insecure defaults, and one for operational reliability and EOL data
integrity. The controlling agent traced and locally reproduced key findings.

No critical issue or committed credential was found. The most important risks
are:

1. Terraform packages the repository with a denylist, so untracked secrets or
   future files can be included in the deployed Lambda ZIP.
2. Config- and provider-controlled values are inserted into HTML reports
   without escaping.
3. Unknown/error lifecycle states can be rendered as healthy or suppressed by
   `alerts_only`, creating false assurance during upstream failures.
4. Notification failures are swallowed while the handler reports that it
   notified successfully, preventing Lambda asynchronous failure handling and
   retries from engaging.
5. Sequential 10-15 second provider calls can consume the default 60-second
   Lambda budget before any degraded report is delivered.

The audit produced these lens-specific counts. Some findings intentionally
overlap because a fail-open monitoring condition is both a security assurance
problem and an operational reliability problem.

| Lens | Critical | High | Medium | Low |
|---|---:|---:|---:|---:|
| Security | 0 | 1 | 2 | 3 |
| Operational/data integrity | 0 | 4 | 5 | 3 |

The security-assurance impact of R-02 and R-03 is discussed with those risks
but is not counted again as a separate security finding. Test coverage is
reported separately as an assurance gap rather than assigned runtime severity.

## Method

- Enumerated tracked blobs from the immutable Git commit. Subagent review and
  finding evidence excluded ignored configs, untracked files, local settings,
  remotes, production systems, and live upstream services.
- Traced the deployed path from EventBridge and S3 config loading through
  provider dispatch, reporting, SNS/SES delivery, and Terraform packaging.
- Applied an insecure-defaults review for hardcoded credentials, permissive
  access, fail-open behavior, environment/config fallback, dangerous APIs, and
  production reachability.
- Reviewed operational failure modes including provider drift, status
  categorization, partial failure, timeout budgets, cache freshness,
  notification outcomes, rollback, observability, and test coverage.
- Locally confirmed that an `unknown` result enters the OK bucket with
  `has_alerts=False`, and that raw label/message HTML is present in the rendered
  document.

## Independent validation

Three read-only subagents independently challenged the staged report against
the audited commit: a security validator, an operational/data-integrity
validator, and an adversarial report/issue-structure reviewer. Every retained
finding was traced to an intended runtime or deployment path; conditional and
local-only reachability is stated per finding. Validation also identified and
corrected duplicate counting, AWS retry terminology, severity calibration,
deliberate-versus-invalid `untracked` behavior, and two omitted risks (R-12 and
R-13).

Severity reflects impact and realistic reachability in this repository:

- **Critical:** direct, broadly exploitable compromise or systemic data loss.
- **High:** serious confidentiality/integrity exposure or likely silent loss of
  the tracker's core assurance.
- **Medium:** material weakness requiring another condition, or significant
  reliability/data-quality degradation.
- **Low:** defense-in-depth, limited exposure, or localized operational risk.

## Security findings

### S-01 - Denylist packaging can deploy unintended local files

Severity: **High** | Confidence: **High**

Evidence: `terraform/main.tf:125-159` archives the repository root and excludes
a finite list before deploying the ZIP. The list omits `.agents/`, `AGENTS.md`,
`tests/`, and any future or local file such as `.env`, credential exports,
backups, or tool state.

Impact: applying Terraform from a non-clean workspace can embed secrets or
internal material in Lambda function code. Anyone able to retrieve the
function package could recover those files. No secret was found in the tracked
snapshot and the generated ZIP was not inspected; the active risk is that
unintended files are eligible for packaging. The same design also grows the
artifact with `.agents`, tests, and other tracked non-runtime files.

Recommendation: build from a clean staging directory containing only
`lambda_function.py` and `eoltracker/**`. Verify the ZIP manifest in CI and fail
when any other path is present.

### S-02 - Dynamic report values are emitted as unescaped HTML

Severity: **Medium** | Confidence: **High**

Evidence: `eoltracker/report.py:209-255` deliberately emits provider messages
raw and also inserts labels, versions, patch/cycle fields, error messages, and
source labels without escaping. For example,
`eoltracker/parsers/npm_registry.py:99-103` copies a registry-controlled
deprecation message into the result. HTML reaches files and SES through
`eoltracker/notify.py:52-53,101-110`.

Impact: malicious config data, a compromised upstream, or package-publisher
content can alter/spoof the report, load remote tracking content, and possibly
execute active content when a local report is opened in a browser. Email client
sanitization reduces but does not remove content-injection and phishing risk.

Recommendation: HTML-escape every dynamic value at the rendering boundary.
Represent intentional markup separately from untrusted text, accept only
validated HTTPS links, and add injection tests for every rendered field.

### S-03 - Terraform provider execution is not reproducibly locked

Severity: **Medium** | Confidence: **High**

Evidence: `terraform/main.tf:1-9` constrains AWS broadly to `~> 5.0` and does not
declare a version constraint for the used `hashicorp/archive` provider.
`.gitignore:15-17` excludes `.terraform.lock.hcl`.

Impact: a fresh `terraform init` executes moving provider binaries with
deployment credentials. Registry signing lowers likelihood but cannot prevent
an unexpected provider change from altering plan/apply behavior.

Recommendation: declare and narrowly constrain every provider, commit the lock
file with platform checksums, and update providers only through reviewed
dependency changes.

### S-04 - SES event overrides create a conditional confused-deputy path

Severity: **Low** | Confidence: **Medium**

Evidence: invocation values are accepted at `eoltracker/handler.py:72-77`, used
as SES routing at `eoltracker/notify.py:85-111`, and backed by
`ses:SendEmail` on `*` at `terraform/main.tf:108-113`. The repository-created
resource policy limits invocation to exact EventBridge rules at
`terraform/main.tf:195-201`.

Impact: a principal separately granted `lambda:InvokeFunction` could turn that
permission into mail delivery to arbitrary recipients using allowed SES
identities. Exploitation also requires a selected config that enables SES
without higher-precedence routing values and an SES-permitted source identity;
none of those conditions is proven by this repository.

Recommendation: map a validated project identifier to trusted routing instead
of accepting destinations from arbitrary invocation payloads. Restrict SES
identities and recipients with IAM resources/conditions where feasible.

### S-05 - External response bodies have no size limits

Severity: **Low** | Confidence: **High**

Evidence: S3 config and provider responses use unbounded `read()` calls, for
example `eoltracker/handler.py:41-42` and
`eoltracker/parsers/npm_registry.py:39-40`. Terraform defaults Lambda memory to
128 MB at `terraform/variables.tf:24-27`.

Impact: a compromised/faulty upstream, unusually large registry document, or
oversized trusted config can exhaust memory/time and suppress the monitoring
run. Fixed HTTPS hosts make this primarily availability hardening.

Recommendation: enforce response-byte, product-count, and parsed-item limits;
validate content types and schemas before processing.

### S-06 - Operational data has indefinite default log retention

Severity: **Low** | Confidence: **High**

Evidence: product labels/messages are logged at
`eoltracker/handler.py:92-93`, and recipient addresses at
`eoltracker/notify.py:112`. Terraform grants log creation at
`terraform/main.tf:87-95` but creates no log group or retention policy.

Impact: internal dependency inventory and email addresses persist indefinitely
unless account-level controls manage the log group.

Recommendation: create the log group explicitly with finite retention, use the
correct IAM resource shapes for log-group creation and stream writes, and
redact or reduce destination logging. Decide separately whether KMS encryption
is required by policy.

## Operational and data-integrity findings

### R-01 - AWS SDK maintenance states are silently downgraded to OK

Severity: **High** | Likelihood: **High** | Confidence: **High**

Evidence: `eoltracker/parsers/aws_sdk.py:124-155` produces
`status="approaching"` with `days_remaining=None` for Maintenance phases.
`eoltracker/report.py:54-57` retains approaching only when days is non-null;
otherwise it places the result in OK.

Impact: SDKs in Maintenance or Maintenance Announcement appear under "No
Immediate Concerns" and do not notify in `alerts_only` mode.

Recommendation: treat undated approaching states as alerts independently of
numeric thresholds and add a regression fixture for both maintenance phases.

### R-02 - Error and unknown states do not trigger health alerts

Severity: **High** | Likelihood: **Medium-high** | Confidence: **High**

Evidence: `eoltracker/report.py:46-57,112,267-281` excludes errors, unknowns,
and untracked rows from `has_alerts`; the documented `unknown` state falls into
OK, while `error` is rendered separately but still leaves the banner green.
`eoltracker/handler.py:98-107` suppresses an `alerts_only` run.

Impact: upstream outages/schema drift, invalid manual dates, unknown sources,
or malformed EOL fields can be silent or falsely green. An `untracked` entry
may be deliberate, so it should not automatically be treated as failure.

Recommendation: model lifecycle state and tracker health separately. Notify on
new error/unknown health failures with a distinct subject/banner, and make the
delivery policy for deliberately untracked entries explicit and configurable.

### R-03 - Notification failures are reported as successful invocations

Severity: **High** | Likelihood: **Medium** | Confidence: **High**

Evidence: `eoltracker/notify.py:129-146` catches all notifier exceptions and
returns no status. `eoltracker/handler.py:109-115` sets `notified` from the
decision to attempt delivery, not an actual outcome. The EventBridge target at
`terraform/main.tf:181-192` has no failure destination or alarm.

Impact: throttling, invalid routing, or oversized messages produce a successful
Lambda invocation, so Lambda asynchronous retries and on-failure handling do
not engage. An unconfirmed SNS email subscription is a separate subscription-
health problem even when publish succeeds.

Recommendation: return per-channel attempted/delivered/error results, fail when
all required channels fail, and configure CloudWatch alarms plus Lambda
asynchronous retry/on-failure handling. Use an EventBridge target DLQ only for
target-delivery failures, not ordinary function-code errors.

### R-04 - Sequential network timeouts can exhaust the Lambda budget

Severity: **High** | Likelihood: **Medium** | Confidence: **High**

Evidence: `eoltracker/handler.py:90` checks products serially. Providers make
10-15 second calls, including `endoflife_date.py:17-32`, while Terraform's
default timeout is 60 seconds at `terraform/variables.tf:18-27`.

Impact: a handful of slow distinct products can terminate the Lambda before it
renders or sends even a degraded report. Partial results are lost.

Recommendation: add bounded concurrency and same-product caching, use Lambda
remaining-time awareness, shorten per-call budgets, and reserve time to send a
partial health report.

### R-05 - One malformed entry can abort the entire run

Severity: **Medium** | Likelihood: **Medium** | Confidence: **High**

Evidence: `eoltracker/handler.py:83-90` has neither schema validation nor a
per-entry exception boundary. `eoltracker/parsers/endoflife_date.py:42-43`
indexes required fields directly, and `eoltracker/parsers/__init__.py:51`
invokes a provider without a catch-all conversion to an error result.

Impact: one editable S3 entry or uncaught provider defect prevents reports and
notifications for every otherwise valid product.

Recommendation: validate the complete config with field-path errors before
processing; also convert unexpected per-entry exceptions into error results so
other products continue.

### R-06 - Documented manual operations use the wrong S3 key/default

Severity: **Medium** | Likelihood: **High** | Confidence: **High**

Evidence: Terraform uploads `projects/${each.key}/eol_config.json` at
`terraform/main.tf:33-39` and sets no `CONFIG_KEY` at
`terraform/main.tf:161-165`. The handler fallback is `eol_config.a.json` at
`eoltracker/handler.py:39`. README instructions use root `eol_config.json`,
claim Terraform sets `CONFIG_KEY`, and invoke with `{}` at
`README.md:188-235`.

Impact: operators following documented test/recovery steps can get
`NoSuchKey` or update an unused object while scheduled checks continue using
stale data.

Recommendation: document project-aware invocation payloads and exact object
keys; optionally configure one explicit default key for single-project/manual
operation.

### R-07 - S3 config replacement has no rollback control

Severity: **Medium** | Likelihood: **Medium** | Confidence: **High**

Evidence: `terraform/main.tf:20-39` creates the bucket/object without bucket
versioning, and the deployment path has no mandatory pre-upload config
validation.

Impact: a malformed or semantically incorrect config replaces the live copy
and removes S3-native point-in-time rollback. Recovery depends on retaining and
identifying a known-good local source.

Recommendation: enable versioning, validate and smoke-test before upload, expose
version IDs, and document a rollback procedure.

### R-08 - Missing registry versions are labelled OK

Severity: **Medium** | Likelihood: **Medium** | Confidence: **High**

Evidence: npm retains `status="ok"` when the in-use version is absent at
`eoltracker/parsers/npm_registry.py:105-111`. Maven behaves similarly at
`eoltracker/parsers/maven_central.py:111-125`.

Impact: a typo, unpublished/private build, or indexing gap is displayed green
even though the deployed version was not verified.

Recommendation: use an explicit unknown/untracked data-quality state. Reserve
OK for positively verified metadata.

### R-09 - Warm-container caches have no freshness boundary

Severity: **Low** | Likelihood: **Low** | Confidence: **High**

Evidence: process-global caches in `aws_rds.py`, `aws_sdk.py`, `jackson.py`,
`maven_central.py`, `npm_registry.py`, and `tyk_lifecycle.py` never expire.

Impact: warm Lambda invocations can reuse stale lifecycle data or a cached 404
after upstream data changes.

Recommendation: use invocation-scoped caches or TTLs with fetch timestamps and
refresh-on-age behavior.

### A-01 - Operational-core test coverage is incomplete

Classification: **Assurance gap** | Confidence: **High**

Evidence: tracked tests primarily cover policy-note rendering/injection and
agent documentation. They do not exercise date/status categorization, AWS SDK
maintenance, malformed configs, provider fixtures/schema drift, timeout
degradation, delivery aggregation, registry discovery, or Terraform package
contents.

Impact: regressions in the tracker's principal monitoring and delivery behavior
can reach production undetected. This is not counted as a separate runtime
finding because every remediation issue must carry its own regression tests.

Recommendation: add focused network-free fixtures and tests within each
remediation issue. Do not create one detached omnibus testing project.

### R-11 - HTML report filenames collide within one minute

Severity: **Low** | Likelihood: **Low-medium** | Confidence: **High**

Evidence: `eoltracker/notify.py:45-53` uses minute precision and overwrites
normally.

Impact: a retry or concurrent local run for one project can replace an earlier
report.

Recommendation: include seconds/microseconds or an invocation ID and use
exclusive or atomic creation.

### R-12 - HTML-file output is not writable in Lambda

Severity: **Medium** | Likelihood: **High** | Confidence: **High**

Evidence: `eoltracker/notify.py:43-53` discards the configured directory and
writes to a relative `reports/...` path. Lambda's deployed working directory is
read-only; only `/tmp` is writable. `generate_config.py:557-561` enables the
HTML-file channel in generated configs, and notifier exceptions are swallowed.

Impact: deployed HTML-file delivery fails systematically and can be mistaken
for a successful notification attempt. Relative output works only for local
runs.

Recommendation: declare `html_file` local-only or preserve and validate an
absolute `/tmp` destination. Use S3 for durable AWS-hosted reports. Add Lambda-
mode and local-mode tests.

### R-13 - Provider registration is not isolated or collision-checked

Severity: **Low** | Likelihood: **Low** | Confidence: **High**

Evidence: `eoltracker/parsers/__init__.py:21-28` eagerly imports every module at
cold start and silently overwrites duplicate `SOURCE` values.

Impact: one import-time provider defect prevents Lambda startup, while a
duplicate source key can route entries to the wrong provider without a clear
error. Current tracked modules import successfully and have unique keys.

Recommendation: reject duplicate source keys, produce actionable import
diagnostics, and add registry tests. Make an explicit product decision before
allowing degraded startup that skips a broken deployed provider.

## Positive controls

- No hardcoded credentials, private keys, weak cryptography, debug bypasses,
  shell/eval execution, or unsafe deserialization were found in production
  code.
- Missing `CONFIG_BUCKET` fails closed through required environment access.
- Terraform blocks all public S3 access.
- S3 reads and SNS publishes are resource-scoped; repository-created Lambda
  invocation permission is scoped to exact EventBridge rule ARNs.
- Production upstream URLs are fixed HTTPS endpoints with normal certificate
  verification and finite socket timeouts.
- JSON and `html.parser` inputs are treated as data and do not execute code.
- `policy_note` and support-message text are HTML-escaped.
- Several scraper providers use header checks, row floors, and canaries to fail
  loudly on upstream structural drift.
- Notification channels are isolated so a failed channel does not prevent a
  later independent channel from being attempted.

## Recommended remediation order

### Immediate

1. Replace denylist ZIP construction with an allowlisted clean staging build
   and verify its manifest.
2. Escape all dynamic HTML fields and add comprehensive injection tests.
3. Correct status categorization for unknown/error and undated approaching
   states; introduce tracker-health notifications.
4. Return real notification delivery outcomes and fail/retry when all required
   delivery paths fail.

### Near term

5. Add bounded concurrency, remaining-time handling, and partial/degraded
   reporting.
6. Validate configs and isolate unexpected failures per product.
7. Bound S3 and upstream response sizes independently of timeout handling.
8. Pin all Terraform providers and commit the dependency lock file.
9. Add Lambda failure handling, alarms, finite log retention, and Lambda-aware
   HTML output.

### Planned hardening

10. Enable S3 versioning and document rollback.
11. Add cache TTLs, correct operator documentation, distinguish unverifiable
    registry versions from OK, and make report filenames collision-resistant.
12. Harden provider registration. Include focused network-free regression tests
    in every remediation change rather than creating an omnibus testing task.

## Validation performed

- Python byte-compilation: passed for `eoltracker/`, `lambda_function.py`, and
  `generate_config.py`.
- Current-working-tree documentation integrity (`tests/check_agent_docs.py`):
  passed; this result is not presented as validation of the audited commit.
- `tests/test_policy_html.py`: passed.
- `tests/test_policy_injection.py`: passed.
- `tests/test_policy_text.py`: passed.
- `terraform fmt -check -diff`: passed.
- Controlled local probes reproduced the unknown-to-OK categorization and raw
  HTML insertion findings.

These commands ran in the working tree, whose tracked runtime and infrastructure
files matched the audited commit. They were not run from a clean exported
archive; only tracked code behavior is cited as audit evidence.

## Limitations

- Static review of one immutable commit; no live upstream schema or latency
  testing was performed.
- No Terraform plan/apply, generated ZIP manifest inspection, AWS IAM account
  review, CloudWatch inspection, or notification subscription verification.
- No exhaustive Git-history secret scan, external scanner installation, or
  third-party service probing.
- Actual SES override exploitability depends on identity policies outside this
  repository.
- Tracked screenshots were not examined for hidden metadata or sensitive
  visual content.
