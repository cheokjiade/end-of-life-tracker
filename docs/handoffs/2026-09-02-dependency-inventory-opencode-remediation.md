# Dependency-inventory final remediation handoff for OpenCode

## Audience and outcome

This document is for the OpenCode agent that will remediate the final Codex Sol
audit findings. Work locally in the existing implementation worktree. Produce
small verified commits, run the required read-only OpenCode Go reviews, and
stop at a clean committed checkpoint. Do not claim the final Codex gate is
clean: the user will manually start Codex Sol again after OpenCode finishes.

The objective and completed implementation are already documented in
`docs/plans/2026-08-28-project-dependency-inventory.md`. Do not redesign or
reimplement the scanner. This pass is limited to the verified findings below.

## Suggested skills

Read each selected `SKILL.md` completely before changing code.

- `manage-eol-config`: required for `--update`, curation, and inventory model
  behavior.
- `add-eol-provider`: required for the NuGet provider budget fix.
- `review`: use for the final standards/spec check if it is available in the
  OpenCode environment.

The canonical repository skills are under `.agents/skills/`. `ask-opencode` is
not needed when the continuing agent is already running inside OpenCode.

## Required repository instructions

Read and follow these files before editing:

- `AGENTS.md`
- `docs/codex-usage-efficient-workflow.md`
- `docs/commit-conventions.md`
- `.agents/skills/manage-eol-config/SKILL.md`
- `.agents/skills/add-eol-provider/SKILL.md`

Important constraints:

- Never push, rebase, reset, amend, force-push, or rewrite history.
- Do not modify the user's unrelated checkout at `E:\Git\endoflife`.
- Stage only files belonging to the current batch. Never use `git add -A`.
- Tests are network-free. Do not install dependencies or call registries.
- Do not fast-forward `codex/dependency-inventory`; leave that for Codex after
  the final manual Sol audit.
- Every bug-fix commit requires one fresh read-only adversarial review as
  specified by `docs/commit-conventions.md`.

## Starting state

- Implementation worktree:
  `C:\Users\Me\.codex\worktrees\aa82\endoflife`
- Audited implementation checkpoint:
  `2fa56b6d6aa7b96c3728f12d796941f938b80490`
- Earlier OpenCode remediation base: `10c0099`
- Whole feature baseline: `4988e1d`
- Named branch: `codex/dependency-inventory`
- Named branch worktree:
  `C:\Users\Me\AppData\Local\Temp\endoflife-dependency-inventory`
- The named branch intentionally remains at `4988e1d`.

This handoff document may be committed immediately after `2fa56b6`. Treat
`2fa56b6` as the implementation audit base and apply remediation on top of the
current clean HEAD. Confirm the relationship before editing:

```powershell
git status --short --branch
git log --oneline --decorate -5
git merge-base --is-ancestor 2fa56b6 HEAD
```

Stop if the implementation checkpoint is not an ancestor, the worktree has
unexpected changes, or the named branch has moved. Report the discrepancy
instead of guessing.

Initialize the Python command once before running any focused checks:

```powershell
$Py = 'C:/Users/Me/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'endoflife-opencode-pycache'
```

## Verification already completed at `2fa56b6`

Codex ran the following successfully without changing the worktree:

- all 22 standalone `tests/*.py` scripts;
- `python -m compileall -q eoltracker helper_scripts tests` with bytecode sent
  outside the worktree;
- `terraform fmt -check terraform` with `TF_CLI_CONFIG_FILE=NUL`;
- Bash and PowerShell noninteractive wrapper coverage in
  `tests/test_helper_wrappers.py`; and
- `git diff --check 10c0099...HEAD`.

The final Codex Sol/max review was not clean. It reported five Standards
findings and two Spec findings. One secret-redaction defect appeared in both
reports, so there are six distinct defects to address.

## Work order

Use the following four batches. Do not combine the provider, update-matching,
or timestamp work with the security boundary batch. After each commit, run one
fresh adversarial review of that commit against its first parent. If the review
finds an actionable bug, fix it in a new commit and repeat with a fresh review.

### Batch 1: close all remaining output-boundary secret leaks

This batch contains three tightly related corrections.

#### 1A. Redact SCP-style Git and SSH references

Severity: **High**.

Relevant code:

