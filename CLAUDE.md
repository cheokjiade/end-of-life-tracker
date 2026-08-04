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
