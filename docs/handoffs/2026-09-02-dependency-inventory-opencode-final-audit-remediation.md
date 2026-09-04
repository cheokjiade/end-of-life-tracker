# Dependency-inventory final-audit remediation handoff for OpenCode

## Purpose

This is a delta handoff for the OpenCode agent continuing the dependency-
inventory work after the final Codex Sol review of checkpoint `5a95537`.
The review was not clean. Fix only the six distinct defects in this document,
verify each coherent batch, run the required OpenCode Go adversarial reviews,
and stop at a clean committed checkpoint for another user-triggered Codex Sol
review.

Do not reimplement the feature or repeat completed work. Background and full
acceptance criteria remain in:

- `docs/plans/2026-08-28-project-dependency-inventory.md`
- `docs/handoffs/2026-09-02-dependency-inventory-opencode-remediation.md`
- `AGENTS.md`

## Suggested skills

Read each selected `SKILL.md` completely before editing.

- `manage-eol-config`: required for update matching, curation preservation,
  unmapped inventory retention, and report behavior.
- `add-eol-provider`: consult only if provider code is touched. The previously
  reported NuGet decompression-budget defect is already fixed; do not redesign
  that provider without a newly reproduced problem.
- `review`: use for read-only standards/spec reviews if available in OpenCode.

The canonical project skills are under `.agents/skills/`. `ask-opencode` is not
needed when the continuing agent is already inside OpenCode.

## Mandatory instructions and safety

Before editing, read:

- `AGENTS.md`
- `docs/codex-usage-efficient-workflow.md`
- `docs/commit-conventions.md`
- `.agents/skills/manage-eol-config/SKILL.md`
- `.agents/skills/add-eol-provider/SKILL.md` if provider code is touched

Constraints:

- Work only in
  `C:\Users\Me\.codex\worktrees\aa82\endoflife`.
- Preserve the unrelated user checkout at `E:\Git\endoflife`.
- Never push, rebase, reset, amend, force-push, rewrite history, or delete
  branches/worktrees.
- Never use `git add -A`; stage only explicit paths or hunks for the batch.
- Keep tests network-free. Do not install packages or call live registries.
- Do not fast-forward `codex/dependency-inventory`. The user will ask Codex to
  do that only after a clean final Sol review.
- Commit every verified coherent batch using `docs/commit-conventions.md`.
- After every bug-fix commit, run exactly one fresh read-only adversarial
  review. Resolve actionable findings in follow-up commits; never amend.

## Starting checkpoint

- Clean detached implementation HEAD:
  `5a95537169e32c58f625dd22f819de7f913f790a`
- Prior handoff document commit: `1d9d5fa`
- Whole feature baseline: `4988e1d`
- Named branch: `codex/dependency-inventory`
- Named branch remains at:
  `4988e1d7eb324a4d512801fd0ede55a299555ba8`
- Named branch worktree:
  `C:\Users\Me\AppData\Local\Temp\endoflife-dependency-inventory`

This document may be committed immediately after `5a95537`. Treat `5a95537`
as the immutable code-review base and apply fixes on top of the current clean
HEAD. Confirm before editing:

```powershell
git status --short --branch
git log --oneline --decorate -8
git merge-base --is-ancestor 5a95537 HEAD
git rev-parse codex/dependency-inventory
```

Stop and report rather than guessing if:

- `5a95537` is not an ancestor of current HEAD;
- the worktree contains unexpected changes; or
- `codex/dependency-inventory` no longer resolves to `4988e1d`.

Initialize the bundled Python command once:

```powershell
$Py = 'C:/Users/Me/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'endoflife-opencode-final-pycache'
```

## Verification state at `5a95537`

Codex independently ran and passed:

- all 22 standalone `tests/*.py` scripts;
- Bash and PowerShell wrapper execution through
  `tests/test_helper_wrappers.py`;
- `python -m compileall -q eoltracker helper_scripts tests`;
- `terraform fmt -check terraform` with `TF_CLI_CONFIG_FILE=NUL`; and
- `git diff --check 4988e1d...HEAD`.

Passing tests do not clear the defects below; several are missing regressions.
Both final Sol reviewers kept the worktree unchanged and clean at `5a95537`.

Previously requested fixes that are verified and should remain intact:

- cumulative NuGet wire and decoded-byte budgets are bounded;
- generated `_inventory.scan_timestamp` was removed while legacy report input
  remains readable;
