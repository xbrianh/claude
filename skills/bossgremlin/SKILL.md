---
name: bossgremlin
description: Run the end-to-end chained gremlin workflow in the background by invoking `python -m gremlins.cli launch`. Chains multiple child gremlins serially: the handoff agent decides what to implement next, each child runs plan→implement→review→address, and the boss lands each one before proceeding. The launcher returns immediately; you'll be notified when the chain finishes.
argument-hint: --plan <spec-path|issue-ref> --chain-kind local|gh [--model <model>]
allowed-tools: Bash(python -m gremlins.cli launch:*)
---

You are running the `bossgremlin` workflow **in the background**. The skill is a thin wrapper over `python -m gremlins.cli launch`, which:

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
python -m gremlins.cli launch --description "<phrase>" bossgremlin --plan <spec-path|issue-ref> --chain-kind <local|gh> [--model <model>]
```

Flags:

- `--plan <spec-path|issue-ref>` — (required) the top-level spec describing the overall multi-step goal. Four forms, mirroring `/ghgremlin --plan`:
    - **Local file path** — an absolute or relative path to a non-empty spec file.
    - **`42` or `#42`** — issue #42 in the current repo.
    - **`owner/repo#42`** — issue #42 in a different repo (cross-repo spec source; the chain still runs against the current repo's `main`).
    - **Full URL `https://github.com/owner/repo/issues/42`** — same cross-repo semantics. `github.com` only.

  At chain start, the boss snapshots the spec into `<state-dir>/spec.md` and reads only the snapshot for the rest of the chain. The original input is never re-read, so a deleted file or mutated GitHub issue cannot perturb a running chain. On rescue, the snapshot is authoritative — no re-fetch.
- `--chain-kind local|gh` — (required) whether children are `localgremlin` (local branch, squash-merged) or `ghgremlin` (GitHub PR, squash-merged to main). A chain is homogeneous — all children are the same kind.
- `--model <model>` — model to use for handoff agent calls (default: `sonnet`).

## Where artifacts go

- `~/.local/state/claude-gremlins/<boss-id>/spec.md` — chain-start snapshot of the spec (file copy or fetched issue body). The handoff agent reads this; the original `--plan` input is never re-read.
- `~/.local/state/claude-gremlins/<boss-id>/boss_state.json` — ordered list of child ids with outcomes, per-handoff records (timestamps, plan paths, exit states), chain base ref, plus `issue_url` / `issue_num` when `--plan` was an issue reference.
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

## When a child bails

If a child fails and headless rescue declines to proceed, the boss halts the chain and records the child's `bail_reason` in `boss_state.json["children"][...]`. Two reasons in particular signal different operator actions:

- **`structural`** — the rescue agent recognized a real bug in the gremlins source (under `~/.claude/gremlins/` or `~/.claude/skills/`) or in a sibling artifact (e.g. a malformed child plan under the boss's own state dir) that *can* be salvaged, but only by a human editing the gremlins source / plan. The boss log surfaces this with a `STRUCTURAL` line plus the agent's diagnosis. Look at the named file, fix it in the source repo for gremlins changes (run any project-specific mirror step needed to propagate the fix into `~/.claude/`), then `/gremlins rescue <boss-id>` to resume — the boss restarts at the next handoff (the failed child is recorded as bailed and `current_child_id` is cleared, so the resume runs a fresh handoff rather than retrying the failed phase; in practice the handoff usually re-plans into a new child for the same work because the diff has not moved).
- **`unsalvageable`** — the run is dead and giving up: corrupted state, missing worktree, conflicting git state. The boss log says `UNSALVAGEABLE`. The chain is finished; no resume will recover it.

Both halt the chain, but `structural` is a "fix and resume" signal and `unsalvageable` is a "write it off" signal.

## Do not

- Do not tail the log or block waiting for the chain to finish.
- Do not invoke `bossgremlin.py` directly — always go through the launcher.
- Do not use `bossgremlin` for single-step tasks — use `/localgremlin` or `/ghgremlin`.
