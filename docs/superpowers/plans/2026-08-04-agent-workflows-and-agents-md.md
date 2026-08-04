# Agent Workflows & Canonical AGENTS.md Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the config-refresh and provider-maintenance workflows as harness-neutral docs + thin Claude skills, and make `AGENTS.md` the canonical repo guide with `CLAUDE.md` as a thin Claude-specific pointer.

**Architecture:** Docs-canonical + thin wrappers. Workflow knowledge lives in `docs/*.md` and `eol_config_generation_prompt.md`; `.claude/skills/*/SKILL.md` are ~40-line invocable wrappers that point at those docs; the existing `eol-config-extractor` subagent gains an update mode that follows the new `docs/updating-a-config.md`. `AGENTS.md` (new) holds the shared architecture/conventions content migrated from `CLAUDE.md` plus a workflows-index routing table.

**Tech Stack:** Markdown docs; Claude Code skill/agent file formats (YAML frontmatter); stdlib-only Python for verification scripts. No changes to `eoltracker/` runtime.

**Spec:** `docs/superpowers/specs/2026-08-04-agent-workflows-and-agents-md-design.md`

## Global Constraints

- Work on branch `feature/agent-workflows`. Commit after each task. **NEVER push to remote** (user instruction).
- Do NOT modify anything under `eoltracker/`, `lambda_function.py`, `generate_config.py`, or `terraform/`.
- New docs/skills: plain ASCII where practical; the em-dash/arrow style already used in repo docs is fine. Configs (not touched here) must stay ASCII.
- Skill files live at `.claude/skills/<name>/SKILL.md` with YAML frontmatter containing `name` and `description`.
- No placeholder text ("TBD", "TODO") in any deliverable.
- Run commands from the repo root `E:\Git\endoflife` with `python` (3.x, stdlib only).

---

### Task 1: `docs/updating-a-config.md` — the config-refresh workflow

**Files:**
- Create: `docs/updating-a-config.md`

**Interfaces:**
- Produces: the doc path `docs/updating-a-config.md` and its rule names ("Never regenerate wholesale", the added/version-changed/removed/unchanged diff categories, the curation-preservation list). Tasks 3, 4, and 5 reference this path; Task 5 references the diff categories in the agent's update-mode section.

- [ ] **Step 1: Verify the file does not yet exist (failing check)**

Run: `python -c "from pathlib import Path; assert not Path('docs/updating-a-config.md').exists(), 'already exists'; print('missing as expected')"`
Expected: `missing as expected`

- [ ] **Step 2: Write the doc**

Create `docs/updating-a-config.md` with exactly this content:

````markdown
# Updating an existing config

How to refresh an `eol_config.<project>.json` after the tracked project upgrades
software, adds components, or retires them — without losing the human curation
the config has accumulated. This is the canonical workflow for any agent or
human, in any harness.

**Never regenerate wholesale.** A regenerated config loses `_comment`
provenance, `policy_note`s, `_section` grouping, and `manual` entries.
Update = diff + patch.

## Inputs

- The existing config: `eol_config.<project>.json` (the baseline).
- Fresh evidence of what the project runs now — one of:
  - **Dependency manifests** (`pom.xml`, `*.gradle*`, `package.json`): scan with
    `python generate_config.py <folder> --name <project>-scan --output <scratch-file>`
    and use the scratch output as the extracted inventory (do not overwrite the
    real config with it).
  - **Documents** (wiki/Confluence exports, spreadsheets, prose): extract an
    inventory per the spec in `eol_config_generation_prompt.md` (mapping decision
    order, strikethrough/"was X now Y"/multi-version-cell rules).
  - **Nothing new**: a pure re-verification pass — skip to step 5 and re-check
    every entry.

## Workflow

1. **Load the baseline.** Parse the existing config. Inventory every real entry
   (skip `_section` dividers): its `source`, identifying fields (`product` +
   `version`, `package`, `group`/`artifact`, `sdk`/`major`, `engine`, or `label`
   for `manual`), and its curation fields (`_comment`, `policy_note`, section).