- warning/meta HTTP URL userinfo, query, and fragment material is redacted;
- the path-redaction linearity regression is registered and executes; and
- full all-changed multi-version updates work when every row has unambiguous
  provenance.

## Work plan

Use four bug-fix batches plus one small test-registration batch. Run focused
tests before each commit. Then use a fresh OpenCode Go reviewer against that
commit's first parent. Do not rely on any prior review session.

### Batch 1: finish the centralized SSH/SCP redaction boundary

Severity: **High**.

Relevant code:

- `helper_scripts/eol_inventory/redact.py`, especially `_SSH_PREFIX_RE`,
  `_SCP_REF_RE`, `redact_dependency_ref()`, and `ssh_placeholder()`;
- `helper_scripts/eol_inventory/parsers/python.py`, direct-reference warnings;
- `helper_scripts/eol_inventory/report_writer.py`, warning and metadata view
  construction; and
- `tests/test_inventory_redaction.py`, `tests/test_inventory_report.py`, and
  `tests/test_output_controls.py`.

Three related bypasses were independently reproduced.

#### Case 1: URI schemes are case-insensitive

Current incorrect behavior:

```text
SSH://git@host.invalid/private/repo.git#fragment
  -> SSH://<redacted>@host.invalid/private/repo.git#<redacted>

Git+SSH://git@host.invalid/private/repo.git?credential=value
  -> Git+SSH://<redacted>@host.invalid/private/repo.git?<redacted>
```

The credential material is hidden, but the private repository path remains.
Lowercase `ssh://` correctly collapses to `<ssh:host.invalid>`. The branch in
`redact_dependency_ref()` and `_SSH_PREFIX_RE` are case-sensitive even though
URI schemes are not.

Required result: every case variant of `ssh://` and `git+ssh://` collapses to a
host-only placeholder. No username, port credential, path, query, or fragment
may survive.

#### Case 2: valid IPv4 SCP hosts are excluded

Current incorrect behavior:

```text
git@192.0.2.1:private/secret-repo.git
git@10.0.0.1:credential/private.git
```

Both can survive byte-identically because `_SCP_REF_RE` requires a letter in
an unbracketed host. An actual requirements scan retained the full reference in
the structured warning, generated config, scanner stdout, Markdown, and HTML.

Required result: recognize valid IPv4 SCP hosts and collapse the entire token
to a host-only SSH placeholder. Preserve benign numeric version/alias strings
such as the existing `user@1.2.3:x` doctrine unless the complete token is a
valid host/reference shape. Use a structured IPv4 check or `ipaddress` from the
standard library rather than a broad "digits and dots" match.

#### Case 3: report metadata uses URL-only redaction

`build_inventory_view()` applies `redact_urls()` to warning category/path/
message and metadata. A hostile or legacy config value containing a
scheme-less SCP reference such as:

```text
git@github.com:credential/private.git#fragment
```

survives into the normalized view and rendered reports because it has neither
`://` nor `user:password@host` URL authority syntax.

Required result: one centralized, bounded display sanitizer must protect all
untrusted report warning and metadata fields from both URL and embedded
SSH/SCP/VCS reference material. It must work when the reference is the whole
field or embedded in surrounding prose. It may collapse an unsafe reference
aggressively, but normal dates, counts, project names, paths, package aliases,
and already-redacted placeholders must remain stable.

Do not fix only the two supplied strings. Test the grammar boundary:

- lowercase, uppercase, and mixed-case SSH schemes;
- IPv4, DNS, localhost, and bracketed IPv6 hosts;
- port-like and malformed hosts;
- with and without query/fragment material;
- SCP references embedded in warning prose;
- hostile values in every warning and rendered metadata field;
- repeated redaction (idempotency);
- long hostile strings (bounded/linear behavior); and
- benign versions, npm aliases, digest anchors, and local paths.

End-to-end assertions must prove the sentinel is absent from:

- parser records and warnings;
- config JSON;
- scanner stdout;
- normalized inventory view; and
- Markdown, CSV, and HTML reports.

Suggested commit subject:

```text
fix(inventory): close remaining SSH redaction bypasses
```

Focused checks:

```powershell
& $Py tests/test_inventory_redaction.py
& $Py tests/test_inventory_python.py
& $Py tests/test_inventory_report.py
& $Py tests/test_output_controls.py
git diff --check
```

