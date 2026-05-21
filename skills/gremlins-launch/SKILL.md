---
name: gremlins-launch
description: Launch a single background gremlin by pipeline name. Use when the user names one unit of work to run now — not when they want a queue, a chain, or scheduled work.
argument-hint: [<pipeline> <flags...>]
---

You are launching one background gremlin on behalf of the user. `gremlins launch` picks a pipeline by name, validates inputs, spawns a detached child process, and returns its id. It does not land the result and does not enqueue follow-up work.

## When to use this skill

- The user names exactly one unit of work to start now ("kick off X", "launch a gremlin for #123", "run the boss on this plan").
- They want a one-off spawn against the current branch or a specific PR.

When NOT to use:
- The user wants multiple units serialized → `/gremlins-queue`.
- They want to resume or rescue an existing gremlin → that's `gremlins resume` / `gremlins rescue`, not `launch`.
- They want to land work → that's `gremlins land`.

## Discovering what's available

Pipeline names are not hard-coded — they're discovered from the project and the installed package. Always:

```
gremlins launch --list                      # see pipelines available here
gremlins launch <pipeline> --help           # see the flags that pipeline accepts
```

Don't memorize pipeline names or guess flags. The flag set for each pipeline is built from its first stage's `orchestration_args()`, so it varies pipeline-to-pipeline.

## Two categories of flags

**Infrastructure flags** (apply to every pipeline):

- `--description TEXT` — short human label stored in state.
- `--gremlin-id ID` — explicit id, must match `[A-Za-z0-9_-]+`. Rejected if it collides with a pipeline name or a live gremlin with that id. Omit to get an auto-id like `<slug>-<hex6>`.
- `--parent PARENT_ID` — record a parent gremlin (for chains).
- `--print-id` — print the gremlin id to stdout (banner still goes to stderr).
- `--print-id-only` — print only the id to stdout; suppress the banner. Use this when scripting.
- `--wait` — block until the spawned gremlin exits and return its exit code. **No timeout** — a hung gremlin blocks forever. Use this when the caller (e.g. the queue runner) needs to serialize.
- `--base-ref REF` *or* `--pr PR` (mutex) — `--base-ref` sets the branch to fork the worktree from; `--pr` checks out the PR head in a detached worktree (PR number or URL). Default base-ref comes from the pipeline.
- `--client LABEL` — override the client/provider for this run.

**Pipeline-specific flags** — get them from `gremlins launch <pipeline> --help`. Commonly seen on issue-driven pipelines:

- `--plan PATH_OR_REF` — path to a `.md` plan file, or a GitHub issue ref (`#123` or `owner/repo#123`). When a ref, the issue title becomes the description and the body is saved as `artifacts/plan.md`.
- `--instructions TEXT` — extra instructions for the planning agent (mutually exclusive with `--plan`).
- `--repo OWNER/NAME` — repo for `gh` operations.

## Canonical examples

Issue-driven:
```
gremlins launch gh-terse --plan '#697'
gremlins launch gh-terse --plan '#697' --gremlin-id fix-cache-ttl --print-id-only
```

Extend an existing PR:
```
gremlins launch pr-extend --pr 697 --instructions 'address review comments'
```

Boss chain (boss takes instructions positionally, no `--plan`):
```
gremlins launch boss 'plan and dispatch a fix for the flaky parallel test'
```

Local plan file:
```
gremlins launch local --plan ./plans/refactor-foo.md
```

(Don't take these pipeline names as authoritative — confirm with `--list` and `--help`.)

## What happens when you run it

1. Validates plan/instructions (mutually exclusive; plan file must exist and be non-empty; issue refs are fetched).
2. Resolves base-ref (or PR head), pipeline path, and gremlin id.
3. Writes initial state under `<state_root>/<gremlin_id>/` (instructions, expanded pipeline.yaml, state.json, optional `artifacts/plan.md`).
4. Spawns `python -m gremlins.spawn.pipeline ...` as a detached child, redirecting to `<state_dir>/log`.
5. Polls the child for up to 2 seconds. **If it exited early, prints the tail of the log to stderr and returns the child's exit code.** Otherwise prints id / log / state-file to stderr.
6. With `--wait`, blocks on `proc.wait()` and returns the gremlin's exit code; without `--wait`, returns 0.

## Output channels (matters for scripting)

- **stderr**: banner (`gremlin id: …`, log path, state path), and any error output.
- **stdout**: empty by default. With `--print-id`, the id. With `--print-id-only`, just the id, no banner. Prefer `--print-id-only` when capturing the id programmatically.

## Gotchas

- `--wait` has **no timeout**. Don't use it in interactive flows where you might want to walk away.
- `--gremlin-id` colliding with a pipeline name is rejected, not silently renamed.
- Re-using an id whose previous run is still alive raises `GremlinAlreadyRunning`. Old terminal state with the same id is overwritten on disk.
- Early-exit (within the first 2s) is surfaced inline; later failures are NOT — the parent has already returned. Check `state.json` / `log` to see how a backgrounded gremlin actually ended.
- `--plan` accepts paths AND issue refs (`#N`, `owner/repo#N`). It does not accept arbitrary strings — if it's not a file and not a recognized ref, it errors.
- The default base-ref is pipeline-dependent (typically `current` / `HEAD`), not always `main`. Pass `--base-ref` explicitly if it matters.

## Don't do this skill's job for it

- Don't pick the pipeline for the user unless they've named one or the choice is obvious from context (issue ref → `gh-terse`, PR number → `pr-extend`, etc.). When unclear, ask.
- Don't add `--wait` unless asked or unless you're inside the queue pattern (`gremlins-queue` skill).
- Don't follow up with `gremlins land` automatically. Landing is a separate, deliberate step.
