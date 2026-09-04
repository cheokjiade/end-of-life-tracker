# CLAUDE.md

Read **`AGENTS.md`** first. It is the canonical guide for every AI harness. This
file contains only Claude Code discovery aliases; no workflow is authoritative
under `.claude/`.

## Skills (`.claude/skills/`)

- `eol-config` — generate or update a config from manifests,
  Confluence/wiki inventories, spreadsheets, documents, or prose. It delegates
  to the canonical cross-harness skill at
  `.agents/skills/manage-eol-config/SKILL.md`.
- `eol-provider` — add or repair a data-source provider. It delegates to
  `.agents/skills/add-eol-provider/SKILL.md`.

## Development flow

- Keep `AGENTS.md` authoritative: when architecture, conventions, or workflows
  change, update `AGENTS.md` or the canonical `.agents/skills/` content, not the
  Claude loaders.

## Agent skills

Issue tracker, triage labels, and domain-docs config for backlog skills
(`to-issues`, `triage`, `to-prd`, `qa`, …) — see the `## Agent skills` section
in `AGENTS.md` and `docs/agents/`.