- `helper_scripts/eol_inventory/redact.py`, especially
  `redact_dependency_ref()` around lines 162-179;
- `helper_scripts/eol_inventory/parsers/python.py`, especially the direct URL
  warning around lines 172-191; and
- `tests/test_inventory_redaction.py`.

Verified failure:

```text
widget @ git@github.com:private/repo.git#token-SECRET
```

The raw `git@github.com:...` reference survives `redact_dependency_ref()` and
is copied into `_inventory.warnings` and scanner stdout. Current logic only
recognizes `ssh://` and `git+ssh://` prefixes.

Required behavior:

- Recognize SCP-style Git/SSH references such as
  `git@github.com:group/private.git` and `user@host:path`.
- Collapse the entire user/path/query/fragment portion. A safe result such as
  `<ssh:github.com>` is acceptable; the original repository path and fragment
  must not survive.
- Use the existing host validation in `ssh_placeholder()` where practical.
- Preserve ordinary exact versions, ranges, PEP 508 markers, npm aliases, and
  local paths that are not credential-shaped.
- Redaction must be idempotent.
- No raw reference may reach normalized records, structured warnings, config
  JSON, Markdown, CSV, HTML, or stdout.

Add network-free regressions covering at least:

1. a requirements direct reference with `git@host:path#fragment`;
2. an editable SCP-style reference;
3. direct calls to `redact_dependency_ref()`;
4. serialization of records and warnings; and
5. a benign version/range that remains byte-identical.

Avoid an over-broad rule that treats every string containing `@` and `:` as
SSH. Hosted package aliases and version constraints must keep working.

#### 1B. Redact every report metadata and warning field

Severity: **Medium**.

Relevant code:

- `helper_scripts/eol_inventory/report_writer.py`, especially warning view
  construction around lines 280-285 and metadata around lines 331-343;
- the existing `_scalar_text()` and `_redacted_text()` helpers in that module;
- `tests/test_inventory_report.py`; and
- `tests/test_output_controls.py`.

Verified failure: a hostile legacy config containing a credential URL in a
warning `category`, warning `path`, `scan_date`, `scan_timestamp`,
`generator_version`, `scan_root`/project, or files count can retain the secret
in the normalized view and the Markdown/HTML report. Only warning `message` is
currently redacted at view construction.

Required behavior:

- Treat loaded config content as untrusted, even for fields the scanner would
  normally generate itself.
- Coerce each displayed warning and metadata value to safe scalar text, then
  apply `redact_urls()` before it enters the normalized view.
- Cover warning `category`, `path`, and `message`, plus every value under
  `view["meta"]` that can be rendered.
- Retain the existing control-character normalization at Markdown/CSV/HTML
  output boundaries.
- Preserve normal ASCII metadata byte-for-byte.
- Malformed lists/dicts must not raise `TypeError` or `RecursionError`.

Add one hostile-config regression that places a distinct credential-bearing
URL in every affected field. Assert the secret is absent from the normalized
view and all three report formats.

#### 1C. Register the existing path-linearity regression

Severity: **Low**, but mandatory.

`tests/test_inventory_redaction.py` defines the path-scan linearity test around
line 716, but the function is absent from the standalone `TESTS` list near line
1280. Add the exact function to `TESTS` and verify it executes when running the
script directly. Do not merely call it from a different test.

Suggested commit subject:

```text
fix(inventory): close remaining report redaction gaps
```

Focused checks before committing:

```powershell
& $Py tests/test_inventory_redaction.py
& $Py tests/test_inventory_report.py
& $Py tests/test_output_controls.py
& $Py tests/test_inventory_python.py
git diff --check
```

The adversarial reviewer should try SCP-style refs with ports, fragments,
multiple `@` characters, malformed hosts, nested URL text, non-string report
metadata, and already-redacted placeholders. It must also verify benign
constraints are unchanged.

### Batch 2: bound cumulative NuGet decompression work

Severity: **Medium**.

Relevant code:

- `eoltracker/parsers/nuget_registry.py`, especially `_NugetBudget` around
  lines 62-87 and `_http_get_json()` around lines 108-130; and
- `tests/test_nuget_registry.py`, especially gzip tests around line 470 and
  cumulative budget tests around line 540.

