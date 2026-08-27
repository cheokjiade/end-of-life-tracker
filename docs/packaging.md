# Lambda packaging: allowlisted artifact build and verification

How the AWS Lambda deployment artifact (`terraform/build/lambda.zip`) is
produced, verified, and consumed by Terraform.

This replaces the former denylist packaging (a `data "archive_file"` that
archived the repository root minus a hand-maintained exclusion list), which
could silently include untracked secrets or unrelated future files in the
deployed ZIP — audit finding **S-01** in
[the security audit](2026-08-27-security-risk-audit.md).

## The allowlist

The artifact contains exactly:

- `lambda_function.py`
- every `*.py` under `eoltracker/`

Everything else in the repository is *structurally* excluded because it is
never collected — there is no exclusion list to fall out of date. Two special
collection rules keep builds predictable while staying fail-closed:

| Situation under `eoltracker/` | Behaviour |
|---|---|
| known compiled/junk artifacts (`__pycache__/`, `*.pyc`, `*.pyo`) | skipped silently (never runtime code) |
| symlink, Windows junction, or other reparse point | **build refuses to run** (prevents out-of-repository code inclusion) |
| any other non-`.py` file | **build refuses to run** (fail closed) |
| missing `lambda_function.py` or `eoltracker/` | **build refuses to run** |

A new non-Python runtime file therefore cannot enter the artifact unnoticed:
adding one requires a deliberate change to the allowlist in
`build_lambda_package.py`.

## Build

From the repository root:

```bash
python build_lambda_package.py build
```

Outputs under `terraform/build/` (gitignored; rebuildable):

- `lambda.zip` — the artifact
- `manifest.json` — SHA-256 of each input file, each ZIP entry, and the whole ZIP

The ZIP is deterministic: members are written sorted with fixed timestamps
and attributes, so two rebuilds from identical sources are byte-identical.
Only stdlib modules (`hashlib`, `json`, `zipfile`) are used.

**Rebuild whenever runtime code changes**, before any Terraform plan/apply.
An operator editing `eoltracker/` without rebuilding cannot deploy a stale
artifact — Terraform rejects it (below).

## Verify

```bash
python build_lambda_package.py verify          # exit 0 = artifact sound
python tests/test_packaging_manifest.py        # network-free regression suite
```

Verification is offline and checks, against the manifest *and* the live tree:

1. manifest exists and has a supported schema;
2. recorded inputs match the current runtime file set exactly (added/removed
   files fail);
3. recorded input hashes match the current file contents (edits fail);
4. the ZIP's SHA-256 matches the manifest (tamper/replacement fails);
5. the ZIP contains exactly the recorded member set — no extras, no missing
   entries, no duplicates;
6. each embedded entry's bytes hash both to its manifest value and to the
   current source snapshot.

Exit code `0` means the artifact is byte-for-byte the currently checked-out
runtime allowlist.

## Terraform integration

Terraform does not package code anymore. It consumes the prebuilt artifact
and enforces chain-of-custody via `lifecycle.precondition` blocks on
`aws_lambda_function.eol_checker` (see `terraform/main.tf`):

| Guard | Fails when |
|---|---|
| manifest present / schema 1 | artifact never built, or unknown format |
| input key set == working-tree allowlist | sources added/removed since last build |
| input hashes == working-tree hashes | source contents edited since last build |
| ZIP SHA-256 == manifest value | ZIP missing, corrupted, replaced, half-written |

Together these mean plan/apply only proceeds when *the deployed bytes equal
the checked-out runtime sources*, closing the non-clean-workspace hole the
denylist design had. The working-tree allowlist expectation is computed by
Terraform itself (`fileset` over `eoltracker/**/*.py` plus the shim), so a
future drift between Terraform's expectation and the script's allowlist also
fails loudly rather than silently shipping or dropping files.

Typical failure messages all end with the same remedy:

```bash
python build_lambda_package.py build
```

Deployment workflow summary:

```bash
python build_lambda_package.py build   # step 0: rebuild the artifact
cd terraform
terraform init && terraform apply      # preconditions re-check steps above
```

Note for CI: run `build` followed by `verify` (and the test suite) before
`terraform apply`; verification is network-free and safe on runners. A
half-completed build (ZIP without a fresh manifest) fails preconditions
instead of deploying, because the manifest records the ZIP's full hash at
write time and the ZIP writer replaces its output atomically.

## Residual risks

- A deliberately forged `manifest.json` matching an attacker-chosen ZIP would
  pass verification until rerun; treat out-of-band edits to
  `terraform/build/` as untrusted and rebuild instead of hand-patching.
- Files outside the allowlist roots (e.g. a hypothetical new top-level Python
  module next to `lambda_function.py`) ship only if someone extends the
  allowlist in `build_lambda_package.py`; omission fails as a runtime error in
  Lambda rather than a packaging error, so keep runtime surface inside the
  documented roots.