The post-commit reviewer must attempt case-folding tricks, numeric hosts,
partial tokens, embedded prose, multiple anchors, exotic whitespace, and
over-redaction of legitimate aliases. Temporary probes only; no repo edits.

### Batch 2: make update matching provenance-safe and NuGet-aware

Severity: **Medium**.

Relevant code:

- `helper_scripts/generate_config.py`, `_merge_identity()` and
  `_merge_existing_config()`; and
- `tests/test_generate_config.py`, update tests near the end of the file.

This batch has two related identity/matching defects.

#### Case 1: partial multi-version changes swap curation between sites

Reproduction using two rows with the same provider/package identity:

```text
existing:
  a/pom.xml -> version 2.0, policy A
  b/pom.xml -> version 1.0, policy B

fresh scan:
  a/pom.xml -> version 3.0
  b/pom.xml -> version 2.0
```

Current incorrect result:

```text
b/pom.xml -> version 2.0, policy A
a/pom.xml -> version 3.0, policy B
update_summary: changed=1, unchanged=1
```

The exact-version match runs before provenance, so the old row at site A is
matched onto the new row at site B. Both actual site upgrades occurred, but
curation moves to the wrong component and one upgrade is reported unchanged.

Required result:

```text
a/pom.xml -> version 3.0, policy A
b/pom.xml -> version 2.0, policy B
update_summary: added=0, changed=2, unchanged=0,
                retained_not_observed=0
```

Matching rules:

1. When an identity has multiple unused fresh candidates, prefer a unique
   stable-provenance match before an exact-version fallback.
2. A tracked-to-tracked provenance match is a normal same-component update.
   Merge from the old row and update with the fresh row, then reapply documented
   curated fields. Do not set unmapped-remap semantics for this path.
3. Use identity-wide exact-version matching only when provenance cannot safely
   distinguish candidates.
4. Ambiguous provenance must retain conservatively; never guess.
5. Preserve legacy bare Docker `FROM` behavior and existing transitions between
   tracked and generated-unmapped rows.

The current unique-provenance branch sets `remapped=True` for tracked-to-
tracked matches. This can drop an explicit default `source` and other old
metadata merely because one old row happened to be processed while several
candidates remained. Split "selected by provenance" from "mapping type
changed" so matching order does not change merge semantics.

Add regressions for:

- the crossing partial-upgrade case above;
- all versions changed;
- only one version changed;
- order reversal on both sides;
- unique, missing, and ambiguous provenance;
- site-specific `_comment`, `policy_note`, `note`, and `reference_url`;
- an explicit `source: endoflife_date` when the fresh row uses the default;
- legacy bare Docker provenance; and
- tracked/unmapped transitions.

#### Case 2: NuGet IDs are case-insensitive

Current incorrect behavior:

```text
existing: Newtonsoft.Json 13.0.2 at one site
fresh:    newtonsoft.json 13.0.3 at the same site
result:   old retained + new added
```

`config_writer._entry_key()` already lowercases NuGet package IDs, and the
authoritative plan says NuGet IDs match case-insensitively while display casing
is preserved. `_merge_identity()` does not normalize them.

Required result: one changed row with its curation preserved and fresh display
casing/provenance. Case-fold only the identity key for `nuget_registry`; do not
lowercase the emitted package or label.

Suggested commit subject:

```text
fix(config): preserve identity and curation across updates
```

Focused checks:

```powershell
& $Py tests/test_generate_config.py
& $Py tests/test_inventory_integration.py
& $Py tests/test_inventory_go_dotnet.py
& $Py tests/test_inventory_containers.py
git diff --check
```

The post-commit reviewer must try crossing version sets, duplicate sites,
partial removals, case-only NuGet changes, order permutations, stale generated
rows, and curated fields that differ per declaration.

### Batch 3: keep retained untracked inventory visible

Severity: **Medium**.

Relevant code:

- `helper_scripts/generate_config.py`, retained-not-observed merge path and
  `_inventory` merge behavior;
- `helper_scripts/eol_inventory/report_writer.py`, product suppression around
  lines 296-311; and
- `tests/test_generate_config.py` and `tests/test_inventory_report.py`.

Reproduction:

1. An existing generated config contains a scanner-generated manual product
   marked `_inventory_generated: "unmapped"` and a matching old
   `_inventory.unmapped` item.
