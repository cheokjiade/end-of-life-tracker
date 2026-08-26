# Commit conventions

This is the canonical Git commit policy for humans and AI coding agents working
in this repository. `AGENTS.md` makes it a standing instruction for Claude Code,
Codex, OpenCode, and other harnesses.

## When to commit

Commit after every completed, verified batch of repository changes. A batch is
one logical user-requested change, or a tightly related set of changes that is
implemented, documented, and tested together.

Examples of one batch:

- add a provider, its tests, and its documentation;
- fix one report-rendering bug and add its regression test;
- refactor one module without changing behaviour;
- update agent instructions and their integrity checks.

Do not wait and combine unrelated tasks into a large end-of-session commit. If
a batch contains independently useful changes, split it into multiple atomic
commits in dependency order. Each commit should build on the previous one and
be understandable on its own.

Do not create a normal completion commit when relevant checks fail or the work
is known to be incomplete. If the user explicitly asks for a checkpoint, state
the incomplete or failing condition in the commit body.

## Safe agent workflow

The working tree may contain user work or edits from another agent. Preserve
them.

1. Before editing, run `git status --short` and note existing changes.
2. Make one logical batch and run its relevant checks.
3. Inspect `git diff` and identify only the paths or hunks belonging to the
   batch.
4. Stage explicit paths or hunks. Do not use `git add -A` in a dirty tree.
5. Inspect `git diff --cached` and confirm it contains the complete batch and
   nothing else.
6. Commit using the format below.
7. Run `git status --short` again and report the commit hash plus any changes
   deliberately left uncommitted.

If a file contains both pre-existing edits and batch edits, use safe hunk
staging only when the changes can be separated confidently. Otherwise leave
the file uncommitted and tell the user why. Never discard, overwrite, stash, or
commit unrelated work merely to obtain a clean tree.

Ignored per-project `eol_config.*.json` files and generated reports are runtime
artifacts, not repository changes. Do not force-add them. Never commit secrets
or local agent settings.

## Commit message format

Use Conventional Commits:

```text
<type>(<scope>)!: <summary>

<optional body>

<optional footer(s)>
```

The scope and `!` are optional. Use `!` only for a breaking change and explain
the impact in the body or a `BREAKING CHANGE:` footer.

Allowed types:

| Type | Use for |
|---|---|
| `feat` | New user-visible behaviour or capability |
| `fix` | Bug fixes |
| `refactor` | Behaviour-preserving code restructuring |
| `perf` | Performance improvements |
| `test` | Tests only |
| `docs` | Documentation or agent instructions only |
| `build` | Packaging, dependencies, or build tooling |
| `ci` | Continuous-integration configuration |
| `chore` | Maintenance that fits no type above |
| `revert` | Reverting an earlier commit |

Choose a short, stable scope when it adds useful context, such as `provider`,
`report`, `config`, `terraform`, or `agents`. Omit the scope when it would be
vague or redundant.

Subject rules:

- use an imperative, lower-case summary: `add`, `fix`, `preserve`, not `added`
  or `fixes`;
- do not end with a period;
- keep the full subject at 72 characters or fewer where practical;
- describe the outcome, not the editing activity.

Use the optional body to explain why the change was needed, notable trade-offs,
or behaviour that is not obvious from the diff. Reference issues in footers,
for example `Refs: #123` or `Closes: #123`.

Do not add platform-specific generated-by text or AI co-author trailers unless
the user explicitly requests attribution. The same history format should result
regardless of which agent made the change.

## Examples

```text
feat(provider): add lifecycle source for example product
```

```text
fix(report): escape policy notes in HTML output

Prevent policy text containing markup from changing the report structure.
```

```text
docs(agents): standardize batch commits across coding agents
```

```text
feat(config)!: require explicit notification channels

BREAKING CHANGE: configs without a notifications list are no longer accepted.
```

## Actions that require separate authorization

A request to change code authorizes the local completion commits described
above. It does not authorize pushing, force-pushing, rebasing, amending a
published commit, rewriting history, deleting branches, or opening a pull
request. Do those only when the user explicitly asks.
