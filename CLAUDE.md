# CLAUDE.md

Guidance for AI agents (Claude Code) working in this repository.

## What this is

An AWS Lambda that checks software **end-of-life (EOL)** status across multiple data
sources and reports via console / HTML file / SNS / SES. The runtime is a **stdlib-only**
Python package — `eoltracker/` — with a thin `lambda_function.py` shim that re-exports the
handler (preserving the `lambda_function.lambda_handler` entry point); Terraform packages
both into the deployment zip.

## Package layout

```
lambda_function.py        # shim: re-exports lambda_handler; __main__ runs run_local
eoltracker/
  core.py                 # logger, parse_date_field, _error_result, the two HTML table parsers
  parsers/                # one file per provider + __init__.py (auto-registration + dispatch)
  report.py               # _categorise + plain-text and HTML formatters
  notify.py               # notification channels
  handler.py              # config loading, lambda_handler, run_local
```

## Architecture: providers (the "parsers")

The core is a **provider/registry plugin pattern**. Each data source is a *provider* — a
function with a uniform contract:

```python
def _provider_<name>(entry, today) -> dict   # a normalized result dict
```

- **Dispatch:** `check_product(entry, today)` reads `entry["source"]`, looks it up in the
  `PROVIDERS` registry (defaults to `endoflife_date`), and calls it. Entries carrying a
  `_section` marker return `None` (they are config-file dividers, not products).
- **Uniform result shape:** every provider returns the same dict keys (`label`, `product`,
  `version`, `status`, `message`, `eol_date`, `days_remaining`, `latest_patch`, `source`,
  …) so both formatters (`format_report_text`, `format_report_html`) consume any provider
  unchanged.
- **Shared contract helpers:** in `eoltracker/core.py` — `_error_result(entry, msg)`
  (uniform error shape), `parse_date_field` (date/bool/None), and the reusable HTML parsers
  `_HtmlTableExtractor` (single-table pages) and `_AWSCalendarParser` (heading-anchored,
  multi-table pages). `_categorise` (bucket by status) lives in `eoltracker/report.py`.
- **Status values:** `eol`, `approaching`, `ok`, `error`, `unknown`, `untracked`.
  `_categorise` buckets them; note `approaching` requires `days_remaining <=
  max(thresholds)`, else it falls to `ok` (so a far-future EOL is informational, not an
  alert).

**Modularity:** each provider is its own file under `eoltracker/parsers/`, **auto-registered**
at import time (`parsers/__init__.py` scans the package) — adding one is localized and touches
no other provider: drop in a new file, no registry edits. Current providers (8):
`endoflife_date`, `aws_rds_scrape`, `aws_sdk_lifecycle`, `jackson_lifecycle`, `maven_central`,
`npm_registry`, `manual`, `tyk_lifecycle`.

## Adding a data-source provider (parser)

The full how-to with a copy-paste skeleton is in **`docs/adding-a-provider.md`**. In brief:

1. **Write the provider module** as a new file `eoltracker/parsers/<name>.py`: a cached
   fetch/scrape helper, a *pure* parse helper (so logic is testable without network), and
   `def _provider_<name>(entry, today):` that returns the normalized dict (or
   `_error_result(entry, msg)` with `result["source"] = "<name>"` on failure). Import shared
   helpers from `..core`.
2. **Register via module attributes** (auto-discovered — no registry edits): set `SOURCE`,
   `LABEL`, `provider = _provider_<name>`, and an optional `url_for(r)` for the upstream link.
3. **Defensive parsing** (match `aws_rds_scrape` / `jackson_lifecycle`): required-header
   checks, a row-count floor, and/or a hardcoded canary — fail loudly on page drift rather
   than emit silently-wrong dates.
4. **New status?** If you add one (as `untracked` was), update `_categorise` (bucket +
   return tuple) AND both formatters (unpack + rendering), plus `_STATUS_COLOURS` /
   `_status_label` for HTML — all in `eoltracker/report.py`.
5. **Test network-free**, then one live smoke run (`python lambda_function.py <config>`).
6. **Document it** in `eol_config_generation_prompt.md` (providers table + entry shape +
   decision order) so config generation uses it.

