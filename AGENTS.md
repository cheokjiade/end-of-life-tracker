# AGENTS.md

Canonical guidance for AI coding agents — any harness — working in this
repository. Authoritative reusable workflows live under `.agents/skills/`;
`CLAUDE.md` contains only the aliases Claude Code needs for discovery.

## What this is

An AWS Lambda that checks software **end-of-life (EOL)** status across multiple
data sources and reports via console / HTML file / SNS / SES. The runtime is a
**stdlib-only** Python package — `eoltracker/` — with a thin
`lambda_function.py` shim that re-exports the handler (preserving the
`lambda_function.lambda_handler` entry point); `build_lambda_package.py`
assembles Git-tracked runtime sources into an allowlisted deployment zip with
a verified manifest (see `docs/packaging.md`).

## Workflows index

| I want to... | Read / run |
|---|---|
| Generate or update a config with an AI coding agent | Invoke `manage-eol-config` in Codex/OpenCode or `eol-config` in Claude Code; canonical instructions: `.agents/skills/manage-eol-config/SKILL.md` |
| Validate a config structurally (network-free) | `python lambda_function.py --validate <config.json>` |
| Update Terraform providers, handle the lockfile, or roll back an S3 config | `terraform/README.md` |
| Generate a config from dependency manifests (`pom.xml`, `*.gradle*`, package.json) | `python generate_config.py <folder> --name <project>`, then live-verify (norms below) |
| Generate a config by scanning dependency manifests and container files | `python helper_scripts/generate_config.py <folder> --name <project>` (Java, Node, Python, Go, .NET, Dockerfile, GitLab CI images); render the Markdown/CSV/HTML inventory with `python helper_scripts/generate_inventory_report.py <config>`; novice guide: `helper_scripts/README.md` |
| Generate a config from messy inputs (wiki/Confluence tables, spreadsheets, prose) | Follow the extraction spec in `eol_config_generation_prompt.md` |
| Update an existing config after upgrades or inventory changes | `docs/updating-a-config.md` |
| Add a new data-source provider | Invoke `add-eol-provider` in Codex/OpenCode or `eol-provider` in Claude Code; canonical instructions: `.agents/skills/add-eol-provider/SKILL.md` |
| Repair a provider whose upstream page drifted | Use the same provider skill; canonical repair procedure: `docs/adding-a-provider.md` |
| Make and commit repository changes | Follow `docs/commit-conventions.md`; commit each completed, verified batch |
| Run a Codex task while conserving Codex allowance | Ask Codex to follow `docs/codex-usage-efficient-workflow.md` |

