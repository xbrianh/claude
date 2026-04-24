---
name: handoff
description: Reads the current plan document and the diff accumulated on the branch, decides whether the chain is complete or a next step is needed, and writes an updated plan plus (on next-plan) a child plan suitable for launch.sh --plan. Foreground, not backgrounded.
argument-hint: --plan <path> [--out <path>] [--base <ref>] [--model <model>] [--timeout <secs>]
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

- **`next-plan`**: work remains; a child plan has been written for the next gremlin.
- **`chain-done`**: all tasks are implemented; the chain is complete.
- **`bail`**: something prevents safe continuation; reason is in the signal file and updated plan.

Script exit code 0 on any recognized outcome; 1 on infrastructure failure (read the signal file to distinguish outcomes).

## Arguments

$ARGUMENTS

Forward them verbatim to the script:

```
~/.claude/skills/handoff/handoff.py $ARGUMENTS
```

Flags:

- `--plan <path>` — (required) path to the current plan document.
- `--out <path>` — path for the updated plan output. Auto-named from `--plan` if omitted (e.g. `plan.md` → `plan-001.md`, `plan-001.md` → `plan-002.md`).
- `--base <ref>` — git ref to use as the chain-start point for diff/log collection. Defaults to `main`.
- `--model <model>` — model for the inner agent. Defaults to `sonnet`.
- `--timeout <secs>` — timeout in seconds for the inner agent. No timeout by default.

After the script exits, report the exit state, updated plan path, and child plan path (if applicable) to the user.