## Agent-driven development

- **Config generation is agent-driven.** `.claude/agents/eol-config-extractor.md` is a
  reusable subagent that converts a software inventory (dependency manifest, Confluence /
  wiki EOL table, spreadsheet, prose) into a validated `eol_config.<project>.json`,
  verifying every slug/package against live APIs before writing. The human-pasteable
  equivalent is `eol_config_generation_prompt.md`.
- **Prefer automation over manual.** Check endoflife.date first for commercial/infra
  software (Splunk, MongoDB, Jenkins, RHEL, … are all there); use a scraper provider where
  one fits (e.g. Tyk); reserve `manual` for things with no automated source anywhere
  (PuTTY, OpenSSH's own schedule). A live source stays current; a hardcoded manual date
  rots.
- **Verify, don't fabricate.** A wrong endoflife.date slug/cycle becomes a broken `error`
  row on every run. Confirm cycles/packages live before writing.
- **Larger changes** follow a brainstorm → spec → plan → subagent-driven execution flow;
  specs and plans live under `docs/superpowers/`.

## Conventions & gotchas

- **Stdlib only** across the `eoltracker/` package (`boto3` is imported lazily inside the
  S3/SNS/SES paths in `eoltracker/notify.py` and `eoltracker/handler.py`). No third-party
  dependencies.
- **Keep configs ASCII.** `load_config_from_file` opens with no explicit encoding, so on
  cp1252 (Windows) systems non-ASCII characters break the read. `json.dump(...,
  ensure_ascii=True)` (the default) keeps generated configs safe.
- **`eol_config.*.json` and `reports/` are gitignored** (except `eol_config.sample.json`,
  the template). Per-project configs and generated reports are local artifacts.
- **Reports** land in `reports/<project>/<year>/<month>/<day>/`; `<project>` derives from
  the `html_file` `path` base name (`eol_report_a.html` → `a`, plain `eol_report.html`
  → `default`).
- **Testing:** no framework — tests are standalone `python` assertion scripts that import
  the relevant `eoltracker` modules (e.g. `eoltracker.parsers.<name>`, `eoltracker.report`)
  and inject synthetic data to stay network-free.
- **Run locally:** `python lambda_function.py <config.json>`, or `./run.sh` / `.\run.ps1`
  (interactive config picker).
- **`policy_note`** (optional, any config entry) is a short ASCII observation of a
  product's release/support policy. `check_product` copies it onto the result and both
  formatters render it as a muted sub-line (HTML: a `&#9432;` marker; text: `Policy:`).
  Use it for no-EOL-date platform/infra items where a blank EOL date is misleading.

## Key files

| Path | Purpose |
|---|---|
| `lambda_function.py` | Shim: re-exports `lambda_handler` (the Lambda entry point) + the local CLI (`run_local`) |
| `eoltracker/core.py` | Shared primitives: `logger`, `parse_date_field`, `_error_result`, the two HTML table parsers |
| `eoltracker/parsers/` | One file per provider + `__init__.py` auto-registration (`PROVIDERS`, `SOURCE_LABELS`, `source_url_for`, `check_product`) |
| `eoltracker/report.py` | Categorizer + plain-text and HTML formatters |
| `eoltracker/notify.py` | Notification channels (console / html_file / SNS / SES) |
| `eoltracker/handler.py` | Config loading, `lambda_handler`, and `run_local` (local CLI body) |
| `eol_config.<project>.json` | Per-project product lists (gitignored; `eol_config.sample.json` is the template) |
| `eol_config_generation_prompt.md` | The config-generation prompt (8 providers, mapping rules, real-world patterns) |
| `.claude/agents/eol-config-extractor.md` | The reusable extraction subagent |
| `generate_config.py` | Static dependency-manifest → config generator (Maven/Gradle/npm) |
| `docs/adding-a-provider.md` | Step-by-step guide to adding a provider |
| `terraform/` | Deployment (packages `lambda_function.py` + `eoltracker/` as a zip) |