Verified failure: `_NUGET_MAX_TOTAL_BYTES` currently charges compressed wire
bytes only. As many as 256 individually valid gzip responses may each expand
up to the 16 MiB per-response limit, allowing roughly 4 GiB of cumulative
decompression and JSON parsing work in one lookup.

Required behavior:

- Keep the existing per-response compressed-body and gzip-expansion bounds.
- Retain the cumulative request and retained-leaf limits.
- Add a cumulative decoded/expanded-byte budget, or charge decoded bytes to an
  appropriately defined cumulative work budget.
- Enforce the expanded budget after decompression and before UTF-8 decoding or
  `json.loads()`.
- Non-gzip responses must also count toward decoded work.
- Exhaustion must stop the lookup and surface the provider's normal explicit
  `error` result. It must not cache a partial package result.
- Update module comments so wire-byte and decoded-byte budgets are not
  confused.

Add a network-free test using multiple small compressed responses whose
individual expanded sizes are legal but whose cumulative expanded size exceeds
the lookup budget. Assert that the later response is rejected, no later fetch
occurs, the thread-local budget is cleared, and no partial package cache entry
is installed.

Suggested commit subject:

```text
fix(provider): bound cumulative NuGet decompression
```

Focused checks before committing:

```powershell
& $Py tests/test_nuget_registry.py
& $Py tests/test_provider_http_bounds.py
& $Py tests/test_configs_have_notes.py
git diff --check
```

The adversarial reviewer should test many high-ratio gzip bodies, the exact
budget boundary, corrupt gzip, short-reading streams, non-gzip aggregation,
cache hits, and exception cleanup. No network access is allowed.

### Batch 3: match changed multi-version rows by unique provenance

Severity: **Medium**.

Relevant code:

- `helper_scripts/generate_config.py`, `_merge_existing_config()` around lines
  134-233, especially the filter at lines 176-180; and
- `tests/test_generate_config.py`, beside the existing update tests beginning
  around line 1230.

Verified failure: when an existing config has two tracked rows with the same
provider/package identity and both versions change, exact version matching
finds nothing. The provenance candidates are then discarded unless either row
is generated-unmapped. The merge retains both stale rows and adds both fresh
rows.

Concrete regression shape:

```text
existing: shared 1.0.0 at a/pom.xml, shared 2.0.0 at b/pom.xml
fresh:    shared 1.1.0 at a/pom.xml, shared 2.1.0 at b/pom.xml
```

Current incorrect result:

```text
versions: 1.0.0, 2.0.0, 1.1.0, 2.1.0
summary: added=2, changed=0, retained_not_observed=2
```

Required result:

```text
versions: 1.1.0, 2.1.0
summary: added=0, changed=2, unchanged=0, retained_not_observed=0
```

Required behavior:

- For multiple fresh rows with the same merge identity, allow an old tracked
  row to match a fresh tracked row when their stable provenance intersection
  identifies exactly one unused candidate.
- Preserve the normal tracked-row merge behavior and curated fields. Do not
  treat a tracked-to-tracked version change as an unmapped remap.
- Continue retaining the old row when provenance is absent or ambiguous.
- Preserve the conservative behavior of legacy bare Docker `FROM` locators.
- Do not use line numbers as stable identity; existing provenance-key behavior
  intentionally ignores line movement through its locator design.
- Keep all existing unmapped-to-tracked and tracked-to-unmapped tests green.

Add at least two regressions:

1. two tracked versions both changing at distinct provenance sites; and
2. the same identity with ambiguous/identical provenance, which must retain the
   old rows rather than guess.

Suggested commit subject:

```text
fix(config): match changed multi-version rows by provenance
```

Focused checks before committing:

```powershell
& $Py tests/test_generate_config.py
& $Py tests/test_inventory_integration.py
& $Py tests/test_inventory_containers.py
git diff --check
```

The adversarial reviewer should vary ordering, line movement, missing locators,
duplicate declaration sites, partially changed version sets, curated fields,
manual rows, and Docker legacy provenance.

### Batch 4: restore deterministic generated state

Severity: **Medium**.

Relevant code:

- `helper_scripts/eol_inventory/config_writer.py`, metadata construction around
  lines 450-469;
- `tests/test_inventory_integration.py`, deterministic test around lines
  193-202; and
