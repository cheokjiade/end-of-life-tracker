# Codex usage-efficient workflow

Use this process when running work in **Codex** and conserving the user's Codex
allowance matters. It is an execution constraint, not a reason to reduce
correctness, skip required verification, or weaken repository safety rules.

## Scope

This process applies to:

- the primary Codex task;
- native Codex subagents and delegated Codex tasks;
- Codex tool calls, approvals, reviews, waits, and synthesis turns;
- Codex acting as an orchestrator for another tool.

It does **not** limit work performed entirely inside OpenCode or its worker
models. OpenCode uses its configured provider allowance. If Codex launches
OpenCode, only the Codex controller is governed here: dispatch once, wait once,
consume a concise result, and validate once. Do not have Codex micromanage an
OpenCode worker.

This process does not authorize Codex to change the user's model, reasoning
level, global configuration, plugins, permissions, or safety settings. Use the
settings selected by the user unless the user explicitly requests a change.

## Objective

Complete one well-defined outcome with the fewest useful Codex turns:

1. establish the deliverable and acceptance criteria;
2. perform one targeted reconnaissance pass;
3. make one coherent change batch;
4. run one proportionate verification pass;
5. stop when the acceptance criteria pass.

Correctness and evidence come first. Lower turn count is useful only when the
result remains complete and verified.

## Start-of-task contract

Before broad inspection, identify:

- the requested outcome;
- the files or subsystem likely in scope;
- observable acceptance criteria;
- required checks;
- actions that need separate authorization;
- whether the task is an answer, diagnosis, plan, or implementation.

Make reasonable, low-risk assumptions when the repository can answer an
ambiguity. Ask the user only when an unresolved choice would materially change
the result or expand scope.

Do not turn a focused request into a whole-codebase audit. Do not implement a
fix when the user asked only for diagnosis, review, or explanation.

## Execution loop

### 1. Reconnaissance

- Read the repository instructions and the smallest relevant set of files.
- Use `rg` or `rg --files` to locate evidence before opening large files.
- Batch related read-only checks in one tool call where their outputs can be
  interpreted together.
- Limit command output with targeted paths, filters, or output budgets.
- Do not dump whole logs, generated artifacts, lock files, databases, or large
  directory trees into the conversation.
- Do not repeat a check unless repository state changed or the first result was
  incomplete.

At the end of reconnaissance, state a short internal working hypothesis or
plan and proceed. Do not repeatedly rewrite the plan.

### 2. Implementation

- Work in one logical batch that can be reviewed and verified together.
- Prefer one focused patch over many small edit-and-reinspect cycles.
- Preserve unrelated user changes and follow `docs/commit-conventions.md`.
- Avoid speculative cleanup, adjacent refactors, and optional enhancements.
- If new evidence invalidates the approach, revise it once before continuing.
  Do not alternate repeatedly between competing approaches.

### 3. Verification

- Run the narrowest check that proves the changed behaviour first.
- Run broader tests only when the change's risk or repository instructions
  require them.
- Do not rerun an unchanged successful check for reassurance.
- Capture concise results: command, pass/fail, and the material failure detail.
- Perform the required live smoke run when this repository's workflow calls
  for one.

### 4. Completion

Stop when the acceptance criteria pass. The final response should contain:

- the outcome;
- changed files or produced artifacts;
- verification performed;
- the commit hash when a commit was required;
- any remaining risk, blocker, or deliberately unperformed work.

Do not add optional research, another review pass, or further improvements
after completion unless required by repository policy or requested by the user.

## Turn and tool-call budgets

These are soft checkpoints, not permission to skip necessary work:

| Task shape | Target before reassessment | Native Codex subagents |
|---|---:|---:|
| Explanation or documentation-only edit | 8 tool calls | 0 |
| Focused bug fix or feature | 20 tool calls | 0 by default |
| Larger refactor or multi-file feature | 35 tool calls per verified batch | At most 2 when work is independent |

Reassess immediately when any of these occurs:

- the task reaches its target tool-call count without a clear path to finish;
- the conversation is compacted for the first time;
- the same command, test, or hypothesis has been attempted twice;
- tool output is growing faster than implementation progress;
- a delegated worker needs repeated clarification or polling.

At reassessment, choose one action:

1. finish the current batch if only final verification remains;
2. narrow the scope to the acceptance criteria;
3. create a concise checkpoint or handoff and continue in a fresh task;
4. report a genuine blocker and request the missing decision.

Do not continue an open-ended exploration merely because context remains
available.

## Native Codex subagents

Native Codex subagents consume Codex allowance. Use them only when:

- the user explicitly requests them;
- repository instructions require one, such as the post-commit adversarial
  review for bug fixes; or
- at least two substantial subtasks are independent and parallel execution is
  expected to save more controller turns than coordination will add.

When using a native Codex subagent:

- give it one bounded deliverable and exact evidence requirements;
- pass only the context it needs;
- prohibit unrelated exploration and mutation when the task is read-only;
- request a concise final result with file and line references;
- wait for completion instead of repeatedly polling;
- do not ask several agents the same question unless comparison is the goal;
- synthesize results once.

Do not delegate sequential microtasks, command execution, routine file reads,
or work the primary task can complete in one or two turns.

## OpenCode boundary

OpenCode worker usage is outside this Codex allowance process. When Codex is
used to start OpenCode, follow a single-dispatch pattern:

1. give OpenCode the complete goal, scope, constraints, acceptance criteria,
   and required tests;
2. let OpenCode inspect, implement, and iterate independently;
3. ask it to write changes to the workspace and return only changed files,
   decisions, test results, and remaining risks;
4. wait for completion without intermediate review or short polling;
5. have Codex inspect the final diff once and run one relevant verification
   pass;
6. follow up only when verification fails or a material decision is missing.

Do not paste full OpenCode reasoning, transcripts, or command logs into Codex.
Although OpenCode generated them, Codex must spend input context to read and
carry them through later turns.

## Tool and approval discipline

- Batch related safe checks without hiding failures or using unsafe shell
  composition.
- Prefer workspace-local, non-escalated operations.
- Request escalation only when it is necessary to complete the user's task.
- Use one bounded wait for a running job; do not poll unchanged state.
- Reduce large tool results before returning them to the model when a
  deterministic filter, count, or summary preserves the required evidence.
- Keep user-visible progress updates concise and avoid repeating information
  that will appear in the final response.

## Copy-paste invocation

Use this when starting another Codex task:

```text
Follow docs/codex-usage-efficient-workflow.md for this task. Treat it as an
execution constraint and do not change my Codex model or global settings.

Goal: <one concrete outcome>
Scope: <files, subsystem, or boundaries>
Acceptance criteria: <observable checks>

Work in one bounded batch. Batch related reconnaissance, avoid repeated
polling and broad exploration, use native Codex subagents only when explicitly
required by the document or repository policy, run one proportionate
verification pass, and stop when the acceptance criteria pass.
```

For Codex orchestrating OpenCode, add:

```text
OpenCode worker usage is out of scope for the Codex allowance. Dispatch one
self-contained assignment, let OpenCode complete it independently, request a
concise final summary, and have Codex perform only one final diff review and
verification pass.
```

## Final checklist

Before finishing, confirm:

- [ ] the requested outcome and acceptance criteria are satisfied;
- [ ] inspection stayed within the relevant scope;
- [ ] repeated tool calls, polls, and validation runs were avoided;
- [ ] native Codex subagents were omitted or justified;
- [ ] required tests and repository-specific checks passed;
- [ ] only task-owned paths were staged and committed;
- [ ] the final response is concise and evidence-backed;
- [ ] no model, global setting, plugin, permission, or safety configuration was
      changed without explicit user authorization.

## Rationale and source

OpenAI's model guidance recommends lean prompts, exposing only relevant tools,
tracking context growth, and setting explicit stopping and retry limits. It
also notes that long sessions amplify repeated prompt and tool content. See
the [official OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model).