2. **Extract the current inventory** from the fresh inputs (see Inputs above).
3. **Diff** baseline vs current, component by component:
   - **Added** — in the inputs, not in the config → draft a new entry following
     the mapping decision order in `eol_config_generation_prompt.md`; place it in
     the matching `_section`.
   - **Version-changed** — same component, different version → update `version`
     (re-derive the cycle string at the granularity the provider needs) and
     `label`; refresh the `_comment` provenance to cite the new evidence and note
     the old version ("was 3.3.4").
   - **Removed** — in the config, absent from the inputs → remove ONLY with
     explicit evidence: strikethrough, a "decommissioned"/"migrated away" note,
     or absence from an authoritative manifest that previously declared it.
     Documents are often partial — absence from a partial input is NOT evidence.
     When unsure, keep the entry and flag it in the report.
   - **Unchanged** — keep the entry byte-identical, curation intact.
4. **Preserve curation** on every kept or updated entry:
   - `_comment` provenance: update it, never delete it.
   - `policy_note`: keep unless the policy claim itself changed upstream.
   - `_section` grouping: new entries join the closest existing section; create a
     new section only when none fits.
   - `manual` entries: they exist precisely because no automated source does —
     no manifest will ever mention them, so an update never drops them. At most,
     refresh their `eol_date` against their `reference_url`.
5. **Verify live — only the added and version-changed entries** (a pure
   re-verification pass checks all entries). Use one batched stdlib script per
   source, as shown in the "Verification Checklist" section of
   `eol_config_generation_prompt.md`: confirm each endoflife.date slug + exact
   `cycle`, each npm `package` (+ `version`) resolves on `registry.npmjs.org`,
   each Maven `group:artifact` resolves on `search.maven.org`. Anything
   unverifiable is flagged, not guessed.
6. **Validate + smoke-run:**
   `python -c "import json; json.load(open('eol_config.<project>.json'))"`, then
   `python lambda_function.py eol_config.<project>.json` — confirm no new
   `error` rows appear that were not already in the baseline report.
7. **Report the diff.** Counts plus per-entry lists: added / version-changed /
   removed (each removal with its evidence) / kept-but-flagged. This report is
   the deliverable a reviewer checks before the config is deployed.

## Rules

- ASCII only — `load_config_from_file` reads with the platform default encoding,
  so non-ASCII breaks on cp1252 systems.
- Strictly valid JSON: no comments, no trailing commas; use `_comment` /
  `_section` fields instead.
- Do not deduplicate distinct majors (Java 8 and Java 17 stay two entries).
- Never turn an automated entry (`endoflife_date`, scraper, registry) into a
  `manual` entry just because verification was inconvenient — a live source
  stays current; a hardcoded date rots.
````

- [ ] **Step 3: Verify content landed**

Run: `python -c "from pathlib import Path; t = Path('docs/updating-a-config.md').read_text(encoding='utf-8'); assert 'Never regenerate wholesale' in t; assert '## Workflow' in t and '## Rules' in t; assert 'Version-changed' in t and 'absence from a partial input is NOT evidence' in t; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add docs/updating-a-config.md
git commit -m "docs: add updating-a-config workflow (curation-preserving refresh)"
```

---

### Task 2: "Repairing a broken provider" section in `docs/adding-a-provider.md`

**Files:**
- Modify: `docs/adding-a-provider.md` (append a new section between "## Document the provider" and "## Worked example: `tyk_lifecycle`")

**Interfaces:**
- Consumes: the existing doc's section layout (headings "## Document the provider" and "## Worked example: `tyk_lifecycle`").
- Produces: the exact heading `## Repairing a broken provider`, referenced by Task 3 (AGENTS.md workflows index) and Task 4 (`add-eol-provider` skill).

- [ ] **Step 1: Failing check for the heading**

Run: `python -c "from pathlib import Path; t = Path('docs/adding-a-provider.md').read_text(encoding='utf-8'); assert '## Repairing a broken provider' not in t; print('missing as expected')"`
Expected: `missing as expected`

- [ ] **Step 2: Insert the section**

In `docs/adding-a-provider.md`, insert the following between the "## Document the provider" section and the "## Worked example: `tyk_lifecycle`" heading:

````markdown
## Repairing a broken provider

Scraper providers fail loudly by design: when an upstream page drifts, the report
grows `error` rows ("source may have changed", a canary assertion, a row-count
floor) instead of silently wrong dates. When that happens:

1. **Reproduce.** Run the failing provider directly (network on) and read the
   actual error — canary failure, row-count floor, missing header, HTTP error:

   ```python
   import sys; sys.path.insert(0, r"E:\Git\endoflife")
   from datetime import date
   from eoltracker.parsers import tyk_lifecycle as mod   # the broken module
   print(mod.provider({"source": mod.SOURCE, "version": "5.8"}, date.today()))
   ```

2. **Fetch the raw source** the provider parses (the URL constant at the top of
   the module) and save it to a scratch file. Compare its structure against what
   the pure `_parse_*` helper expects: headers, column order, section headings,
   markdown vs rendered HTML.
3. **Fix the pure parse helper** against the saved raw text. Keep it pure — no
   network — so the fix is testable offline.
4. **Keep the defensive checks.** Never delete a canary or lower a row-count
   floor just to silence the error. Update the canary only when the upstream
   fact legitimately changed (e.g. a version's EOL date was revised upstream),
   and say so in the commit message.
5. **Re-run the module's network-free test script** (synthetic raw text +
   injected cache, as in "Test it (network-free)" above), adding a regression
   case built from the new page shape.
6. **One live smoke run:** `python lambda_function.py <config using the source>`
   — confirm the `error` rows are gone.

If the upstream source is gone for good (page deleted, product discontinued),
migrate the affected config entries to another provider or `manual`, and update
`eol_config_generation_prompt.md` so config generation stops recommending the
dead source.
````

- [ ] **Step 3: Fix the root-relative registry path**

In the same file's "## Register (module attributes — auto-discovered)" section, change `` `parsers/__init__.py` scans the package `` to `` `eoltracker/parsers/__init__.py` scans the package `` — the current form does not resolve from the repo root, and the Task 6 integrity checker verifies backticked paths.

- [ ] **Step 4: Verify insertion and ordering**

Run: `python -c "from pathlib import Path; t = Path('docs/adding-a-provider.md').read_text(encoding='utf-8'); i = t.index('## Document the provider'); j = t.index('## Repairing a broken provider'); k = t.index('## Worked example'); assert i < j < k; assert 'Never delete a canary' in t; assert 'eoltracker/parsers/__init__.py' in t; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add docs/adding-a-provider.md
git commit -m "docs: add 'Repairing a broken provider' to adding-a-provider"
```

---

### Task 3: `AGENTS.md` canonical guide + thin `CLAUDE.md`

**Files:**
- Create: `AGENTS.md`
- Rewrite: `CLAUDE.md` (full replacement)

**Interfaces:**
- Consumes: `docs/updating-a-config.md` (Task 1), `## Repairing a broken provider` heading (Task 2).
- Produces: `AGENTS.md` with a `## Workflows index` section; the thin `CLAUDE.md` naming the three skills (`generate-eol-config`, `update-eol-config`, `add-eol-provider`) that Task 4 creates and the update mode Task 5 adds. (Forward references to Task 4/5 deliverables are intentional; Task 6 verifies they all resolve.)

- [ ] **Step 1: Failing check**

Run: `python -c "from pathlib import Path; assert not Path('AGENTS.md').exists(); print('missing as expected')"`
Expected: `missing as expected`

- [ ] **Step 2: Write `AGENTS.md`**

Create `AGENTS.md` with exactly this content:

````markdown
# AGENTS.md

