# Consolidate the two config generators

Date: 2026-09-05. Status: approved design, pending implementation plan.

## Problem

Two config generators coexist since PR #36 (inventory port) and PR #35 (extractor
accuracy) merged on 2026-09-04:

- Root `generate_config.py` (1,752 lines): Java (pom, Gradle, `settings.gradle`,
  `libs.versions.toml`) and npm. Collects `maven_repositories`, resolves transitive
  graphs by running `mvn`/`gradle`, parses npm lockfile graphs, carries Vue/Next/Nuxt/
  Jackson/Spring Security mapping rules, and writes `_discovered_dependencies`. It has
  no `--update`, no redaction, no input bounds, and overwrites its output
  unconditionally. Eight test files (2,287 lines) import it by name.
- `helper_scripts/generate_config.py` (478 lines) + `helper_scripts/eol_inventory/`
  (6,763 lines): Java, Node, Python, Go, .NET, Dockerfile, GitLab CI. Curation-preserving
  `--update`, `.eolignore`/`--exclude`, `--strict`, redaction of every output boundary,
  bounded reads and a bounded atomic write, `_found_in` provenance and `_inventory`
  metadata, a Markdown/CSV/HTML report writer, and interactive wrappers.

The runtime (`eoltracker/`) reads exactly two generator-produced keys beyond the
product rows: top-level `maven_repositories` (validated in `validation.py`, stamped onto
`maven_central` entries in `handler.py`) and per-entry `repository` (read by
`parsers/maven_central.py`). Everything else under an underscore key is ignored.

Decision (user, 2026-09-05): the inventory scanner survives; the root script's unique
capabilities move into it; the root script and its tests are deleted in the same PR.

## Decisions taken

1. Transitive resolution that executes `mvn`/`gradle` is ported as an explicit opt-in
   flag, `--resolve-transitive`, with the same degrade-to-warning behaviour. It is the
   only code path in the scanner that runs external commands, and the docs say so.
2. Root `generate_config.py` and the eight `tests/test_generate_*.py` files that import
   it are deleted in the same PR, after their coverage has moved.
3. The full-picture record is folded into `_inventory.declarations` using the root
   script's `{decl, file, kind, outcome}` record shape. `_discovered_dependencies` is no
   longer written; `--update` tolerates it in older configs and drops it on write.

## End state

One scanner: `helper_scripts/generate_config.py` backed by `helper_scripts/eol_inventory/`.
All current inventory behaviour is unchanged. The runtime is not modified. The two keys
the runtime reads (`maven_repositories`, per-entry `repository`) are produced by the
consolidated scanner with the same shapes the root script produced.

## What moves, and where

| Capability (root script anchor) | Destination | Notes |
|---|---|---|
| Maven repository collection from pom `<repositories>` and Gradle `repositories {}` blocks, plus fallback ordering (`generate_config.py:766-874`, emit at `:1688`) | new `eol_inventory/parsers/maven_repositories.py`; emission in `config_writer.py` | Emits top-level `maven_repositories` (list of URLs, deduplicated, central-first rule preserved) and per-entry `repository` for artifacts declared against a non-central host, matching the runtime's expectations in `validation.py:290-297` and `maven_central.py:331-355`. |
| Gradle version catalogs (`generate_config.py:875-993`) | `eol_inventory/parsers/java.py` | `libs.versions.toml` parsed; `libs.*` references resolved to versions instead of being treated as interpolation (`java.py:175`). Unresolvable references stay unresolved with a warning. |
| npm lockfile graph enumeration (`generate_config.py:1097-1194`) | `eol_inventory/parsers/node.py` | `node.py` already pins declared ranges from sibling lockfiles; it gains full graph enumeration when `--include-transitive` is set. Records are marked indirect as the inventory already does. |
| Vue/Next/Nuxt/Jackson/Spring Security mapping rules (`generate_config.py:163-171, 219-226, 323-368, 1628-1649`) | `eol_inventory/mappings.py` | Merged into the existing tables and mapping functions. Where the two sides disagree on a mapping, the root script's rule wins only if its test proves a runtime-visible difference; otherwise the inventory rule stands and the test is adjusted with a note. |
| Transitive resolution via `mvn dependency:list` and a Gradle init-script task (`generate_config.py:1037-1096, 1195-1322`) | new `eol_inventory/resolvers.py` | Reached only by `--resolve-transitive`. Missing tool, non-zero exit, or timeout produces a warning (`transitive_unavailable`) and the scan continues. Output records flow through the same record model and redaction as file-parsed records. |
| `_discovered_dependencies` record and summary tally (`generate_config.py:1432-1449, 1692`) | `config_writer.py` (`_inventory.declarations`, `_inventory.summary.declarations`), `report_writer.py` (declarations section) | See Decision 3. |

