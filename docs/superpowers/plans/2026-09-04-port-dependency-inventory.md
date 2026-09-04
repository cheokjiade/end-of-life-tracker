# Plan: port the dependency-inventory work from the `endoflife` clone into `end-of-life-tracker`

## Context

Two clones diverged from one repo. `E:\Git\end-of-life-tracker` (this repo, GitHub
`cheokjiade/end-of-life-tracker`) is the main repo going forward. `E:\Git\endoflife`
(GitHub `cheokjiade/endoflife-tracker`) holds the dependency-inventory subsystem:
`helper_scripts/eol_inventory/`, three new providers (Go proxy, NuGet, PyPI), ~17
standalone tests, ~70 fixtures, a plan and two remediation handoffs, developed
2026-08-27..2026-09-04 as commits `e127dda..68e117b` (155 commits, 2 merges).

All of `endoflife`'s branches have been fetched LOCALLY into this repo's object store as
`refs/endoflife/*` (e.g. `refs/endoflife/codex/dependency-inventory-remediation` =
`68e117b`). They exist only on this machine and must never be pushed.

Synthetic merge base: `endoflife` commit `e127dda` ("feat(agents): centralize EOL
workflows under .agents", 2026-08-27) corresponds to this repo's `ab6bcf1` (same
subject); their trees differ only by `docs/audits/2026-08-27-security-risk-audit.md`,
three screenshot PNGs, one `.gitignore` line and one `terraform/main.tf` line.

A dry-run `git merge-tree --merge-base=e127dda main 68e117b` conflicts in exactly 10
files: `AGENTS.md`, `README.md`, `eoltracker/core.py`, `eoltracker/handler.py`,
`eoltracker/parsers/__init__.py`, `eoltracker/parsers/endoflife_date.py`,
`eoltracker/parsers/maven_central.py`, `eoltracker/report.py`, `terraform/main.tf`,
and root `generate_config.py` (deleted on the inventory side, modified on main).

Spec authority: the user's decisions recorded in Global Constraints. There is no separate spec file.

## Global Constraints

1. Work only in the worktree `E:\Git\end-of-life-tracker-worktrees\dependency-inventory-port`
   on branch `feat/dependency-inventory-port` (started from `main` = `12018d5`).
2. Never push. Never touch `main`. Never push or reference `refs/endoflife/*` in any
   commit message, branch name, or remote operation. The controller pushes at the end.
3. Preserve the original commits' authorship, dates, and messages when replaying. Do not
   add trailers or attribution to replayed commits. Commits you author yourself follow
   `docs/commit-conventions.md` and end with the trailer
   `Claude-Session: https://claude.ai/code/session_01Dxjfq5wEbszHksHxbxJZZB`.