Two config generators currently coexist on purpose: root `generate_config.py`
(the plain manifest extractor, being extended by open PR #35) and
`helper_scripts/generate_config.py` backed by `helper_scripts/eol_inventory/`
(the multi-ecosystem inventory scanner with provenance, curation-preserving
`--update`, and container-image scanning). Consolidating them into one tool is
a follow-up decision, not yet made — do not delete or merge either one without
an explicit instruction to do so.

Universal norms, whichever workflow you are in:

- **Verify, don't fabricate.** Confirm endoflife.date slugs/cycles and
  npm/Maven/PyPI/NuGet/Go packages against the live APIs before writing them
  into a config. A wrong string becomes a broken `error` row on every future run.
- **Validate + smoke-run** any config you write or change:
  `python -c "import json; json.load(open('eol_config.<project>.json'))"`, then
  `python lambda_function.py eol_config.<project>.json`.
- **Prefer automation over manual.** Check endoflife.date first for
  commercial/infra software; use a scraper provider where one fits (e.g. Tyk);
  reserve `manual` for things with no automated source anywhere (PuTTY,
  OpenSSH's own schedule). A live source stays current; a hardcoded manual date
  rots.
- **Tests are network-free**; finish with one live smoke run.

## Package layout

```
lambda_function.py        # shim: re-exports lambda_handler; __main__ runs run_local
eoltracker/
  core.py                 # logger, parse_date_field, _error_result, the two HTML table parsers,
                          #   plus the bounded-JSON/HTTP helpers validate_bounded_json,
                          #   read_response_bytes, decompress_gzip_bytes
  parsers/                # one file per provider + __init__.py (auto-registration + dispatch);
                          #   includes go_proxy.py, nuget_registry.py, pypi_registry.py
  report.py               # _categorise + plain-text and HTML formatters
  notify.py               # notification channels
  runner.py               # run_checks: bounded-concurrency provider execution, lookup dedupe,
                          #   Lambda remaining-time budget with configurable reserve (R-04);
                          #   env knobs EOL_MAX_WORKERS / EOL_TIME_RESERVE_MS /
                          #   EOL_CHECK_START_GUARD_MS
  handler.py              # config loading, lambda_handler, run_local
  html_runner.py          # HTML-only local report runner: one process, many configs, reusing
                          #   provider caches, never touching console/SNS/SES
  validation.py           # config JSON decode/parse + eoltracker.validation schema enforcement
helper_scripts/
  generate_config.py      # root-alternative CLI: folder scan -> eol_config.<project>.json (+ _inventory);
                          #   extended by open PR #35 — see the coexistence note above
  generate_inventory_report.py  # CLI: config -> Markdown/CSV/HTML inventory report
  generate_config.sh/.ps1, generate_inventory_report.sh/.ps1  # interactive wrappers
  eol_inventory/          # importable scan package: discovery.py, models.py, mappings.py,
                          #   config_writer.py, report_writer.py, config_io.py (bounded config
                          #   load, mirrors eoltracker/core.py's size/depth bounds), redact.py
                          #   (URL/SSH/SCP secret redaction), parsers/ (per ecosystem)
```

## Architecture: providers (the "parsers")

The core is a **provider/registry plugin pattern**. Each data source is a
*provider* — a function with a uniform contract:

```python
def _provider_<name>(entry, today) -> dict   # a normalized result dict
```

- **Dispatch:** `check_product(entry, today)` reads `entry["source"]`, looks it
  up in the `PROVIDERS` registry (defaults to `endoflife_date`), and calls it.
  Entries carrying a `_section` marker return `None` (they are config-file
  dividers, not products). `check_product` is also the per-entry isolation
  boundary: non-dict entries and entries failing the `eoltracker.validation`
  field checks return an error result *before* the provider runs, and an
  unexpected provider exception is converted into the normalized error shape
  (details logged) so one broken entry cannot abort the run.
- **Uniform result shape:** every provider returns the same dict keys (`label`,
  `product`, `version`, `status`, `message`, `eol_date`, `days_remaining`,
  `latest_patch`, `source`, ...) so both formatters (`format_report_text`,
  `format_report_html`) consume any provider unchanged.
- **Shared contract helpers:** in `eoltracker/core.py` — `_error_result(entry,
  msg)` (uniform error shape), `parse_date_field` (date/bool/None), and the
  reusable HTML parsers `_HtmlTableExtractor` (single-table pages) and
  `_AWSCalendarParser` (heading-anchored, multi-table pages). `_categorise`
  (bucket by status) lives in `eoltracker/report.py`.
- **Status values:** `eol`, `approaching`, `ok`, `error`, `unknown`,
  `untracked`. `_categorise` buckets them along two independent dimensions:
  *lifecycle* (`eol`; `approaching` alerts when `days_remaining <=
  max(thresholds)` **or no date is published** — only a dated far-future
  approaching falls to informational `ok`) and *tracker health* (`error` /
  `unknown` never render as healthy and notify even under
  `notify_when=alerts_only`, with a distinct `[TRACKER HEALTH]` subject and
  banner). Deliberate `untracked` stays a distinct informational bucket.

**Modularity:** each provider is its own file under `eoltracker/parsers/`,
**auto-registered** at import time (`eoltracker/parsers/__init__.py` scans the
package) —
adding one is localized and touches no other provider: drop in a new file, no
registry edits. Current providers (11): `endoflife_date`, `aws_rds_scrape`,
`aws_sdk_lifecycle`, `jackson_lifecycle`, `maven_central`, `npm_registry`,
`pypi_registry`, `nuget_registry`, `go_proxy`, `manual`, `tyk_lifecycle`.

## Adding or repairing a data-source provider

The full how-to with a copy-paste skeleton is in `docs/adding-a-provider.md`
(including the "Repairing a broken provider" flow for when an upstream page
drifts). In brief:

1. **Write the provider module** as a new file `eoltracker/parsers/<name>.py`: a
   cached fetch/scrape helper, a *pure* parse helper (so logic is testable
   without network), and `def _provider_<name>(entry, today):` returning the
   normalized dict (or `_error_result(entry, msg)` with
   `result["source"] = "<name>"` on failure). Import shared helpers from
   `..core`.
2. **Register via module attributes** (auto-discovered — no registry edits):
   set `SOURCE`, `LABEL`, `provider = _provider_<name>`, and an optional
   `url_for(r)` for the upstream link.
3. **Defensive parsing** (match `aws_rds_scrape` / `jackson_lifecycle`):
   required-header checks, a row-count floor, and/or a hardcoded canary — fail
   loudly on page drift rather than emit silently-wrong dates.
4. **New status?** If you add one (as `untracked` was), update `_categorise`
   (bucket + return tuple) AND both formatters (unpack + rendering), plus
   `_STATUS_COLOURS` / `_status_label` for HTML — all in `eoltracker/report.py`.
5. **Test network-free**, then one live smoke run
   (`python lambda_function.py <config>`).
6. **Document it** in `eol_config_generation_prompt.md` (providers table + entry
   shape + decision order) so config generation uses it, and update the provider
   count/list in this file.

## Cross-agent skills

- `.agents/skills/manage-eol-config/SKILL.md` is the canonical workflow for
  config generation, messy-input extraction, and curation-preserving updates.
- `.agents/skills/add-eol-provider/SKILL.md` is the canonical workflow for
  adding or repairing providers.
- Codex and OpenCode discover the canonical skills directly. Claude Code
  discovers only `.claude/skills/`, so `eol-config` and `eol-provider` are thin
  loaders that point to the canonical files. Their distinct IDs prevent exact
  collisions when OpenCode scans both locations. Do not copy workflow logic
  into a loader.
- There is no repository-defined extractor subagent. Any harness may delegate
  the canonical skill to its normal subagent mechanism when context isolation
  or parallel work is useful.

## Config generation & maintenance

- `eol_config_generation_prompt.md` is the **canonical extraction spec**: config
  schema, the 11 providers' entry shapes, the input-to-entry mapping decision
  order, and real-world document patterns (strikethrough = skip, "was X now Y" =
  current version, multi-version cells, reference-URL slug hints). Any agent in
  any harness can follow it directly.
- Root `generate_config.py` is the deterministic extractor for clean dependency
  manifests (Maven / Gradle / npm) — no LLM required. `--resolve-transitive` resolves the full graph by shelling out to mvn/gradle and parsing npm lockfiles.
- Generated configs carry `_discovered_dependencies` (every parsed declaration with its outcome) alongside the deduped runnable `products`.
- `helper_scripts/generate_config.py` is the deterministic scanner — no LLM
  required — for Java, Node, Python, Go, and .NET manifests plus Dockerfile and
  GitLab CI image declarations. It emits per-entry `_found_in` provenance, an
  ignored `_inventory` object (warnings, unmapped items, counts), explicit
  `manual`/untracked rows for unmapped items, scans **direct dependencies
  only** by default (`--include-transitive` opts into indirect/lockfile
  records), refuses to overwrite an existing config unless `--update`
  (curation-preserving merge) or `--replace` (explicit wholesale) is given,
  and writes atomically as ASCII. Root `generate_config.py` (previous bullet)
  is a separate extractor focused on Maven / Gradle / npm (see the coexistence
  note under "Workflows index"); do not remove or modify it as part of
  inventory-scanner work.
- `helper_scripts/generate_inventory_report.py` renders a config locally (no
  network) as Markdown (default), CSV, and HTML under
  `reports/inventory/`, with a manual-review checklist; legacy configs and
  `_skipped_npm_packages` remain readable. Interactive `.sh`/`.ps1` wrappers
  cover both CLIs for first-time users; see `helper_scripts/README.md`.
- `docs/updating-a-config.md` is the refresh workflow: diff new evidence against
  the existing config and patch it, preserving human curation. Never regenerate
  an existing config wholesale — with the scanner, that means `--update`, never
  `--replace`.

## Git workflow and commits

AI agents are expected to leave completed work in reviewable commits, not as an
ever-growing working-tree diff. Follow `docs/commit-conventions.md` for the
canonical message format and detailed workflow.

- Treat one logical user-requested change, or a tightly related group of
  changes verified together, as a **batch**. Commit immediately after that
  batch passes its relevant checks and before starting unrelated work.
- A batch may use more than one commit when its changes are independently
  reviewable. Do not combine unrelated changes merely to produce one commit.
- At the start and end of a batch, inspect `git status --short`. Stage only the
  paths or hunks changed for that batch; never use `git add -A` in a dirty
  working tree.
- Do not include pre-existing user edits, untracked files, generated reports,
  ignored per-project configs, secrets, or another agent's work. If a file
  mixes changes that cannot be safely separated, leave it uncommitted and
  report the blocker.
- Review the staged diff and run the relevant tests before committing. Do not
  commit a knowingly failing or incomplete batch unless the user explicitly
  asks for a checkpoint commit; label such a commit clearly in its body.
- Do not run routine post-commit audits for feature, refactor, documentation,
  test, build, CI, or maintenance batches.
- For bug-fix batches only, after committing the verified fix, dispatch one
  fresh, read-only adversarial subagent using the process in
  `docs/commit-conventions.md`. The subagent attempts to disprove the fix and
  must not edit, stage, commit, or push.
- Resolve actionable adversarial findings in a follow-up commit and re-run the
  adversarial review. A bug fix is not complete until the review is clean or
  the user explicitly accepts a documented residual risk.
- A direct user instruction such as "do not commit" or "leave this for review"
  overrides the standing commit rule for that batch.
- Committing does not authorize pushing, force-pushing, rebasing, or rewriting
  history. Perform those actions only when the user explicitly requests them.

## Conventions & gotchas

- **Stdlib only** across the `eoltracker/` package (`boto3` is imported lazily
  inside the S3/SNS/SES paths in `eoltracker/notify.py` and
  `eoltracker/handler.py`). No third-party dependencies.
- **Config files: ASCII or UTF-8.** Config loading decodes bytes explicitly
  (never locale-dependent): pure ASCII is always accepted (ASCII is a UTF-8
  subset), UTF-8 with or without a BOM is accepted — hand-edited configs may
  contain UTF-8 characters — and any other encoding is rejected with a clear
  re-save-as-UTF-8 error, so the old cp1252 (Windows) locale hazard is gone.
  **Every load — local or S3 — also enforces a bounded size and JSON-nesting
  depth** (`MAX_CONFIG_FILE_BYTES` / `MAX_CONFIG_DEPTH` in `eoltracker/core.py`,
  mirrored by `helper_scripts/eol_inventory/config_io.py`), rejecting
  oversized or over-nested input with a clear error before recursive JSON
  parsing so a hostile or malformed config cannot raise `RecursionError` or
  exhaust memory. **Every load — local or S3 — enforces the
  `eoltracker.validation` schema**: invalid top-level or runtime shapes
  (`products` container, `alert_thresholds_days`, `notify_when`, notification
  channels) are rejected before any provider call. Malformed individual
  product entries do not abort the run; they become `error` rows while valid
  products continue.
  `json.dump(..., ensure_ascii=True)` (the default) keeps generated configs
  pure ASCII.
- **`eol_config.*.json` and `reports/` are gitignored** (except
  `eol_config.sample.json`, the template). Per-project configs and generated
  reports (tracker reports under `reports/<project>/...`, inventory reports
  under `reports/inventory/`) are local artifacts.
- **Scanner metadata keys are ignored by the runtime.** Like `_comment` and
  `_section`, `_found_in` (per-entry provenance) and `_inventory` (scan
  metadata, warnings, unmapped items) are underscore-prefixed and skipped by
  the Lambda; keep them intact so inventory reports stay accurate.
  `_inventory.scan_date` (`helper_scripts/eol_inventory/config_writer.py`) is
  the scan's `date.today().isoformat()` at write time, read back by the
  inventory report writer.
- **Reports** land in `reports/<project>/<year>/<month>/<day>/`; `<project>`
  derives from the `html_file` `path` base name (`eol_report_a.html` → `a`,
  plain `eol_report.html` → `default`).
- **Delivery outcomes (R-03/R-12 contract).** Every notify channel returns an
  attempted/delivered/skipped/error outcome record; the handler's `notified`
  field reflects actual delivery, and in Lambda mode it raises
  `DeliveryFailureError` when every required channel is undelivered (SNS/SES
  required by default; console/html optional unless explicitly overridden;
  Lambda
  retries, function DLQ, and CloudWatch ops alarms are wired in
  `terraform/main.tf`). `html_file` writes relative paths only locally; inside
  Lambda it skips unless given an explicit absolute path under `/tmp`.
  Recipient addresses must never appear in logs or outcome details.
- **Testing:** no framework — tests are standalone `python` assertion scripts
  that import the relevant `eoltracker` modules and inject synthetic data to
  stay network-free. Guard/integrity scripts (`python tests/check_*.py`) are
  the same style but check the repository itself rather than a module:
  `tests/check_agent_docs.py` verifies agent-facing docs and skills stay
  consistent, and `tests/check_test_registration.py` parses every
  `tests/test_*.py` with `ast` (never imports it) to catch a module-level
  `def test_*` defined twice (shadowed — only the last definition runs) or,
  in files with a `TESTS = [...]` list, never added to that list
  (unregistered — never runs); run it with `--self-test` to exercise its own
  detector against synthetic fixtures.
  `tests/test_generator_parity.py` — temporary: both generators over the
  shared fixtures; removed with the root script.
- **Run locally:** `python lambda_function.py <config.json>`, or `./run.sh` /
  `.\run.ps1` (interactive config picker).
- **`policy_note`** (optional, any config entry) is a short ASCII observation of
  a product's release/support policy (config files themselves may be UTF-8). `check_product` copies it onto the result
  and both formatters render it as a muted sub-line (HTML: a `&#9432;` marker;
  text: `Policy:`). Use it for no-EOL-date platform/infra items where a blank
  EOL date is misleading.

## Key files

| Path | Purpose |
|---|---|
| `AGENTS.md` | This file — the canonical guide for AI agents in any harness |
| `CLAUDE.md` | Thin Claude Code discovery aliases for canonical `.agents` skills |
| `lambda_function.py` | Shim: re-exports `lambda_handler` (the Lambda entry point) + the local CLI (`run_local`) |
| `eoltracker/core.py` | Shared primitives: `logger`, `parse_date_field`, `_error_result`, the two HTML table parsers, and the bounded-JSON/HTTP helpers `validate_bounded_json`, `read_response_bytes`, `decompress_gzip_bytes` |
| `eoltracker/parsers/` | One file per provider + `eoltracker/parsers/__init__.py` auto-registration (`PROVIDERS`, `SOURCE_LABELS`, `source_url_for`, `check_product`) |
| `eoltracker/report.py` | Categorizer + plain-text and HTML formatters |
| `eoltracker/notify.py` | Notification channels (console / html_file / SNS / SES) |
| `eoltracker/runner.py` | Bounded-concurrency check execution, per-invocation lookup dedupe, and the Lambda time-budget reserve (R-04) |
| `eoltracker/handler.py` | Config loading, `lambda_handler`, and `run_local` (local CLI body) |
| `eol_config.<project>.json` | Per-project product lists (gitignored; `eol_config.sample.json` is the template) |
| `eol_config_generation_prompt.md` | The canonical config-generation/extraction spec |
| `generate_config.py` | Root-level manifest extractor CLI (one of two coexisting generators — see "Workflows index"); extended by open PR #35 |
| `helper_scripts/` | Second, multi-ecosystem dependency scanner + inventory report CLIs, wrappers, and the `eol_inventory` package (provenance, curation-preserving `--update`, container scanning, `helper_scripts/eol_inventory/redact.py`, `helper_scripts/eol_inventory/config_io.py`); see `helper_scripts/README.md` |
| `docs/adding-a-provider.md` | Step-by-step guide to adding (and repairing) a provider |
| `docs/updating-a-config.md` | Curation-preserving config refresh workflow |
| `docs/commit-conventions.md` | Batch boundaries, safe staging, and commit-message standard |
| `terraform/README.md` | Provider pinning + dependency-lock update workflow, S3 config rollback runbook |
| `build_lambda_package.py` | Builds the allowlisted Lambda artifact + manifest (`terraform/build/`, gitignored) and verifies it offline; run `python build_lambda_package.py build` after runtime changes |
| `docs/packaging.md` | Packaging allowlist, manifest verification, and Terraform preconditions |
| `terraform/` | Deployment (deploys the prebuilt, precondition-checked artifact from `terraform/build/`) |
| `docs/agents/` | Per-repo config the engineering skills read: issue tracker, triage labels, domain docs |

## Agent skills

### Issue tracker

Issues live as GitHub issues on `cheokjiade/end-of-life-tracker`, operated via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) with no renames. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: read CONTEXT.md (created lazily by the grill-with-docs skill) + `docs/adr/` before exploring — proceed silently if absent; `AGENTS.md` is the de facto context doc until then. See `docs/agents/domain.md`.