2. A later scan no longer observes that dependency and produces fresh
   `_inventory.unmapped: []`.
3. `--update` correctly retains the old product as
   `retained_not_observed`.
4. `build_inventory_view()` suppresses that product merely because any
   `_inventory` object exists, then has no structured unmapped item to render.

Current report result: zero products and zero unmapped items. The dependency is
still in the config but invisible in Markdown/CSV/HTML, violating the plan's
"never silently drop inventory" rule.

Recommended behavior: when a generated-unmapped product is retained as
unobserved, carry forward its matching structured `_inventory.unmapped`
metadata into the merged inventory. This keeps it in the proper untracked
report section instead of incorrectly presenting it as a tracked manual
product. Deduplicate against fresh structured items using stable provenance and
identity. If a different approach is used, suppress a product only when an
equivalent structured item will actually render.

Required regressions:

- one retained generated-unmapped row remains visible as untracked;
- its old reason and provenance remain present;
- it is counted once, not duplicated;
- a currently observed unmapped row is still counted once;
- two same-name rows at distinct sites do not borrow each other's metadata;
- a curated manual row not generated by the scanner remains a product; and
- Markdown, CSV, and HTML agree with the normalized view and checklist counts.

Suggested commit subject:

```text
fix(report): retain unobserved untracked inventory
```

Focused checks:

```powershell
& $Py tests/test_generate_config.py
& $Py tests/test_inventory_report.py
& $Py tests/test_inventory_integration.py
git diff --check
```

The post-commit reviewer should probe stale/fresh collisions, legacy configs
without structured unmapped data, changed reasons, duplicate provenance, and
curated manual entries.

### Batch 4: align helper and runtime config-loading contracts

Severity: **Medium**.

Relevant code:

- `helper_scripts/eol_inventory/config_io.py`;
- `eoltracker/handler.py`, both `load_config_from_s3()` and
  `load_config_from_file()`;
- `eoltracker/core.py` only if a genuinely shared runtime-safe primitive is
  appropriate; and
- runtime/config input tests.

Two related runtime failures were reproduced.

#### Case 1: valid generated configs can exceed Lambda's limit

The scanner supports up to 5,000 manifests/provenance sites. Generated configs
at that advertised bound can exceed 2,000,000 bytes. Two independent probes
produced approximately 2.26 MB for one product with 5,000 provenance sites and
4.60 MB for many simple unmapped dependencies.

The helper loader accepts up to 20 MB:

```text
helper_scripts/eol_inventory/config_io.py
MAX_CONFIG_FILE_BYTES = 10 * MAX_FILE_BYTES
```

The Lambda S3 loader rejects above 2 MB:

```text
eoltracker/handler.py
_MAX_CONFIG_BYTES = 2_000_000
```

Terraform can upload the generated file, but the deployed Lambda then rejects
it before checking any product. A helper-generated config must remain usable by
the tracker.

#### Case 2: runtime JSON lacks the helper's nesting guard

A small valid JSON document containing an unused key nested around 10,000
arrays raises `RecursionError` in both S3 `json.loads()` and local
`json.load()`. The helper CLIs reject nesting deeper than 100 with a clear
`ConfigLoadError` before recursive JSON parsing.

Required behavior:

- Establish one documented maximum config size that accommodates every valid
  scanner output at its advertised scan bounds while remaining finite.
- Enforce equivalent size and nesting rules in helper update/report loading,
  Lambda S3 loading, and local tracker loading.
- Runtime code must remain standard-library-only; do not import helper modules
  into the Lambda package.
- Use the existing bounded response reader for S3 bodies.
- Reject excessive size/depth and invalid/non-object JSON with a clear error,
  never `RecursionError`, partial data, or silent truncation.
- Preserve ASCII-safe generated files and Windows local loading behavior.
- Do not increase a limit without a regression proving maximum supported
  scanner output fits beneath it.

Prefer a runtime-owned bounded JSON primitive or clearly shared constants with
tests proving helper/runtime parity. Remember Terraform intentionally excludes
`helper_scripts/`; Lambda runtime code cannot depend on files that will not be
packaged.

Required network-free tests:

- a generated config at the supported 5,000-file/provenance bound is accepted
  by the runtime parser;
