# Dependency-inventory review findings carried forward from the port

## Purpose

**Status (2026-09-04):** Confirmed-prior findings 1-5, New findings 1, 2, and
4, and Standards findings 2-5 were fixed on branch
`fix/inventory-review-findings` per
`docs/superpowers/plans/2026-09-04-inventory-review-findings-fix.md`.
Finding 2's provenance guarantee applies to rows carrying `_comment` per
`docs/updating-a-config.md` (rows whose `_comment` was deleted keep the older
fallback behaviour). Standards findings 1, 3, and 6 remain historical notes
about the source repository.

This document carries forward, unresolved, the review findings for the
dependency-inventory subsystem (`helper_scripts/eol_inventory/`, the Go proxy /
NuGet / PyPI providers, and their tests) as it now exists in this repository
after being ported from a sibling clone. It is a **carry-forward record, not a
remediation plan**: the port that introduced this file did not fix any finding
below, by design (a future task will decide whether/how to remediate).

Read this alongside `docs/superpowers/plans/2026-09-04-port-dependency-inventory.md`
(the port plan) and, for the original remediation history that led to these
findings, `docs/handoffs/2026-09-02-dependency-inventory-opencode-remediation.md`
and `docs/handoffs/2026-09-02-dependency-inventory-opencode-final-audit-remediation.md`.

## Source and provenance

- Source repository: the `endoflife` clone (GitHub `cheokjiade/endoflife-tracker`),
  commit range `4988e1d..68e117b` (the whole dependency-inventory feature
  branch, including all remediation batches from the two 2026-09-02 handoffs
  above).
- The findings below were produced on 2026-09-04 by two independent Fable
  subagents (a Standards axis and a Spec axis), verifying and extending a
  partial Codex review of the same range, with every finding confirmed by
  reproduction (probe scripts) or by reading the code **at `68e117b`**.