Canonical guidance for AI coding agents — any harness — working in this
repository. Claude Code layers its own entry points (a subagent and skills) on
top of this file; those live in `CLAUDE.md`.

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
| Generate a config from dependency manifests (`pom.xml`, `*.gradle*`, `package.json`) | `python generate_config.py <folder> --name <project>`, then live-verify (norms below) |
| Generate a config from messy inputs (wiki/Confluence tables, spreadsheets, prose) | Follow the extraction spec in `eol_config_generation_prompt.md` |
| Update an existing config after upgrades or inventory changes | `docs/updating-a-config.md` |
| Add a new data-source provider | `docs/adding-a-provider.md` |
| Repair a provider whose upstream page drifted | "Repairing a broken provider" in `docs/adding-a-provider.md` |

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
- Claude Code additionally packages these as a subagent
  (`.claude/agents/eol-config-extractor.md`) and skills — see `CLAUDE.md`.

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
| `CLAUDE.md` | Claude-specific entry points (subagent + skills) layered on this file |
| `lambda_function.py` | Shim: re-exports `lambda_handler` (the Lambda entry point) + the local CLI (`run_local`) |
| `eoltracker/core.py` | Shared primitives: `logger`, `parse_date_field`, `_error_result`, the two HTML table parsers |
| `eoltracker/parsers/` | One file per provider + `__init__.py` auto-registration (`PROVIDERS`, `SOURCE_LABELS`, `source_url_for`, `check_product`) |
| `eoltracker/report.py` | Categorizer + plain-text and HTML formatters |
| `eoltracker/notify.py` | Notification channels (console / html_file / SNS / SES) |
| `eoltracker/handler.py` | Config loading, `lambda_handler`, and `run_local` (local CLI body) |
| `eol_config.<project>.json` | Per-project product lists (gitignored; `eol_config.sample.json` is the template) |
| `eol_config_generation_prompt.md` | The canonical config-generation/extraction spec |
| `generate_config.py` | Static dependency-manifest → config generator (Maven/Gradle/npm) |
| `docs/adding-a-provider.md` | Step-by-step guide to adding (and repairing) a provider |
| `docs/updating-a-config.md` | Curation-preserving config refresh workflow |
| `terraform/` | Deployment (packages `lambda_function.py` + `eoltracker/` as a zip) |
````

- [ ] **Step 3: Replace `CLAUDE.md`**

Replace the entire content of `CLAUDE.md` with exactly:

````markdown
# CLAUDE.md

Read **`AGENTS.md`** first — it is the canonical guide to this repo (what it is,
package layout, provider architecture, conventions and gotchas, and the
workflows index). This file adds only the Claude-specific entry points layered
on top of it.

## Subagent

- `.claude/agents/eol-config-extractor.md` — dispatch it to turn inventory
  inputs (dependency manifests, Confluence/wiki EOL tables, spreadsheets, prose)
  into a validated `eol_config.<project>.json`. Give it the input file path(s)
  and the project name. **Update mode:** also give it the path of an existing
  config and it diff-and-refreshes per `docs/updating-a-config.md` instead of
  writing fresh.

## Skills (`.claude/skills/`)

- `generate-eol-config` — routes config generation: clean manifests →
  `generate_config.py`; messy/mixed inputs → the extractor subagent.
- `update-eol-config` — refresh an existing config (wraps
  `docs/updating-a-config.md`).
- `add-eol-provider` — add or repair a data-source provider (wraps
  `docs/adding-a-provider.md`).

## Development flow

- Larger changes follow brainstorm → spec → plan → subagent-driven execution;
  specs and plans live under `docs/superpowers/`.
- Keep `AGENTS.md` authoritative: when architecture, conventions, or workflows
  change, update `AGENTS.md` (not this file) and keep this file a thin index.
````

- [ ] **Step 4: Verify both files**

Run: `python -c "from pathlib import Path; a = Path('AGENTS.md').read_text(encoding='utf-8'); c = Path('CLAUDE.md').read_text(encoding='utf-8'); assert '## Workflows index' in a and 'Current providers (8)' in a; assert Path('docs/updating-a-config.md').exists() and Path('docs/adding-a-provider.md').exists(); assert 'AGENTS.md' in c and len(c.splitlines()) < 45; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md CLAUDE.md
git commit -m "docs: make AGENTS.md the canonical guide; thin CLAUDE.md to Claude-specifics"
```

---

### Task 4: Three thin skills under `.claude/skills/`

**Files:**
- Create: `.claude/skills/generate-eol-config/SKILL.md`
- Create: `.claude/skills/update-eol-config/SKILL.md`
- Create: `.claude/skills/add-eol-provider/SKILL.md`

**Interfaces:**
- Consumes: `docs/updating-a-config.md` (Task 1), `## Repairing a broken provider` (Task 2), `AGENTS.md` norms (Task 3), the `eol-config-extractor` subagent (update mode arrives in Task 5 — forward reference is intentional).
- Produces: skill names `generate-eol-config`, `update-eol-config`, `add-eol-provider` exactly as listed in the thin `CLAUDE.md`.

- [ ] **Step 1: Failing check**

