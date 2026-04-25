---
name: bossgremlin
description: Run the end-to-end chained gremlin workflow in the background by invoking ~/.claude/skills/_bg/launch.sh. Chains multiple child gremlins serially: the handoff agent decides what to implement next, each child runs plan→implement→review→address, and the boss lands each one before proceeding. The launcher returns immediately; you'll be notified when the chain finishes.
argument-hint: --plan <spec-path> --chain-kind local|gh [--model <model>]
allowed-tools: Bash(~/.claude/skills/_bg/launch.sh:*)
---

You are running the `bossgremlin` workflow **in the background**. The skill is a thin wrapper over `~/.claude/skills/_bg/launch.sh`, which:

1. Creates an isolated git worktree (detached HEAD) of the current project.
2. Spawns the real boss (`~/.claude/skills/bossgremlin/bossgremlin.py`) detached from this session — it survives Ctrl-C, shell exit, and Claude Code quitting.
3. Records per-gremlin state under `~/.local/state/claude-gremlins/<gremlin-id>/` — `state.json`, `boss_state.json`, per-handoff plan files, combined `log`.
4. Returns within ~1s.

A `SessionStart` / `UserPromptSubmit` hook notifies a future Claude session for this project when the chain finishes.

## Arguments

$ARGUMENTS

## What to do

Before invoking the launcher, compose a short (≤60 characters) human-readable phrase that summarizes the overall goal — this becomes the boss's `description` in status views. Example:

- "refactor API then migrate callers then update docs" → `"api refactor + caller migration + docs"`

Pass it as `--description "<phrase>"` before the `bossgremlin` kind argument:

```
~/.claude/skills/_bg/launch.sh --description "<phrase>" bossgremlin --plan <spec-path> --chain-kind <local|gh> [--model <model>]
```

Flags:

- `--plan <spec-path>` — (required) absolute path to the top-level spec file describing the overall multi-step goal. Must be a non-empty file. This file is immutable for the life of the chain — the boss passes it to the first handoff agent and never reads it.
- `--chain-kind local|gh` — (required) whether children are `localgremlin` (local branch, squash-merged) or `ghgremlin` (GitHub PR, squash-merged to main). A chain is homogeneous — all children are the same kind.
- `--model <model>` — model to use for handoff agent calls (default: `sonnet`).

## Where artifacts go

- `~/.local/state/claude-gremlins/<boss-id>/boss_state.json` — ordered list of child ids with outcomes, per-handoff records (timestamps, plan paths, exit states), chain base ref.
- `~/.local/state/claude-gremlins/<boss-id>/handoff-001.md`, `handoff-002.md`, … — rolling plan documents produced by each handoff invocation, each containing only the work still remaining at that point. The sequence of files is the audit trail of progression.
- `~/.local/state/claude-gremlins/<boss-id>/handoff-001-child.md`, … — child plans passed to each child gremlin.
- `~/.local/state/claude-gremlins/<boss-id>/handoff-001.state.json`, … — handoff signal files recording exit_state and reason.
- `~/.local/state/claude-gremlins/<boss-id>/log` — boss lifecycle events (handoff invoked, child started, child landed, rescue attempts). Does not contain plan/diff/review content.
- Per-child state dirs are preserved after land (as with standalone gremlins).

## Observability

The boss appears in `/gremlins` like any other gremlin (KIND=boss). Its stage column shows the current operation: `handoff`, `waiting`, `landing`, `rescuing`, or `done`. Children display their owning boss in the ID column as `[boss:<id>]`.

## Stop and resume

- `/gremlins stop <boss-id>` — sends SIGTERM to the boss; it stops the current child (or handoff) and exits. The chain is halted; children that already landed stay landed.
- `/gremlins rescue <boss-id>` — resumes a dead or stalled boss. The boss re-reads `boss_state.json` and continues from where it stopped.

## Do not

- Do not tail the log or block waiting for the chain to finish.
- Do not invoke `bossgremlin.py` directly — always go through the launcher.
- Do not use `bossgremlin` for single-step tasks — use `/localgremlin` or `/ghgremlin`.
