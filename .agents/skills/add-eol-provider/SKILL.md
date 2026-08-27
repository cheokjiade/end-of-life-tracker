---
name: add-eol-provider
description: Add a new EOL tracker data-source provider, or repair an existing provider after upstream drift, error rows, failed canaries, or changed source formats. Use for parser/provider implementation and maintenance.
---

# Add or repair an EOL provider

The repository root is three directories above this skill file (`../../..`). Resolve it before doing any work. Run all repository commands from that root and treat unqualified repository paths below as relative to it.

Read `../../../AGENTS.md`, then read `../../../docs/adding-a-provider.md` completely. The document is the authoritative provider contract, implementation skeleton, defensive-parsing standard, test strategy, and repair workflow.

## Choose the mode

- **Add:** the requested `source` does not exist under `eoltracker/parsers/`.
- **Repair:** the provider exists but returns errors, fails a canary or row-count check, or no longer parses its upstream source.

Do not change provider behavior until you have reproduced the need and inspected the current provider, its network-free tests, and the authoritative upstream source.

## Implement

For a new provider, add one module under the repository-root path `eoltracker/parsers/` with a cached fetch helper, a pure parse helper, and a normalized provider function. Register it through the module attributes documented in `../../../docs/adding-a-provider.md`; do not edit a central registry.

For a repair, preserve the provider contract and strengthen or update the pure parser around observed upstream evidence. Never remove a canary, lower a row-count floor, or weaken a required-header check merely to silence an error.

Keep `eoltracker/` stdlib-only. Return the uniform result shape and use `_error_result` on failures. If the work introduces a status, update categorization, both report formatters, labels/colours, and tests as described in the canonical document.

## Verify and document

1. Add or update network-free tests using synthetic upstream data and injected caches.
2. Verify automatic registration, source labels, and upstream URL generation.
3. Run the relevant network-free test scripts.
4. Run one live smoke test with a config entry using the provider.
5. Update `../../../eol_config_generation_prompt.md` with the provider table row, entry shape, and mapping decision.
6. Update the provider count and list in `../../../AGENTS.md`.

Report the reproduced failure or new-source need, files changed, defensive checks retained or added, network-free results, live smoke result, and any upstream assumptions still requiring review.

Follow `../../../AGENTS.md` for batch commit behavior. Do not push, deploy, or modify user configs unless the user asks.
