---
name: handoff
description: Reads the current plan document and the diff accumulated on the branch, decides whether the chain is complete or a next step is needed, and writes an updated plan plus (on next-plan) a child plan suitable for launch.sh --plan. Foreground, not backgrounded.
argument-hint: --plan <path> [--spec <path>] [--out <path>] [--base <ref>] [--model <model>] [--timeout <secs>]
allowed-tools: Bash(~/.claude/skills/handoff/handoff.py:*)
---

You are running the `handoff` chain-step decision agent. It reads the current plan, compares it against the diff that has landed on the branch since the chain started, and decides what to do next.

This skill runs **in the foreground** — it blocks until the inner agent finishes. It does not spawn a background gremlin, does not create a worktree, and does not push.

## What it does

1. Reads the input plan from `--plan`.
2. Collects the git log and diff since the branch diverged from `--base` (default: `main`).
3. Runs an inner `claude -p` agent to compare landed work against the plan and determine the exit state.
4. Writes an updated plan to `--out` (auto-named if omitted).
5. If exit state is `next-plan`, also writes a child plan to `<out-stem>-child.md`.
6. Writes a machine-readable signal file to `<out-stem>.state.json`.

## Exit states

- **`next-plan`**: implementation work remains; a child plan has been written for the next gremlin.
- **`chain-done`**: all implementation tasks are landed; the chain is complete. Pending operator tasks do not block this state — they surface in the final rolling plan's `## Operator follow-ups` section.
- **`bail`**: something prevents safe continuation; reason is in the signal file and updated plan. Includes the case where remaining work is dominated by operator tasks with no coherent code-only chunk to hand a child.

Script exit code 0 on any recognized outcome; 1 on infrastructure failure (read the signal file to distinguish outcomes).

## Implementation vs operator tasks

A child gremlin runs inside a **detached-HEAD worktree, against a single feature branch, ending in one squash-merged PR**. The handoff agent classifies every still-open task in the input plan as either:

- **Implementation** — a code/doc/config change that lands inside the child's PR. These flow into the child plan.
- **Operator** — work that requires escaping the worktree scope. These are filtered out of the child plan and land in the rolling plan's `## Operator follow-ups` section for the human to pick up between phase landings.

Operator signals the agent looks for:

- Mutates `~/.claude/` or other live user state (`scripts/sync.sh push`/`pull`, hand-edits under `~/.claude/`).
- Launches another gremlin (`/localgremlin`, `/ghgremlin`, `/bossgremlin`, end-to-end smoke runs).
- Pushes outside the PR flow (`git push origin main`, force-pushes, manual merges).
- `/gremlins` operator commands (`land`, `rescue`, `stop`, `close`, `rm`).
- Post-merge verification (PR CI green checks, deploy confirmation, dashboard watching).

Edits to tracked repo files like `CLAUDE.md`, `scripts/sync.sh`, or `pipeline/DESIGN.md` are **implementation**, even if those files are mirrored to `~/.claude/` by `sync.sh` — the child edits the in-repo file, the human runs the sync later.

Spec authors should label genuinely operator-level acceptance criteria explicitly (e.g. "Operator verification:" prefix, or a separate `## Operator acceptance` section) so the agent classifies correctly. The filter is robust to inline mixing, but explicit labels reduce ambiguity.

## Arguments

$ARGUMENTS

Forward them verbatim to the script:

```
~/.claude/skills/handoff/handoff.py $ARGUMENTS
```

Flags:

- `--plan <path>` — (required) path to the current plan document.
- `--spec <path>` — overarching chain spec, surfaced to the agent as a read-only "north star" so subsequent handoffs see the original goal alongside the rolling remaining-work plan. Optional (the standalone `/handoff` use case has no separate spec).
- `--out <path>` — path for the updated plan output. Auto-named from `--plan` if omitted (e.g. `plan.md` → `plan-001.md`, `plan-001.md` → `plan-002.md`).
- `--base <ref>` — git ref to use as the chain-start point for diff/log collection. Defaults to `main`.
- `--model <model>` — model for the inner agent. Defaults to `sonnet`.
- `--timeout <secs>` — timeout in seconds for the inner agent. No timeout by default.

After the script exits, report the exit state, updated plan path, and child plan path (if applicable) to the user.
