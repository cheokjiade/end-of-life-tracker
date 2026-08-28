---
name: manage-eol-config
description: "Generate or update EOL tracker JSON configs from dependency manifests, Confluence or wiki inventories, spreadsheets, documents, or prose. Use for new configs, upgrades, inventory changes, or lifecycle re-verification while preserving curated notes."
---

# Manage an EOL config

Create or refresh a validated config for this repository's EOL tracker. Work directly in the current harness; do not depend on a particular agent, subagent, plugin, spreadsheet tool, or browser implementation.

The repository root is three directories above this skill file (`../../..`). Resolve it before doing any work. Run all repository commands from that root and treat unqualified repository paths below as relative to it.

## Read first

Read `../../../AGENTS.md` for repository-wide rules. Then select the mode:

- **Generate:** no existing target config. Read `../../../eol_config_generation_prompt.md`, the canonical schema, provider-selection, extraction, and verification specification.
- **Update:** an existing config was supplied or the target `eol_config.<project>.json` already exists. Read `../../../docs/updating-a-config.md` and patch the baseline; never regenerate it wholesale.

If the user's wording says "generate" but the target already exists, treat it as an update unless the user explicitly requests a separate replacement file.
Establish the project slug, target config path, and all authoritative inputs.
If the project name is omitted, infer a short lowercase slug and report it.

## Extract the inventory

Use every input the user placed in scope and retain provenance for each entry.

- For a new config sourced only from clean `pom.xml`, `*.gradle*`, or `package.json` files, run `python generate_config.py <folder> --name <project>` from the repository root. Review `_skipped_npm_packages`; the generated file is a draft until verified.
- For an update sourced from manifests, run the generator to a scratch config and diff that inventory against the baseline. Never point `--output` at the baseline.
- For Confluence/wiki exports, CSV/XLSX files, architecture documents, tables, or prose, extract product, version, lifecycle date, status, and source-row provenance using the real-world document rules in `../../../eol_config_generation_prompt.md`. Do not modify the source document.
- When a live Confluence, document-store, or spreadsheet connector is available,
  use it read-only. Platform-specific connectors are optional; the workflow must
  also work from exported files.
- For mixed inputs, combine their evidence. An authoritative manifest may establish dependency presence while an inventory document supplies ownership or vendor lifecycle dates.
- If the harness cannot read a binary document, ask for an export such as CSV, HTML, PDF, or plain text. Do not infer unseen content.
- Do not add products the inputs do not mention, and do not collapse distinct in-use major versions into one entry.

Skip `_section` dividers when comparing products. Preserve distinct versions or majors when the inputs show they coexist.

## Generate or patch

For a new config, map every supported component to the best automated provider, group entries with `_section` dividers, and add an `_comment` that identifies its input file, row, cell, property, or dependency coordinate.

For an update, classify each baseline entry as added, version-changed, removed, or unchanged:

- Preserve unchanged entries byte-for-byte where practical.
- On version changes, update the provider-specific version/cycle, label, and provenance while retaining valid `policy_note` content.
- Remove an entry only with explicit retirement evidence or because an authoritative manifest that previously declared it no longer does. Absence from a partial document is not removal evidence.
- Preserve `manual` entries unless explicit evidence retires them; manifests do not normally contain them.
- Keep existing section organization and human curation. Place additions in the nearest matching section.

Prefer `endoflife_date` and the repository's specialized providers over `manual`. Never fabricate a slug, cycle, package, artifact, version, or date. Keep unresolved components visible in Needs-Manual-Review rather than creating a broken entry.

When an input document explicitly supplies a lifecycle date for a component that has no automated provider, that document is authoritative for the `manual` entry. Corroborate it against a vendor source when one is available, and record both provenance and any discrepancy instead of silently replacing the supplied date.

## Verify before finishing

Live-check every new or changed automated entry; for a pure re-verification request, check all entries:

- Confirm each endoflife.date slug exists and its `cycle` exactly equals the config's `version`.
- Confirm npm package names and pinned versions against the npm registry.
- Confirm Maven group/artifact coordinates and versions against Maven Central.
- Use the provider's authoritative vendor page for scraper-backed lifecycle data and for any `policy_note` or manual date.

Batch checks per source when practical. If network access is unavailable, do not claim verification: leave existing entries unchanged, omit speculative new entries, and list the exact checks still required.

Keep configs strictly valid JSON. ASCII is the convention (generated configs are pure ASCII); UTF-8 files are accepted — save as UTF-8, and keep `policy_note` text ASCII for the console/SNS report. Use `_comment` and `_section` instead of JSON comments. Do not alter alert or notification settings unless the user or evidence requires it.

## Validate and report

Use an available Python launcher (`python`, `python3`, or `py`) to parse the result, then smoke-run it through `lambda_function.py`. Correct new provider errors; do not hide errors by converting automated entries to `manual`.

Finish with:

- output config path and entry counts by provider;
- added, version-changed, removed, and unchanged counts for updates;
- live verification results;
- Needs-Manual-Review, including ambiguous/skipped raw values and provenance;
- inputs intentionally skipped as retired, struck through, or migrated away, with the evidence for each skip;
- smoke-run result and any pre-existing versus newly introduced error rows.

Do not stage, commit, deploy, upload, or send the config unless the user asks.