Run: `python -c "from pathlib import Path; assert not Path('.claude/skills').exists(); print('missing as expected')"`
Expected: `missing as expected`

- [ ] **Step 2: Write `generate-eol-config/SKILL.md`**

Create `.claude/skills/generate-eol-config/SKILL.md` with exactly:

````markdown
---
name: generate-eol-config
description: Generate an eol_config.<project>.json for the EOL tracker from inventory inputs. Use when the user wants to create a tracker config from dependency manifests (pom.xml, build.gradle, package.json), a wiki/Confluence EOL table, a spreadsheet, or a prose software list. Routes clean manifests to generate_config.py and messy or mixed inputs to the eol-config-extractor subagent.
---

# Generate an EOL config

Route by input type — do not extract by hand:

1. **Only clean dependency manifests** (`pom.xml` / `*.gradle*` / `package.json`
   in a folder): run the deterministic scanner, then verify:

   ```
   python generate_config.py <folder> --name <project>
   ```

   Review the `_skipped_npm_packages` list in the output, then live-verify the
   generated entries per the universal norms in `AGENTS.md`.

2. **Anything messier** (wiki/Confluence tables, spreadsheets, prose, or mixed
   manifest + document inputs): dispatch the `eol-config-extractor` subagent
   with the input file path(s) and the project name. It reads
   `eol_config_generation_prompt.md` (the canonical extraction spec), verifies
   every slug/package live, writes the config, and smoke-runs it.

3. **Updating an existing config?** Stop — use the `update-eol-config` skill
   instead. Regenerating from scratch destroys human curation.

Done when: the config parses (`python -c "import json;
json.load(open('eol_config.<project>.json'))"`), a smoke run
(`python lambda_function.py eol_config.<project>.json`) shows no unexpected
error rows, and the user has the verification report (entry counts, verified
checklist, Needs-Manual-Review list).
````

- [ ] **Step 3: Write `update-eol-config/SKILL.md`**

Create `.claude/skills/update-eol-config/SKILL.md` with exactly:

````markdown
---
name: update-eol-config
description: Refresh an existing eol_config.<project>.json after upgrades or inventory changes, preserving human curation (_comment provenance, policy_notes, _section grouping, manual entries). Use when the user wants to update, refresh, re-verify, or sync a tracker config against new manifests or documents — NOT regenerate it from scratch.
---

# Update an EOL config

Read `docs/updating-a-config.md` first — it is the canonical workflow. This
skill only frames the checklist:

1. Identify the baseline (`eol_config.<project>.json`) and the fresh inputs:
   manifests, documents, or none (none = pure re-verification pass).
2. Dispatch the `eol-config-extractor` subagent in **update mode**: give it the
   existing config path, the input path(s), and the project name.
3. Review the agent's diff report. Every **removal** must cite explicit
   evidence (strikethrough / "decommissioned" / dropped from an authoritative
   manifest that previously declared it). Absence from a partial document is
   not evidence.
4. Confirm the smoke run passed and curation survived: spot-check that
   `policy_note`s, `_comment` provenance, and `manual` entries are still
   present in the updated file.

Never regenerate wholesale — that destroys human curation. If the user actually
wants a brand-new config, use the `generate-eol-config` skill instead.
````

- [ ] **Step 4: Write `add-eol-provider/SKILL.md`**

Create `.claude/skills/add-eol-provider/SKILL.md` with exactly:

````markdown
---
name: add-eol-provider
description: Add a new data-source provider (parser) to the EOL tracker, or repair an existing one whose upstream page drifted (error rows, canary failures, "source may have changed"). Use when a new lifecycle data source is needed or a scraper provider is failing.
---

# Add or repair a provider

Read `docs/adding-a-provider.md` first — it is the canonical how-to (contract,
copy-paste skeleton, defensive-parsing bar, auto-registration, tests). This
skill only frames the checklist.

**Adding a provider:**

1. New file `eoltracker/parsers/<name>.py` from the doc's skeleton: cached
   fetch, pure `_parse_*` helper, `_provider_<name>(entry, today)`.
2. Register via module attributes (`SOURCE`, `LABEL`, `provider`, optional
   `url_for`) — auto-discovered; no registry edits anywhere.