4. User decision, KEEP BOTH GENERATORS: root `generate_config.py` stays exactly as on
   `main` (it is being extended by open PR #35). The inventory side's
   `helper_scripts/generate_config.py` plus `helper_scripts/eol_inventory/` are added
   alongside. Never delete or modify root `generate_config.py`.
5. User decision, CHERRY-PICK NOT SQUASH: replay the individual commits so the
   granular history survives. Merge commits are linearized (dropped); commits that
   become empty after conflict resolution are skipped.
6. Neither side regresses. Everything `main` has (runner, HTML runner, validation,
   delivery outcomes and observability, packaging allowlist, provider cache, runtime
   budget, health alerts, notify HTML mode, Maven repository fallback) keeps working, and
   everything the inventory side adds lands. Where the 10 shared files conflict, the
   resolution must contain BOTH sides' functionality, not one side's file.
7. Where `terraform/main.tf` conflicts on the Lambda package archive, keep `main`'s
   packaging approach (`build_lambda_package.py` plus verified allowlist, PR #20); carry
   over only inventory-side additions that do not replace it.
8. The runtime stays stdlib-only (`boto3` lazy import in the S3 loader is the one
   pre-existing exception). Python 3.9+ syntax only in `eoltracker/`.
9. Tests are standalone `python tests/test_x.py` scripts (no pytest). Every
   `tests/test_*.py` and `tests/check_*.py` from BOTH sides must exit 0 at the end of
   Task 2. `python -m compileall -q eoltracker helper_scripts tests` must pass.
   `terraform fmt -check terraform` must pass.
10. Do not fix the known review findings of the inventory work (they are carried forward
    as a handoff in Task 3). The port is a move, not a remediation. The one exception is
    anything that must change for the two sides to coexist.
11. Redirect Python bytecode outside the tree (`PYTHONDONTWRITEBYTECODE=1` or
    `PYTHONPYCACHEPREFIX=<scratch dir>`) so no `__pycache__` lands in the diff.

## Task 1: Replay `e127dda..68e117b` onto the port branch, resolving the shared-file conflicts

Files: everything the range touches; the 10 conflict files above get hand-merged.

Steps:
1. In the worktree, confirm `git status` is clean and HEAD is `feat/dependency-inventory-port`.
2. Run `git rebase --onto feat/dependency-inventory-port e127dda 68e117b`. This checks out
   `68e117b` detached and replays every non-merge commit in `e127dda..68e117b` onto the
   branch, in order. Do NOT use `--rebase-merges`.
3. At each conflict:
   - Root `generate_config.py` (modify/delete): keep `main`'s file with
     `git checkout 12018d5 -- generate_config.py` then `git add generate_config.py`.
     Same rule if any later commit touches root `generate_config.py`: keep `main`'s content.
   - The nine shared code/doc files: produce a merged file containing both sides'
     behaviour (Global Constraint 6). Read both sides fully before editing; do not accept
     "ours" or "theirs" wholesale. For `AGENTS.md` and `README.md` merge the sections;
     the "which generator" note is Task 3's job, but do not lose either side's text.
   - Run `python -m compileall -q eoltracker helper_scripts tests` (bytecode redirected)
     before `git rebase --continue` on any commit that touched `.py` files.
   - If a commit becomes empty, `git rebase --skip`.
   - Never `git rebase --abort` after progress; if truly stuck, stop and report BLOCKED
     with the commit hash and the conflict.
   - `git rebase --continue` opens an editor for the commit message only when the
     message must be re-edited; set `GIT_EDITOR=true` so it never blocks.
4. When the rebase finishes, `git branch -f feat/dependency-inventory-port HEAD` and
   `git checkout feat/dependency-inventory-port`.
5. Verify the port carried the inventory side faithfully:
   `git diff --stat 68e117b HEAD -- helper_scripts tests/fixtures docs/plans docs/handoffs eoltracker/parsers/go_proxy.py eoltracker/parsers/nuget_registry.py eoltracker/parsers/pypi_registry.py .gitattributes`
   must be EMPTY (those paths exist only on the inventory side). Include the command
   and its output in the report. Any non-empty output is a defect to fix before reporting.
6. Verify `git diff --stat 12018d5 HEAD -- generate_config.py` is EMPTY (Constraint 4).
7. Run every `tests/test_*.py` and `tests/check_*.py` as standalone scripts and record
   pass/fail per script in the report. Failures are EXPECTED at this stage for tests that
   exercise the hand-merged files; do not fix them here (Task 2 does), just list them with
   the first assertion or traceback line each.
8. Write the report to the path the controller gives you, with a section
   "Conflict log": one line per conflicted commit: hash, subject, files, and what
   the resolution kept from each side.

Done when: rebase complete, branch updated, steps 5 and 6 diffs empty, compileall OK,
per-test results recorded, conflict log written.

## Task 2: Make both sides' test suites green on the port branch

Files: the 9 hand-merged files from Task 1 and, if needed, tests on either side that
assert on merged behaviour.

Steps:
1. Read Task 1's report (path given by controller), especially the conflict log and the
   failing-test list.
2. For each failing script, find the root cause in the merged files. Fix the integration,
   not the test, unless the test asserts on one side's exact text that legitimately
   changed in the merge (then update the assertion and say so in the report).
3. Keep Constraint 6: no main-side feature removed, no inventory-side feature removed.
   Keep Constraint 10: do not remediate the known inventory review findings.
4. Small, labelled commits per `docs/commit-conventions.md`, e.g.
   `fix(port): reconcile handler config loading with runtime validation`. Each commit is
   your own, so it carries the trailer from Constraint 3.
5. Final gate, all from the worktree root with bytecode redirected:
   - every `tests/test_*.py` and `tests/check_*.py` exits 0 (list them all in the report
     with exit codes)
   - `python -m compileall -q eoltracker helper_scripts tests` exits 0
   - `terraform fmt -check terraform` exits 0 (skip with a note if terraform is missing)
   - `git diff --stat 12018d5 HEAD -- generate_config.py` is empty
   - the Task 1 step-5 inventory-only diff is still empty EXCEPT for files you had to
     change for coexistence; list each such file and why.

Done when: all five gate items hold and are evidenced in the report.

## Task 3: Docs for coexistence and carry the review findings forward as a handoff

Files: `AGENTS.md`, `README.md`, `docs/handoffs/2026-09-04-dependency-inventory-port-review-findings.md` (new).

Steps:
1. In `AGENTS.md` and `README.md`, add a short, factual note that two config generators
   currently coexist: root `generate_config.py` (the extractor being extended in PR #35)
   and `helper_scripts/generate_config.py` backed by `helper_scripts/eol_inventory/`
   (the multi-ecosystem inventory scanner ported from the `endoflife` clone on
   2026-09-04). State that consolidation is a follow-up decision. Fix any statement in
   `AGENTS.md` that the merge made stale (for example the config loader now decodes UTF-8
   and enforces bounds; the package layout table must list `eol_inventory`, `redact.py`,
   `config_io.py`, and the three new providers).
2. Create the handoff file from the source notes at the path the controller gives you
   (`review-findings-2026-09-04.md` in the SDD workspace). Follow the format of the two
   existing `docs/handoffs/2026-09-02-*.md` files. It must state: the source repo and
   range, that these findings were confirmed by reproduction on 2026-09-04 against
   `68e117b`, the 5 confirmed findings (1 High, 4 Medium), the 4 new findings, the 2
   standards findings, and that the port did NOT fix them. Rewrite file:line anchors to
   match the ported tree (line numbers may have shifted; verify each anchor).
3. Run `python tests/check_agent_docs.py` (if present) and any docs integrity test;
   they must pass.
4. One commit: `docs(port): note coexisting generators and carry inventory review findings`.

Done when: both docs updated, handoff present with verified anchors, docs tests pass.