- `tests/test_inventory_report.py`, optional timestamp compatibility tests.

Verified failure: `generate_config()` stores
`datetime.now().astimezone().isoformat()` as `_inventory.scan_timestamp`.
Identical inputs therefore produce different configs. The determinism test
deletes that field before comparing outputs, hiding the difference.

The authoritative project plan requires a **scan date**, not a wall-clock
timestamp (`docs/plans/2026-08-28-project-dependency-inventory.md`, report
requirements around line 417). Recommended resolution:

- Stop generating `_inventory.scan_timestamp`.
- Keep `_inventory.scan_date` and its existing report output.
- Keep report-reader compatibility for an optional `scan_timestamp` already
  present in older/intermediate configs; do not needlessly break those files.
- Remove the timestamp-normalization workaround from the deterministic test so
  it compares complete generated configs.
- Remove an unused `datetime` import if this change makes it unused.

Do not replace the timestamp with another un-injected wall-clock value. If a
different resolution is chosen, it must preserve complete-output determinism
and explain why it is more faithful to the plan.

Suggested commit subject:

```text
fix(inventory): keep generated metadata deterministic
```

Focused checks before committing:

```powershell
& $Py tests/test_inventory_integration.py
& $Py tests/test_inventory_report.py
& $Py tests/test_generate_config.py
git diff --check
```

The adversarial reviewer should compare complete config bytes across repeated
identical scans and confirm a legacy config containing `scan_timestamp` still
renders without error.

## OpenCode Go adversarial review protocol

For every bug-fix commit, use a fresh detached audit worktree and a new
read-only OpenCode session with exactly:

```text
provider/model: opencode-go/glm-5.3-flash
thinking: max
```

Give the reviewer:

- the original finding and reproduction;
- the exact base and fix commit hashes;
- `git diff FIX_COMMIT^..FIX_COMMIT` as the owned range;
- relevant requirements and focused tests; and
- an explicit prohibition on editing, staging, committing, pushing, or writing
  repository files. Temporary probes must go under the OS temp directory with
  bytecode disabled.

Ask it to disprove the fix using counterexamples, boundary/error paths,
regression risks, unsafe failure modes, and missing tests. Require each finding
to include severity, file/line, evidence, impact, and concrete remediation. A
summary or code-style review is insufficient.

Independently reproduce every reported issue before editing. Fix only verified
actionable findings in a follow-up commit. Repeat with another fresh read-only
review until clean or until the user explicitly accepts a documented residual
risk.

## Full final verification

If the shell was restarted, initialize the bundled Python command again:

```powershell
$Py = 'C:/Users/Me/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'endoflife-opencode-pycache'
```

Run every standalone test and stop on the first failure:

```powershell
Get-ChildItem tests -Filter '*.py' | Sort-Object Name | ForEach-Object {
    & $Py $_.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "failed: $($_.Name)"
    }
}
```

Then run:

```powershell
& $Py -m compileall -q eoltracker helper_scripts tests
$env:TF_CLI_CONFIG_FILE = 'NUL'
terraform fmt -check terraform
git diff --check 4988e1d...HEAD
git status --short --branch
```

`tests/test_helper_wrappers.py` must confirm both Bash and PowerShell
noninteractive wrapper paths. All tests must remain network-free. Perform only
proportionate local scanner/report smoke checks; do not make live registry calls
for these fixes.

## Completion gate and report to the user

OpenCode is finished only when:

- all six distinct defects above are fixed with regressions;
- each bug-fix commit has a fresh clean OpenCode Go adversarial review, or an
  explicitly accepted residual risk;
- the full verification suite passes;
- `git diff --check` passes;
- the implementation worktree is clean; and
- nothing has been pushed or fast-forwarded.

Report:

1. the exact final detached HEAD;
2. every remediation commit after this handoff, with one-line purpose;
3. each OpenCode Go review range and verdict;
4. full verification results;
5. any residual risk; and
6. confirmation that `codex/dependency-inventory` still points to `4988e1d`.

Then stop. Tell the user the checkpoint is ready for a manually triggered
Codex Sol/max whole-range review of `4988e1d..FINAL_HEAD`, split into
functional/spec and standards/security axes. OpenCode cannot perform or trigger
that final Codex gate.