3. Defensive parsing: required-header check, row-count floor, canary — fail
   loudly on page drift.
4. Network-free test script (synthetic raw text + injected cache + registration
   asserts), then one live smoke run.
5. Document it in `eol_config_generation_prompt.md` (providers table + entry
   shape + mapping decision order) and update the provider count/list in
   `AGENTS.md`.

**Repairing a provider:** follow "Repairing a broken provider" in
`docs/adding-a-provider.md` — reproduce, fetch the raw source, fix the pure
parse helper, keep the defensive checks (never delete a canary or lower a floor
to silence an error), retest network-free, one live smoke run.

Done when: tests pass network-free, a live smoke run is clean, and the docs
above mention the provider.
````

- [ ] **Step 5: Verify frontmatter and content**

Run: `python -c "import re; from pathlib import Path; paths = sorted(Path('.claude/skills').glob('*/SKILL.md')); assert len(paths) == 3, paths; [(_ for _ in ()).throw(AssertionError(p)) for p in paths if not re.match(r'^---\s*\nname: [a-z0-9-]+\ndescription: .+\n---\s*\n', p.read_text(encoding='utf-8'))]; print('ok', [p.parent.name for p in paths])"`
Expected: `ok ['add-eol-provider', 'generate-eol-config', 'update-eol-config']`

- [ ] **Step 6: Commit**

```bash
git add .claude/skills
git commit -m "feat: add generate-eol-config, update-eol-config, add-eol-provider skills"
```

---

### Task 5: `eol-config-extractor` agent — update mode

**Files:**
- Modify: `.claude/agents/eol-config-extractor.md`

**Interfaces:**
- Consumes: `docs/updating-a-config.md` (Task 1) and its diff categories (added / version-changed / removed / kept-but-flagged).
- Produces: the "## Update mode" section the `update-eol-config` skill (Task 4) and thin `CLAUDE.md` (Task 3) refer to.

- [ ] **Step 1: Failing check**

Run: `python -c "from pathlib import Path; t = Path('.claude/agents/eol-config-extractor.md').read_text(encoding='utf-8'); assert '## Update mode' not in t; print('missing as expected')"`
Expected: `missing as expected`

- [ ] **Step 2: Extend the frontmatter description**

In `.claude/agents/eol-config-extractor.md`, the frontmatter `description:` block currently ends with `Give it the input file path(s) and the project name.` Change that final sentence to:

```
Give it the input file path(s) and the project name; additionally give it the
  path of an existing config to update it in place (update mode) instead of
  generating fresh.
```

(Keep the `>` folded-block indentation of the surrounding lines.)

- [ ] **Step 3: Fix the stale provider count**

In the "## Read the canonical spec first" section, change `the **seven** \`source\` providers` to `the **eight** \`source\` providers` (the quick reference below it already lists 8).

- [ ] **Step 4: Add the update-mode section**

Append this section immediately after the "## Workflow" section's step 5 and before "## Rules":

````markdown
## Update mode (an existing config path was given)

If you are given the path of an existing `eol_config.<project>.json` alongside
the inputs, do NOT write a fresh config. Follow `docs/updating-a-config.md`
(the canonical diff workflow) on top of the workflow above:

- Step 1 (Extract) still produces the current inventory from the inputs; the
  existing config is the baseline to diff against — added / version-changed /
  removed / unchanged, with the doc's evidence rules (a removal needs explicit
  evidence: strikethrough, "decommissioned", or absence from an authoritative
  manifest that previously declared the component).
- Preserve curation exactly as the doc says: `_comment` provenance (update,
  never delete), `policy_note`s, `_section` grouping, and `manual` entries
  (never dropped — no automated input will ever mention them).
- Step 2 (Verify live) narrows to added and version-changed entries only.
- Steps 3-4 (Write, Validate + smoke-run) are unchanged, except you edit the
  existing file in place and confirm no NEW error rows versus the baseline.
- Step 5 (Report) becomes the diff summary: counts plus per-entry lists of
  added / version-changed / removed (each removal with its evidence) /
  kept-but-flagged.
- If no new inputs were given at all, run the pure re-verification pass from
  the doc: re-check every automated entry live and refresh `manual` entries'
  `eol_date` against their `reference_url`.
````

- [ ] **Step 5: Verify**

