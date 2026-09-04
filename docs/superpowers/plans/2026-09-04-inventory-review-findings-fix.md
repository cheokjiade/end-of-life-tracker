# Plan: fix the dependency-inventory review findings

## Context

`docs/handoffs/2026-09-04-dependency-inventory-port-review-findings.md` (the SPEC for this
plan) lists defects in the dependency-inventory subsystem that were confirmed by
reproduction on 2026-09-04 and carried into this repo unfixed by the port (PR #36). This plan
fixes them on branch `fix/inventory-review-findings`, started from the port branch head
`e395875` (content-identical to main after PR #36 merges; the branch is rebased onto main
before the PR is opened).

Root cause of why these survived 14 review rounds upstream: regression tests that were
defined twice (only the second definition registered) or never registered, so the suite
was green over dead code. Task 1 installs a guard against that class before anything else.

Test conventions in this repo: every `tests/test_*.py` and `tests/check_*.py` is a
standalone script run as `python tests/<file>.py`, no pytest. Some files register tests in
a `TESTS = [...]` list and loop over it; others are top-level assert scripts. Both styles
are valid; the guard must handle both.

## Global Constraints

1. Work only in `E:\Git\end-of-life-tracker-worktrees\inventory-review-findings` on branch
   `fix/inventory-review-findings`. Never push; never touch `main`.
2. Never modify root `generate_config.py` (extended by open PR #35). The scanner under fix is
   `helper_scripts/generate_config.py` and `helper_scripts/eol_inventory/`.
3. TDD for every behaviour change: a failing regression first (RED, command + output in the
   report), then the fix (GREEN). Regressions live in the existing standalone test file that
   covers the module, registered in its `TESTS` list when the file has one.
4. Never weaken or delete an existing assertion. When merging duplicate test definitions,
   keep the union of their assertions.
5. Runtime (`eoltracker/`) stays stdlib-only, Python 3.9+ syntax (no `X | Y` annotations).
   Helper scripts likewise stdlib-only.
6. Secret redaction is fail-closed: when in doubt, collapse to the placeholder rather than
   pass a token through. Digest anchors that are complete 64-hex `sha256:` values stay
   stable; anything else anomalous collapses.
7. Commits follow `docs/commit-conventions.md` (`type(scope): outcome`, imperative, 72 cols
   where practical) and end with the trailer line
   `Claude-Session: https://claude.ai/code/session_01Dxjfq5wEbszHksHxbxJZZB`.
8. Redirect bytecode: `PYTHONPYCACHEPREFIX=C:\Users\Me\AppData\Local\Temp\claude\E--Git-end-of-life-tracker\d893da00-b6a6-4d7f-8630-dfd76299366a\scratchpad\pycache`.
   Run each script with a timeout; all are network-free, a hang is a failure.
9. Gate at the end of every task: the task's covering test files pass. Gate at the end of
   the plan: every `tests/test_*.py` and `tests/check_*.py` exits 0, `python -m compileall -q
   eoltracker helper_scripts tests` exits 0, `python tests/check_agent_docs.py` exits 0.
10. Do not refactor beyond the finding being fixed. No renames, no file splits.

## Task 1: Guard against unregistered and shadowed tests

Files: new `tests/check_test_registration.py`; `AGENTS.md` (list the new check next to the
other `check_*` scripts, wherever they are documented).

Behaviour of the guard (standalone script, exit 1 on any violation, prints each one):
- For every `tests/test_*.py`, parse with `ast`. A module-level `def test_*` defined more than
  once is a violation ("shadowed definition", name, both line numbers).
- If the module assigns a module-level `TESTS` list (an `ast.List` of `ast.Name`s), every
  module-level `def test_*` must appear in it; each missing one is a violation
  ("unregistered", name, line). Modules without `TESTS` are top-level-assert scripts and are
  exempt from the registration rule.
- Never imports or executes the test modules; AST only.

Steps:
1. Write the guard.
2. Run it. It MUST fail on the current tree with exactly these violations (that is the
   RED evidence for Tasks 2 and 3; do not fix them here):
   - `tests/test_generate_config.py`: `test_update_retained_untracked_inventory_visible`,
     `test_update_observed_unmapped_not_duplicated`,
     `test_update_curated_manual_row_remains_product` each defined twice.
   - `tests/test_inventory_report.py`: `test_view_metadata_collapse_scp_references`
     unregistered.
   If it reports anything else, investigate: either the guard is wrong or there is an
   additional latent defect; report which.
3. Add a self-test at the bottom of the guard (guarded by `if __name__ == "__main__"` and a
   `--self-test` flag) that writes two tiny synthetic modules to a temp dir (one shadowed,
   one unregistered, one clean) and asserts the detector flags exactly the right ones.
4. Document the check in AGENTS.md next to the other check scripts.
5. Commit: `test(guard): fail on unregistered or shadowed standalone tests`.

Done when: guard exists, self-test passes, guard fails on the current tree with exactly the
four expected violations (recorded verbatim in the report), AGENTS.md updated.

## Task 2: Close the multi-anchor SCP leak at the report display boundary (High)

Spec: handoff "Confirmed prior findings" row 1, and the "Verified OK" list (which must stay
OK). Files: `helper_scripts/eol_inventory/redact.py`, `tests/test_inventory_redaction.py`,
`tests/test_inventory_report.py`.

Steps:
1. Register `test_view_metadata_collapse_scp_references` in `tests/test_inventory_report.py`'s
   `TESTS`. Run the file: record whether it passes or fails as-is (RED evidence).
2. Add regressions in `tests/test_inventory_redaction.py` (RED first):
   - `redact_display_text("widget@git@host.invalid:private/SENTINEL-repo.git")` must not
     contain `SENTINEL` or `private`.
   - The same token embedded in prose ("see widget@git@host.invalid:private/SENTINEL-repo.git
     for details") must be collapsed while the surrounding prose survives.
   - Idempotency: applying `redact_display_text` twice equals applying it once, for the
     above and for every existing idempotency fixture in the file.
   - Stability: `nginx:1.25@sha256:<64 hex>` unchanged; `@scope/pkg@1.2.3` (npm alias) and
     `pkg@npm:other@1.0.0` unchanged; plain `user@example.com` in prose unchanged unless it
     is followed by a `:` path (then it is an SCP ref and collapses).
   - End to end in `tests/test_inventory_report.py`: plant the sentinel token in a warning
     `path`, a warning `message`, and `generator_version` metadata; assert it is absent from
     the normalized view, the Markdown, and the HTML output.
3. Fix `redact_display_text` / `_DISPLAY_SSH_RE` so a token containing more than one `@`
   collapses unless it matches a complete npm-alias or digest grammar. Keep the change local
   to the display sanitizer; do not touch `redact_dependency_ref`, which already handles this
   input correctly.
4. Run `tests/test_inventory_redaction.py`, `tests/test_inventory_report.py`,
   `tests/test_inventory_python.py`, `tests/test_inventory_node.py`,
   `tests/test_inventory_integration.py`, `tests/test_cli_input_safety.py`.
5. Commit: `fix(inventory): collapse multi-anchor SCP references at the display boundary`.

Done when: all new regressions green, the six files above green, the "Verified OK" list in
the handoff still holds (spot-run the ones with existing tests).

## Task 3: Fix update matching and retained-unmapped identity; unshadow the Batch 3 tests (Medium 2 and 3)

Spec: handoff "Confirmed prior findings" rows 2 and 3, and "New findings" 1. Files:
`helper_scripts/generate_config.py`, `tests/test_generate_config.py`.

Steps:
1. Unshadow: for each of the three duplicated test functions, diff the two bodies. Merge
   into ONE definition holding the union of assertions (Constraint 4); delete the other.
   Run the file: the merged tests may now fail; that failure is RED evidence for this task.
   If a merged test fails for a reason unrelated to findings 2/3, stop and report
   NEEDS_CONTEXT with the traceback.
2. Add regressions (RED) reproducing the handoff probes exactly:
   - Finding 2: existing `shared 1.0/policy A @a/pom.xml` and `shared 2.0/policy B
     @b/pom.xml`; fresh scan has only `shared 3.0 @b/pom.xml`. Expected after update: the
     b-row becomes 3.0 and keeps policy B; the a-row is retained-not-observed with policy A
     intact; no row gets the wrong policy.
   - Finding 3a: same-name `dep` at `a/` (becomes tracked) and `b/` (retained). Expected:
     `_inventory.unmapped` carries only the `b/` item; the view never renders `dep 1.0` as
     both a product and unmapped.
   - Finding 3b: two retained rows at distinct locators in one file. Expected:
     `retained_not_observed=2` and two unmapped items carried; view unmapped count = 2.
3. Fix `generate_config.py`: when the historical identity is multi-row, require a unique
   provenance match before assigning a fresh candidate (else retain conservatively);
   retained-unmapped matching uses the exact retained identity plus full stable provenance,
   and dedup keys include the locator, not just `(name, paths)`.
4. Run `tests/test_generate_config.py`, `tests/test_inventory_integration.py`,
   `tests/test_cli_input_safety.py`, and the Task 1 guard (must now report only the
   Task 2 item if Task 2 has not merged yet, else clean).
5. Commits: `test(config): unshadow the retained-inventory regressions` then
   `fix(config): match updates by provenance and keep retained-unmapped identity`.

Done when: all three merged tests and the new regressions green, guard clean for this file.

## Task 4: Make every config loader enforce one bounded-JSON contract (Medium 4)

Spec: handoff "Confirmed prior findings" row 4 (note its anchors describe the state before
PR #36's final fix wave; read `eoltracker/validation.py` `check_config_bounds` and
`eoltracker/handler.py` as they are now). Files: `eoltracker/core.py`,
`eoltracker/validation.py`, `eoltracker/handler.py`, `tests/test_config_validation.py`,
`tests/test_runtime_guardrails.py`, `tests/test_provider_safety.py`.

Steps:
1. Regressions (RED): through both `load_config_from_file` and `load_config_from_s3` (stub
   `boto3` the way `tests/test_runtime_guardrails.py` already does), a top-level JSON array
   `[1, 2]` and a body with invalid UTF-8 (`{"a": "\xff"}`) must each raise
   `ConfigValidationError` with a finding whose message names the problem (not a raw
   `UnicodeDecodeError`, not a silently accepted list). `--validate` on the same two files
   must exit 1 with the same messages (linter/runtime parity).
2. Fix by making the runtime path use `core.validate_bounded_json` as the single
   implementation: `validation.check_config_bounds` (or its successor) delegates to it, and
   the non-object / invalid-UTF-8 rejections come from there. Keep the exception type and
   finding shape the loaders raise today. Remove any now-dead duplicate check; keep
   `helper_scripts/eol_inventory/config_io.py`'s standalone copy (it cannot import the
   runtime package) but add a one-line comment pointing at the runtime twin.
3. Run `tests/test_config_validation.py`, `tests/test_runtime_guardrails.py`,
   `tests/test_provider_safety.py`, `tests/test_delivery_handler.py`,
   `tests/test_html_runner.py`.
4. Commit: `fix(config): reject non-object and undecodable configs in every loader`.

Done when: parity holds for file, S3, and `--validate`; covering tests green.

## Task 5: Cap generated config output at the runtime limit (Medium 5)

Spec: handoff "Confirmed prior findings" row 5. Files:
`helper_scripts/eol_inventory/config_writer.py`, `helper_scripts/generate_config.py`,
`tests/test_cli_input_safety.py`.

Steps:
1. Regression (RED): a synthetic requirements file under `MAX_FILE_BYTES` with enough pins
   that the serialized config would exceed `MAX_CONFIG_FILE_BYTES` must make the scanner
   exit non-zero with a message naming the limit and the record count, and must NOT write a
   partial or oversized config file. Build the input in a temp dir; keep the test under
   ~10 s (shrink the limit via the module constant inside a `try/finally` if needed, as
   `tests/test_config_validation.py` does).
2. Fix: before writing, serialize and check size against the shared limit; on overflow, fail
   closed with a clear error. Do not silently truncate records.
3. Correct the wrong comment at `tests/test_cli_input_safety.py` ("about 5 MB") to the real
   figure the test exercises.
4. Run `tests/test_cli_input_safety.py`, `tests/test_inventory_integration.py`,
   `tests/test_helper_wrappers.py`.
5. Commit: `fix(inventory): fail closed when generated config exceeds the runtime limit`.

Done when: regression green, no oversized config can be produced, comment corrected.

## Task 6: Standards leftovers

Spec: handoff "Standards findings" 4 and 5, and "New findings" 4. Files:
`eoltracker/parsers/nuget_registry.py`, `AGENTS.md`, `docs/updating-a-config.md`.

Steps:
1. Replace the `tuple | None` annotation in `nuget_registry.py` with `Optional[tuple]`
   (import from `typing`), the only PEP 604 syntax in the package.
2. Document the `_inventory.scan_date` key where the scanner metadata keys are listed.
3. Decide "New findings" 4 (short digest anchors collapse) by reading `redact.py` after
   Task 2: if a truncated `sha256:` value still collapses, add one assertion pinning that
   as intended fail-closed behaviour with a comment; do not change behaviour.
4. Run `tests/test_nuget_registry.py`, `tests/check_agent_docs.py`, `python -m compileall`.
5. Commit: `chore(inventory): align annotations and document scan_date`.

Done when: covering tests green and check_agent_docs green.
