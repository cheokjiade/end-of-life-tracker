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
  - **Dependency manifests** (`pom.xml`, `*.gradle*`, package.json): scan with
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
   re-verification pass checks all entries). Use one batched stdlib
   `urllib.request` script per source: confirm each endoflife.date slug + exact
   `cycle` at `https://endoflife.date/api/{slug}.json`, each npm `package`
   (+ `version`) resolves on `registry.npmjs.org`, each Maven `group:artifact`
   resolves via the `search.maven.org` solrsearch API. (The "Verification
   Checklist" section of `eol_config_generation_prompt.md` shows the per-entry
   curl form for endoflife.date.) Anything unverifiable is flagged, not
   guessed.
6. **Validate + smoke-run:**
   `python -c "import json; json.load(open('eol_config.<project>.json'))"`, then
   `python lambda_function.py eol_config.<project>.json` — confirm no new
   `error` rows appear that were not already in the baseline report.
7. **Report the diff.** Counts plus per-entry lists: added / version-changed /
   removed (each removal with its evidence) / kept-but-flagged. This report is
   the deliverable a reviewer checks before the config is deployed.

## Rules

- ASCII only — `load_config_from_file` reads bytes and requires ASCII-only JSON,
  so non-ASCII fails on cp1252 systems; every load also enforces the schema,
  and one malformed product entry surfaces as an `error` row instead of
  aborting the run.
- Strictly valid JSON: no comments, no trailing commas; use `_comment` /
  `_section` fields instead.
- Do not deduplicate distinct majors (Java 8 and Java 17 stay two entries).
- Never turn an automated entry (`endoflife_date`, scraper, registry) into a
  `manual` entry just because verification was inconvenient — a live source
  stays current; a hardcoded date rots.