Run: `python -c "from pathlib import Path; t = Path('.claude/agents/eol-config-extractor.md').read_text(encoding='utf-8'); assert '## Update mode' in t and t.index('## Workflow') < t.index('## Update mode') < t.index('## Rules'); assert '**eight**' in t and '**seven**' not in t; assert 'update mode' in t[:t.index('---', 4)].lower(); print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add .claude/agents/eol-config-extractor.md
git commit -m "feat: eol-config-extractor update mode (diff-and-refresh existing configs)"
```

---

### Task 6: Integrity checker + full verification

**Files:**
- Create: `tests/check_agent_docs.py`

**Interfaces:**
- Consumes: every deliverable from Tasks 1-5.
- Produces: a permanent standalone checker (repo test convention: plain `python` assertion script, no framework, no network).

- [ ] **Step 1: Write the checker**

Create `tests/check_agent_docs.py` with exactly:

```python
"""Integrity check for agent-facing docs and skills.

Standalone assertion script (repo convention: no framework, no network).
Verifies: required deliverables exist; backticked repo-path references in the
agent docs resolve; skill/agent frontmatter carries name + description; no
placeholder text; the provider-repair section exists.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOCS = [
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "docs" / "adding-a-provider.md",
    ROOT / "docs" / "updating-a-config.md",
]
SKILLS = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
AGENTS = sorted((ROOT / ".claude" / "agents").glob("*.md"))

# 1. Required deliverables exist
required = DOCS + [
    ROOT / ".claude" / "skills" / name / "SKILL.md"
    for name in ("generate-eol-config", "update-eol-config", "add-eol-provider")
]
missing = [str(p) for p in required if not p.exists()]
assert not missing, f"missing deliverables: {missing}"

# 2. Backticked repo-path references resolve (skip <templates>, globs, commands)
path_re = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*\.(?:md|py|json|sh|ps1|tf))`")
dangling = []
for doc in DOCS + SKILLS + AGENTS:
    text = doc.read_text(encoding="utf-8")
    for ref in sorted(set(path_re.findall(text))):
        if any(ch in ref for ch in "<>*"):
            continue
        if not (ROOT / ref).exists():
            dangling.append(f"{doc.name}: {ref}")
assert not dangling, "dangling path references:\n" + "\n".join(dangling)

# 3. Frontmatter: skills and agents carry name + description
fm_re = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
for f in SKILLS + AGENTS:
    m = fm_re.match(f.read_text(encoding="utf-8"))
    assert m, f"{f}: missing frontmatter"
    assert re.search(r"^name:\s*\S", m.group(1), re.M), f"{f}: frontmatter lacks name"
    assert re.search(r"^description:\s*\S", m.group(1), re.M), f"{f}: frontmatter lacks description"

# 4. No placeholder text in the agent-facing docs/skills
for f in DOCS + SKILLS:
    text = f.read_text(encoding="utf-8")
    assert not re.search(r"\bTBD\b|\bTODO\b", text), f"{f}: placeholder text"

# 5. Landmark sections exist
assert "## Repairing a broken provider" in (ROOT / "docs" / "adding-a-provider.md").read_text(encoding="utf-8")
assert "## Workflows index" in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
assert "## Update mode" in (ROOT / ".claude" / "agents" / "eol-config-extractor.md").read_text(encoding="utf-8")

print("check_agent_docs: OK")
```

- [ ] **Step 2: Run the checker**

Run: `python tests/check_agent_docs.py`
Expected: `check_agent_docs: OK` (if it reports a dangling reference or missing landmark, fix the referenced doc — not the checker — unless the checker itself mis-parses a legitimate reference)

- [ ] **Step 3: Run the existing test suite (regression)**

Run each separately (avoids `&&`, which Windows PowerShell 5.1 rejects):

```
python tests/test_policy_text.py
python tests/test_policy_html.py
python tests/test_policy_injection.py
python tests/test_configs_have_notes.py
```

Expected: each script exits 0 (they print their own pass lines). These touch `eoltracker` runtime + local configs, which this plan does not modify.

- [ ] **Step 4: Commit**

```bash
git add tests/check_agent_docs.py
git commit -m "test: add check_agent_docs.py integrity checker for agent docs/skills"
```
