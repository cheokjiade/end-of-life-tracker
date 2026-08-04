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