- one byte over the common limit is rejected before JSON parsing;
- depth exactly at the limit is accepted and one level over is rejected;
- deeply nested small JSON raises a documented error, not `RecursionError`;
- fake short-reading S3 body streams are handled correctly;
- local and S3 loaders enforce the same contract; and
- malformed JSON and non-object top-level values fail clearly.

Avoid tests that allocate hundreds of megabytes. Construct the smallest
representative generated config that exceeds the former 2 MB cap, and use
patched test limits where appropriate for exact boundary cases.

Suggested commit subject:

```text
fix(config): align runtime and helper input bounds
```

Focused checks:

```powershell
& $Py tests/test_cli_input_safety.py
& $Py tests/test_provider_safety.py
& $Py tests/test_generate_config.py
& $Py tests/test_inventory_integration.py
git diff --check
```

The post-commit reviewer should test exact size/depth boundaries, short reads,
UTF-8 failures, non-object JSON, local/S3 parity, and Terraform runtime
packaging. It must remain network-free.

### Batch 5: register the existing snapshot-property regression

Severity: **Low**. This is a test-only correction and does not require a bug-
fix adversarial review unless production behavior is changed in the same
commit.

`tests/test_generate_config.py` defines
`test_snapshot_properties_stay_unmapped` around line 860, but the function is
absent from the standalone `TESTS` list near the bottom of the file. Add the
exact function to `TESTS` and run the script directly. Confirm its name appears
in stdout so the check is not accidentally nested or shadowed.

Suggested commit subject:

```text
test(config): run snapshot-property regression
```

Focused check:

```powershell
& $Py tests/test_generate_config.py
```

## OpenCode Go adversarial review protocol

For every bug-fix commit, create a fresh detached audit worktree and use a new
read-only OpenCode session with exactly:

```text
provider/model: opencode-go/glm-5.3-flash
thinking: max
```

The reviewer prompt must include:

- the original reproduction and expected behavior from this document;
- the exact base and fix commit hashes;
- `git diff FIX_COMMIT^..FIX_COMMIT` as the owned diff;
- relevant standards/spec paths and focused tests; and
- an explicit prohibition on editing, staging, committing, pushing, or writing
  repository files. Any probes go under the OS temporary directory with
  bytecode redirected outside the repository.

Ask the reviewer to disprove the fix, not summarize it. Require boundary and
counterexample testing, error paths, regression risks, unsafe failure modes,
and missing-test analysis. Findings must include severity, exact file/line,
evidence, impact, and concrete remediation.

Independently reproduce every reported issue before changing code. Fix only
verified actionable findings in follow-up commits, run focused tests, and use a
fresh reviewer again. Do not mark a batch complete until the review is clean or
the user explicitly accepts a documented residual risk.

## Full final verification

Run every standalone test and stop on the first failure:

```powershell
Get-ChildItem tests -Filter '*.py' | Sort-Object Name | ForEach-Object {
    & $Py $_.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "failed: $($_.Name)"
    }
}
```

Confirm the newly registered snapshot-property test name appears in the
`test_generate_config.py` output. Then run:

```powershell
& $Py -m compileall -q eoltracker helper_scripts tests
$env:TF_CLI_CONFIG_FILE = 'NUL'
terraform fmt -check terraform
git diff --check 4988e1d...HEAD
git status --short --branch
git rev-parse HEAD
git rev-parse codex/dependency-inventory
```

`tests/test_helper_wrappers.py` must exercise both Bash and PowerShell
noninteractive runners. Keep all tests network-free. Run proportionate local
scanner/config/report smoke checks only; these fixes do not require live
registry access.

## Completion gate

OpenCode is finished only when:

- all six distinct defects are fixed with network-free regressions;
- every bug-fix commit has a clean fresh OpenCode Go adversarial review, or the
  user explicitly accepts a documented residual risk;
- the full verification suite passes;
- the implementation worktree is clean;
- `codex/dependency-inventory` still resolves to `4988e1d`; and
- nothing has been pushed or fast-forwarded.

Report to the user:

1. exact final detached HEAD;
2. every remediation commit after this handoff and its purpose;
3. every OpenCode Go audit range and verdict;
4. complete verification results, including the registered snapshot test;
5. any residual risks; and
6. confirmation that no branch was advanced and nothing was pushed.

Then stop. Tell the user to manually start Codex Sol/max for two fresh,
read-only whole-range reviews of `4988e1d..FINAL_HEAD`: one functional/spec and
one standards/security. OpenCode cannot trigger that final Codex gate.
