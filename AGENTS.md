# AGENTS.md

Canonical guidance for AI coding agents — any harness — working in this
repository. Authoritative reusable workflows live under `.agents/skills/`;
`CLAUDE.md` contains only the aliases Claude Code needs for discovery.

## What this is

An AWS Lambda that checks software **end-of-life (EOL)** status across multiple
data sources and reports via console / HTML file / SNS / SES. The runtime is a
**stdlib-only** Python package — `eoltracker/` — with a thin
`lambda_function.py` shim that re-exports the handler (preserving the
`lambda_function.lambda_handler` entry point); Terraform packages both into the
deployment zip.

## Workflows index

| I want to... | Read / run |
|---|---|
| Generate or update a config with an AI coding agent | Invoke `manage-eol-config` in Codex/OpenCode or `eol-config` in Claude Code; canonical instructions: `.agents/skills/manage-eol-config/SKILL.md` |
| Generate a config from dependency manifests (`pom.xml`, `*.gradle*`, package.json) | `python generate_config.py <folder> --name <project>`, then live-verify (norms below) |
| Generate a config from messy inputs (wiki/Confluence tables, spreadsheets, prose) | Follow the extraction spec in `eol_config_generation_prompt.md` |
| Update an existing config after upgrades or inventory changes | `docs/updating-a-config.md` |
| Add a new data-source provider | Invoke `add-eol-provider` in Codex/OpenCode or `eol-provider` in Claude Code; canonical instructions: `.agents/skills/add-eol-provider/SKILL.md` |
| Repair a provider whose upstream page drifted | Use the same provider skill; canonical repair procedure: `docs/adding-a-provider.md` |
| Make and commit repository changes | Follow `docs/commit-conventions.md`; commit each completed, verified batch |

Universal norms, whichever workflow you are in:

- **Verify, don't fabricate.** Confirm endoflife.date slugs/cycles and npm/Maven
  packages against the live APIs before writing them into a config. A wrong
  string becomes a broken `error` row on every future run.
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
  core.py                 # logger, parse_date_field, _error_result, the two HTML table parsers
  parsers/                # one file per provider + __init__.py (auto-registration + dispatch)
  report.py               # _categorise + plain-text and HTML formatters
  notify.py               # notification channels
  handler.py              # config loading, lambda_handler, run_local
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
  dividers, not products).
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
  `untracked`. `_categorise` buckets them; note `approaching` requires
  `days_remaining <= max(thresholds)`, else it falls to `ok` (so a far-future
  EOL is informational, not an alert).

**Modularity:** each provider is its own file under `eoltracker/parsers/`,
**auto-registered** at import time (`eoltracker/parsers/__init__.py` scans the
package) —
adding one is localized and touches no other provider: drop in a new file, no
registry edits. Current providers (8): `endoflife_date`, `aws_rds_scrape`,
`aws_sdk_lifecycle`, `jackson_lifecycle`, `maven_central`, `npm_registry`,
`manual`, `tyk_lifecycle`.

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
  schema, the 8 providers' entry shapes, the input-to-entry mapping decision
  order, and real-world document patterns (strikethrough = skip, "was X now Y" =
  current version, multi-version cells, reference-URL slug hints). Any agent in
  any harness can follow it directly.
- `generate_config.py` is the deterministic scanner for clean dependency
  manifests (Maven / Gradle / npm) — no LLM required.
- `docs/updating-a-config.md` is the refresh workflow: diff new evidence against
  the existing config and patch it, preserving human curation. Never regenerate
  an existing config wholesale.

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
- **Keep configs ASCII.** `load_config_from_file` opens with no explicit
  encoding, so on cp1252 (Windows) systems non-ASCII characters break the read.
  `json.dump(..., ensure_ascii=True)` (the default) keeps generated configs
  safe.
- **`eol_config.*.json` and `reports/` are gitignored** (except
  `eol_config.sample.json`, the template). Per-project configs and generated
  reports are local artifacts.
- **Reports** land in `reports/<project>/<year>/<month>/<day>/`; `<project>`
  derives from the `html_file` `path` base name (`eol_report_a.html` → `a`,
  plain `eol_report.html` → `default`).
- **Testing:** no framework — tests are standalone `python` assertion scripts
  that import the relevant `eoltracker` modules and inject synthetic data to
  stay network-free.
- **Run locally:** `python lambda_function.py <config.json>`, or `./run.sh` /
  `.\run.ps1` (interactive config picker).
- **`policy_note`** (optional, any config entry) is a short ASCII observation of
  a product's release/support policy. `check_product` copies it onto the result
  and both formatters render it as a muted sub-line (HTML: a `&#9432;` marker;
  text: `Policy:`). Use it for no-EOL-date platform/infra items where a blank
  EOL date is misleading.

## Key files

| Path | Purpose |
|---|---|
| `AGENTS.md` | This file — the canonical guide for AI agents in any harness |
| `CLAUDE.md` | Thin Claude Code discovery aliases for canonical `.agents` skills |
| `lambda_function.py` | Shim: re-exports `lambda_handler` (the Lambda entry point) + the local CLI (`run_local`) |
| `eoltracker/core.py` | Shared primitives: `logger`, `parse_date_field`, `_error_result`, the two HTML table parsers |
| `eoltracker/parsers/` | One file per provider + `eoltracker/parsers/__init__.py` auto-registration (`PROVIDERS`, `SOURCE_LABELS`, `source_url_for`, `check_product`) |
| `eoltracker/report.py` | Categorizer + plain-text and HTML formatters |
| `eoltracker/notify.py` | Notification channels (console / html_file / SNS / SES) |
| `eoltracker/handler.py` | Config loading, `lambda_handler`, and `run_local` (local CLI body) |
| `eol_config.<project>.json` | Per-project product lists (gitignored; `eol_config.sample.json` is the template) |
| `eol_config_generation_prompt.md` | The canonical config-generation/extraction spec |
| `generate_config.py` | Static dependency-manifest → config generator (Maven/Gradle/npm) |
| `docs/adding-a-provider.md` | Step-by-step guide to adding (and repairing) a provider |
| `docs/updating-a-config.md` | Curation-preserving config refresh workflow |
| `docs/commit-conventions.md` | Batch boundaries, safe staging, and commit-message standard |
| `terraform/` | Deployment (packages `lambda_function.py` + `eoltracker/` as a zip) |