- This repository's port (`feat/dependency-inventory-port`, HEAD `2014c5f` at
  the time of writing) replayed the individual commits of that range on top of
  `main`, hand-merging 10 files that both sides had touched (including
  `eoltracker/core.py`, `eoltracker/handler.py`, `eoltracker/report.py`,
  `eoltracker/parsers/__init__.py`, `AGENTS.md`, and `README.md`).
  `helper_scripts/eol_inventory/`, `helper_scripts/generate_config.py`,
  `tests/test_inventory_report.py`, and the three new provider parsers
  themselves were **not** hand-merged — they carried over verbatim.
  `tests/test_generate_config.py` was also not hand-merged, but it did not
  carry over verbatim: commit `2014c5f` ("test(config): assert the packaging
  allowlist kept by the merge") rewrote
  `test_terraform_uses_positive_runtime_allowlist` to assert this branch's
  `build_lambda_package.py` manifest allowlist instead of the source side's
  Terraform `dynamic "source"` block. Every other test in that file is
  verbatim.
- **Every anchor below was independently re-verified against the ported tree
  at `2014c5f` on 2026-09-04** (grep for the quoted code, and for the two most
  complex findings, direct reproduction with a standalone Python snippet). File
  paths and line numbers in `eoltracker/` files changed where the file was
  hand-merged; anchors in `helper_scripts/` and `tests/` are almost all
  unchanged because those files ported verbatim. Any anchor that could not be
  located is called out explicitly below rather than silently dropped.
- The port did **not** fix any finding below. The only two exceptions, both
  documentation-only and addressed by this same commit, are noted inline.

## Confirmed prior (Codex-originated) findings

| # | Severity | Finding | Verified anchor at `2014c5f` | Verification |
|---|---|---|---|---|
| 1 | High | Report display redaction leaks multi-anchor SCP references. `redact_display_text("widget@git@host.invalid:private/SENTINEL-repo.git")` returns the input unchanged (`redact_dependency_ref` on the same input correctly returns `url:<redacted>`, so only the report-display boundary leaks). | `helper_scripts/eol_inventory/redact.py:57-66` (the `_DISPLAY_SSH_RE` regex — the negative lookbehind `(?<![A-Za-z0-9._~%\-@])` at line 58, which guards the whole alternation group including the `[A-Za-z0-9._~%-]+@` branch, blocks a match when preceded by another `@`) and `:386-489` (`redact_display_text()`, which applies that regex in the loop at line 437). | Re-run: `redact_display_text('widget@git@host.invalid:private/SENTINEL-repo.git')` still returns the string unchanged; `redact_dependency_ref` on the same input still returns `'url:<redacted>'`. File unchanged by the port (not hand-merged). |
| 2 | Medium | Partial multi-version removal corrupts identity and curation. With existing `shared 1.0/policy A @a/pom.xml` and `shared 2.0/policy B @b/pom.xml`, and a fresh scan reporting only `shared 3.0 @b/pom.xml`, the merge produces `3.0/policy A` (wrong provenance) plus a stale `2.0/policy B` row; summary `changed=1, retained_not_observed=1`. | `helper_scripts/generate_config.py:198-199` (`elif len(candidates) == 1: selected = candidates[0]` — the sole-identity-candidate branch runs with no provenance check when only one fresh row shares the identity). | Reproduced directly: constructed the exact existing/fresh dicts above and called `_merge_existing_config()`; output is `[{"...": "3.0", "policy_note": "policy A", "_found_in":[{"path":"b/pom.xml"...}]}, {"...": "2.0", "policy_note": "policy B", ...}]` with `update_summary == {'added': 0, 'changed': 1, 'unchanged': 0, 'retained_not_observed': 1}` — exact match to the reported defect. Note: `generate_config.py`'s multi-candidate provenance branch (lines 176-194) only engages when `len(candidates) > 1`; with a single candidate this line is reached directly, so the defect is unchanged by the additional provenance logic elsewhere in the function. File unchanged by the port. |
| 3 | Medium | Retained-unmapped carry-forward uses insufficient identity: (a) a same-name dependency at two sites, where one becomes tracked and the other is retained, can render under both a product row and an unmapped row; (b) two retained rows at distinct locators in one file report `retained_not_observed=2` but only one unmapped item is carried forward. | `helper_scripts/generate_config.py:237` (`retained_unmapped_keys.add((name, paths))`) and `:303,331` (`retained_names = {key[0] for key in retained_unmapped_keys}` … `if name in retained_names`) — matching is name-only, not name+path, so two distinct sites sharing a name collide; and `:315-321` (the `(u_name, u_paths)` dedup against `fresh_keys`) confirms the dedup key is name+paths while the carry-forward gate above it is name-only. | Anchors located and read in full; the name-only `retained_names` set (built from `key[0]` only, dropping `paths`) versus the name+path `fresh_keys` dedup is exactly the mismatch described. Not independently reproduced with a script (time-boxed); confirmed by code reading only. File unchanged by the port. |
| 4 | Medium | Local and S3 config loaders do not use the shared bounded-JSON validator; they re-implement equivalent checks separately. | Anchors changed by the hand-merge. `eoltracker/core.py:56-85` defines `validate_bounded_json()`; it is not imported or called anywhere outside its own tests (`grep -rn validate_bounded_json eoltracker/ helper_scripts/` outside `tests/` returns only its own definition). Instead, `eoltracker/handler.py:144-159` (`_check_config_depth()`) and `:169-220` (`load_config_from_s3()` / `load_config_from_file()`) reimplement the same size/depth checks inline, and `eoltracker/validation.py:406-426` (`load_config_json_bytes()`) reimplements the UTF-8 decode/JSON-parse step a third time. The original finding's anchors (`eoltracker/handler.py:48-55` and `:60-71`) no longer correspond to this logic — `handler.py` was one of the 10 hand-merged files and was substantially restructured (config loading now delegates to the new `eoltracker/validation.py`, which did not exist in either pre-merge side under that name). | Confirmed by reading `eoltracker/core.py`, `eoltracker/handler.py`, and `eoltracker/validation.py` in full; the duplication is now three-way (`core.validate_bounded_json`, unused; `handler._check_config_depth` + inline size checks; `validation.load_config_json_bytes`) rather than the originally reported two-way duplication. |
| 5 | Medium | Valid scanner output can exceed the common config-size limit: a 1,548,890-byte `requirements.txt` (under the 2,000,000-byte `MAX_FILE_BYTES` per-file cap) with 60,000 `==` pins can produce a config well over the runtime's size limit; no record/output cap exists in the config writer. The comment at `tests/test_cli_input_safety.py:114` ("about 5 MB") undercounts the worst case. | `eoltracker/core.py:21-27` (comment documenting the shared `MAX_CONFIG_FILE_BYTES = 20 * 1024 * 1024` bound and its rationale — unchanged text, confirmed by exact grep), `helper_scripts/eol_inventory/config_writer.py:148-469` (`generate_config()`, unbounded by record count) and `tests/test_cli_input_safety.py:114` (comment unchanged, still says "5000 provenance sites ≈ 5 MB"). | Anchors located and read; `MAX_FILE_BYTES = 2_000_000` in `helper_scripts/eol_inventory/models.py:48` and the writer's lack of an output-size cap are unchanged. Not re-run with a live 60,000-pin file (time-boxed; the underlying files are unmodified by the port so the original reproduction still applies). |

## New findings (Spec axis)

| # | Severity | Finding | Verified anchor at `2014c5f` |
|---|---|---|---|
| 1 | Medium | Three Batch-3 regressions are shadowed and never execute: `test_update_retained_untracked_inventory_visible`, `test_update_observed_unmapped_not_duplicated`, and `test_update_curated_manual_row_remains_product` are each defined twice in `tests/test_generate_config.py`; only the second definition of each is bound (Python rebinds the name), so the first bodies are dead code. | `tests/test_generate_config.py:1904` and `:2007` (`test_update_retained_untracked_inventory_visible`, first and second definitions — unchanged line numbers), `:1945` and `:2095` (`test_update_observed_unmapped_not_duplicated`), `:1979` and `:2129` (`test_update_curated_manual_row_remains_product`); the `TESTS` list starts at `:2265` (shifted from the original `:2255+` note by 10 lines — the file itself is otherwise byte-for-byte the same in this region; verified the duplicate `def` lines are identical to the original report). File unchanged by the port. |
| 2 | Low | `test_view_metadata_collapse_scp_references` is defined in `tests/test_inventory_report.py` but absent from that file's standalone `TESTS` list, so it only runs when invoked directly, not via the file's own `python tests/test_inventory_report.py` entry point. | `tests/test_inventory_report.py:719` (`def test_view_metadata_collapse_scp_references():`) and `:813` (`TESTS = [` — unchanged from the original report). Confirmed the function name does not appear in the `TESTS` list body. File unchanged by the port. |
| 3 | Low, scope creep — **now moot in this ported tree** | Original claim: `eoltracker/report.py`'s `has_alerts` computation makes `notify_when: "alerts_only"` fire on provider errors and unknowns, which was not requested by the inventory plan/handoffs, and README documentation contradicted it. | The behaviour is confirmed still present — `eoltracker/report.py:192-208` (`analyse_results()`: `has_lifecycle_alerts = bool(eol or approaching)`, `has_health_failures = bool(error or unknown or empty_inventory)`, both feed `has_alerts` at `:311` and `:516`) — but **this is `main`'s own pre-existing "tracker health" feature**, not something the inventory-side merge introduced (see Global Constraint 6 of the port plan, which lists "health alerts" among `main`'s features that must survive the port unchanged). `README.md` in this repository already documents the behaviour accurately at lines 230 (the tracker-health paragraph: "trigger notifications even under `alerts_only`") and 241 (the `alerts_only` notification-frequency table row), so the originally reported README staleness does not apply to this repository's README. The scope-creep observation (this was not requested by the *inventory* plan/handoffs) remains true as a historical note about the *source* range, but has no outstanding action in this tree. The Terraform-archive and `_validate_provider_result` parts of this same finding are addressed below (Standards notes): the port kept `main`'s packaging approach for Terraform per Global Constraint 7, so that part does not apply here either; `_validate_provider_result` (`eoltracker/parsers/__init__.py:57-76`, anchor unchanged) is present and still undocumented as intentional hardening. |
| 4 | Info | `redact_display_text("nginx:1.25@sha256:abc")` collapses a short/incomplete digest anchor to `url:<redacted>`; full 64-hex digests pass through unchanged. | `helper_scripts/eol_inventory/redact.py` (same file/anchors as prior finding 1). Re-run: `redact_display_text('nginx:1.25@sha256:abc')` → `'url:<redacted>'`; `redact_display_text('nginx@sha256:' + 'a'*64)` → unchanged. File unchanged by the port. |

## Standards findings

| # | Severity | Finding | Status in this tree |
|---|---|---|---|
| 1 | Hard | `AGENTS.md` was not updated for the new architecture: config-loader description stale, package layout omitted `redact.py`/`config_io.py` and the new `core.py` helpers, and README contradicted the alerts change. | **Partially resolved by this same commit** (the AGENTS.md/README task this handoff is part of): the package-layout listing now names `helper_scripts/eol_inventory/redact.py`, `helper_scripts/eol_inventory/config_io.py`, and `eoltracker/parsers/go_proxy.py` / `nuget_registry.py` / `pypi_registry.py`; the `eoltracker/core.py` rows (package layout and Key files table) now name `validate_bounded_json`, `read_response_bytes`, `decompress_gzip_bytes`; the config-loader prose now states the strict UTF-8 decode and the shared size/depth bounds explicitly. The original finding's other citation, `docs/updating-a-config.md:89`, was checked and is **already accurate** in this tree (it correctly describes ASCII/UTF-8 decoding with the re-save-as-UTF-8 error) — no change was needed there. The README alerts-semantics citation is moot in this tree per new-finding 3 above. |
| 2 | Hard | Unregistered standalone test (same defect as new Spec finding 2). | Still present — see new finding 2 above; not fixed by the port (out of scope per Global Constraint 10). |
| 3 | Judgement | Bounded-JSON loading implemented three times. | Still present, and the third implementation moved: originally cited as `helper_scripts/eol_inventory/config_io.py:53-101` (unchanged — verified `_max_nesting_depth` at `:53` and `load_bounded_config` at `:66`), `eoltracker/core.py:30-84` (now `:30-85`, `_max_nesting_depth` + `validate_bounded_json`), and "inline in `eoltracker/handler.py:35-70`" — that inline copy no longer exists at those lines; the third implementation is now `eoltracker/validation.py:406-426` (`load_config_json_bytes()`), a file introduced by the hand-merge of `eoltracker/handler.py`. |
| 4 | Judgement | `tuple \| None` annotation at `eoltracker/parsers/nuget_registry.py:400` is the only PEP 604 annotation in the package. | Confirmed unchanged: `eoltracker/parsers/nuget_registry.py:400` still reads `best_key: tuple | None = None`. File unchanged by the port. |
| 5 | Judgement | `scan_date` added to `_inventory` (`config_writer.py:453`) without a docs entry. | Confirmed unchanged: `helper_scripts/eol_inventory/config_writer.py:453` still sets `"scan_date": date.today().isoformat()`. File unchanged by the port. |
| 6 | Judgement | Commit-message patterns in the source range (17 review-activity subjects, two byte-identical subjects, six subjects over 72 chars, `fix(report)` scope on commits touching only `generate_config.py`, nine fix commits without test changes). | Historical fact about the replayed commits `4988e1d..68e117b`; the port preserved original commit authorship, dates, and messages verbatim (Global Constraint 3 of the port plan), so these patterns are unchanged and carried into this repository's history as-is. No file:line anchor applies. |

## Verified OK at `68e117b` (not independently re-verified item-by-item in this pass)

Case-insensitive SSH schemes, IPv4/localhost/bracketed-IPv6 SCP hosts,
prose-embedded SCP refs, idempotency, benign npm aliases and version ranges,
end-to-end sentinel absence in scanner stdout and config JSON for four hostile
requirement lines, NuGet case-fold with curation preserved, provenance-selected
update keeps explicit `source: endoflife_date`, ambiguous provenance retains
conservatively, `_inventory.scan_timestamp` no longer generated, NuGet
decoded-byte budget present, `test_snapshot_properties_stay_unmapped`
registered and printed, stdlib-only constraint holds. 21 standalone tests
passed at `68e117b`; `compileall` OK; `terraform fmt` OK. These items are
carried forward as background context, not re-verified line-by-line here; the
port's own Task 2 report independently confirmed all `tests/test_*.py` and
`tests/check_*.py` scripts (38, including main's own) pass on the ported tree.

## Suggested first fix (unchanged from the source review)

A guard test that fails when any `test_*` function in a `tests/test_*.py` file
is defined but not registered in `TESTS`, or is defined twice. Both the
shadowed regressions (new finding 1) and the unregistered test (new finding 2 /
Standards finding 2) would have been caught by it. Not implemented by the port
— out of scope per Global Constraint 10 ("the port is a move, not a
remediation").

## What the port did and did not change

- Did NOT fix any behavioural finding above (Confirmed-prior 1-5, New 1-2 and
  4, Standards 2-5). This is intentional per Global Constraint 10 of the port
  plan.
- DID fix, as part of this same docs commit, the two Hard/stale AGENTS.md
  statements the port introduced or inherited: the false "former root-level
  generator script has been removed" claim, and the package-layout/Key-files
  omissions of `eol_inventory`'s `redact.py`/`config_io.py`, the three new
  providers, and `core.py`'s bounded-JSON/HTTP helpers.
- New finding 3 (report.py alerts scope creep) is moot in this tree because
  the behaviour is `main`'s own pre-existing feature, already accurately
  documented in this repository's `README.md`.