## Flags

- `--include-transitive` (existing): surface indirect records already present in
  lockfiles. No external commands.
- `--resolve-transitive` (new): additionally run `mvn`/`gradle` to obtain the resolved
  graph. Implies `--include-transitive`. Documented as executing external tools.
- All other flags unchanged: `folder`, `--name`, `--output`, `--exclude`, `--update`,
  `--replace`, `--strict`.

## Output schema changes

- `maven_repositories`: top-level list, present when at least one non-central
  repository was declared. Same shape the runtime already validates.
- Per-entry `repository`: string, present only on entries whose artifact was declared
  against a non-central host.
- `_inventory.declarations`: list of `{decl, file, kind, outcome}`. `_inventory.summary`
  gains `declarations` (total) and per-outcome counts.
- `_discovered_dependencies`: never written. Read-tolerated by `--update` and dropped.
- `_skipped_npm_packages`: unchanged (both sides already write it).

## Parity gate (temporary)

A standalone test, `tests/test_generator_parity.py`, runs both generators over every
directory under `tests/fixtures/generate_config/` and asserts the consolidated output is
a superset of the root script's: identical `products` (by identity, version, source, and
`repository`), identical `maven_repositories`, identical `_skipped_npm_packages`, and
every `_discovered_dependencies` record present in `_inventory.declarations`. It exists
only while both generators exist and is deleted in the retirement task together with the
root script.

## Tests

The eight root test files move file by file into the inventory test files as each
capability lands (`test_generate_repositories.py` into a new
`tests/test_inventory_maven_repositories.py`; catalogs, npm graph, mappings, transitive
parsers and merge into the matching `tests/test_inventory_*.py` files). No assertion is
dropped; assertions that pin root-script-specific output shapes are retargeted to the
consolidated shape with a note. `tests/check_test_registration.py` guards registration.

## Docs and retirement

- `AGENTS.md`: coexistence note removed; workflow row, layout tree, and key-files table
  reference one generator; the "executes external tools" statement for
  `--resolve-transitive` added next to the CLI description.
- `README.md` lines ~113-119 and `eol_config_generation_prompt.md` references updated.
- `eoltracker/parsers/maven_central.py:55-58` docstring updated to name the helper.
- `docs/updating-a-config.md`, `.agents/skills/manage-eol-config/SKILL.md`, the
  wrappers, and `run.sh`/`run.ps1` already reference only the helper: no change.
- Generated fixtures `eol_config.b-auto.json` and `eol_config.smoke.json` keep their
  historical "generated by" comments.
- Root `generate_config.py` deleted last, together with the parity gate.

## Out of scope

- Any change to `eoltracker/` runtime behaviour.
- Changing `--update` merge semantics (PR #37 settled them).
- New ecosystems.

## Success criteria

- Every `tests/test_*.py` and `tests/check_*.py` exits 0; `compileall` clean.
- Parity gate green immediately before the retirement task.
- `grep -rn "generate_config" --include=*.md --include=*.py --include=*.sh --include=*.ps1` finds no reference to a root-level script except historical plans, handoffs, and audits.
- A scan of `tests/fixtures/generate_config/mixed` with and without `--resolve-transitive` (tools absent) succeeds, the latter with a `transitive_unavailable` warning.
