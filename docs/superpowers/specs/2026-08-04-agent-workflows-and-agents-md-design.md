# Agent workflows: skills, config-update mode, and a canonical AGENTS.md

**Date:** 2026-08-04
**Status:** Approved (design decisions confirmed with user)

## Problem

The repo's agent-driven tooling grew organically and now has three gaps:

1. **No packaged workflow for *updating* an existing config.** `eol-config-extractor`
   and `generate_config.py` both generate from scratch; after a project upgrades its
   dependencies there is no diff-and-refresh path that preserves human curation
   (`_comment` provenance, `policy_note`s, `_section` grouping, `manual` entries).
2. **No packaged workflow for *maintaining providers*.** `docs/adding-a-provider.md`
   covers adding a source, but nothing covers repairing one when the upstream page
   drifts (the canary/error-row case), and neither is invocable as a skill.
3. **All repo guidance is Claude-shaped.** `CLAUDE.md` and `.claude/` are invisible to
   other harnesses (Codex, Cursor, Gemini CLI, …). There is no `AGENTS.md`, the
   emerging cross-harness standard.

## Goals

1. Any coding agent — Claude or not — can generate a config, refresh an existing
   config, and add/repair a provider by following repo docs.
2. Claude Code additionally gets explicit, invocable entry points (skills + the
   existing subagent) for those workflows.
3. One source of truth: no content duplicated between `AGENTS.md`, `CLAUDE.md`,
   skills, and docs.

## Decisions (confirmed)

- **"Update sources and dependencies" = both workflows**: config refresh AND
  provider add/repair.
- **`AGENTS.md` is canonical.** The shared architecture/conventions content moves
  there; `CLAUDE.md` becomes a thin pointer holding only Claude-specific material.
- **Docs-canonical + thin wrappers packaging.** Workflow knowledge lives in
  harness-neutral `docs/*.md`; skills are thin invocable wrappers over those docs
  (the same shape `eol-config-extractor` already uses with
  `eol_config_generation_prompt.md`).

## Design

### 1. `AGENTS.md` (new, repo root — canonical guide)

Harness-neutral. Content migrates from `CLAUDE.md`: what the repo is, package
layout, provider/registry architecture, conventions & gotchas (stdlib-only, ASCII
configs, gitignored artifacts, report paths, no-framework testing, local run), and
the key-files table. Adds a **Workflows index** routing any agent to the right
playbook:

| I want to… | Read / run |
|---|---|
| Generate a config from dependency manifests | `python generate_config.py <folder>`, then verify |
| Generate a config from messy docs (wiki tables, spreadsheets, prose) | `eol_config_generation_prompt.md` |
| Update an existing config after upgrades | `docs/updating-a-config.md` |
| Add a new data-source provider | `docs/adding-a-provider.md` |
| Repair a provider whose upstream drifted | `docs/adding-a-provider.md` § Repairing |

Also states the universal norms: verify slugs/cycles live before writing; validate
JSON; smoke-run `python lambda_function.py <config>`; keep tests network-free.

### 2. `CLAUDE.md` (thinned to Claude-specifics)

Opens with "Read `AGENTS.md` first — it is the canonical guide." Retains only what
other harnesses cannot use: when to dispatch the `eol-config-extractor` subagent,
the three repo skills, and the superpowers brainstorm → spec → plan flow under
`docs/superpowers/`. Target ~25 lines.

### 3. `docs/updating-a-config.md` (new canonical workflow)

The refresh workflow — the genuinely new capability:

1. Load the existing `eol_config.<project>.json` as the baseline inventory.
2. Extract the current inventory from fresh inputs (manifests via
   `generate_config.py`, or documents via the extraction spec in
   `eol_config_generation_prompt.md`).
3. **Diff**: added / removed / version-changed components.
4. Apply changes **preserving human curation**: `_comment` provenance,
   `policy_note`s, `_section` grouping, and `manual` entries are never dropped
   without explicit input evidence (strikethrough / "decommissioned" / absent from
   an authoritative manifest).
5. Live-verify only new and version-changed entries (same batched-script technique
   as the extractor agent).
6. Validate JSON + smoke-run; report the diff (added/changed/removed/kept).

Explicitly forbids wholesale regeneration — that destroys curation.

### 4. `docs/adding-a-provider.md` — add "Repairing a broken provider"

New section: symptom (error rows in the report, canary `ValueError`) → diagnose
(fetch the raw source, run the pure parse helper against it, compare structure to
parser expectations) → fix the parser **keeping the defensive checks** (never
loosen a row-count floor or delete a canary just to silence the error; update the
canary only when the upstream fact legitimately changed) → network-free tests →
one live smoke run.

### 5. Three thin skills (`.claude/skills/<name>/SKILL.md`)

Each ~40 lines: frontmatter (`name`, `description` with trigger phrases), a
"read the canonical doc first" pointer, a checklist, done-criteria.

- **`generate-eol-config`** — routing only: clean manifests →
  `generate_config.py` then verify; messy/mixed inputs → dispatch the
  `eol-config-extractor` agent.
- **`update-eol-config`** — wraps `docs/updating-a-config.md`; dispatches the
  extractor agent in update mode for the heavy lift.
- **`add-eol-provider`** — wraps `docs/adding-a-provider.md` (covers add and
  repair).

### 6. `eol-config-extractor` agent — update mode

Small extension to `.claude/agents/eol-config-extractor.md`: when given an
existing config path alongside the inputs, follow `docs/updating-a-config.md`
(diff, preserve curation) instead of writing fresh; the final report becomes the
diff summary.

## Testing

No runtime code changes (`eoltracker/` untouched); run the existing `tests/`
scripts to confirm nothing regresses. Deliverable checks: every path referenced in
`AGENTS.md` / `CLAUDE.md` / skills exists; no placeholder text; skill frontmatter
is valid YAML with `name` + `description`.

## Out of scope (YAGNI)

New providers; CI; porting skills to other harnesses' native formats (AGENTS.md
pointing at canonical docs covers them); changes to `eoltracker/` runtime or
report formats.
