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
