---
name: gremlins
description: On-demand status of background gremlins launched by /localgremlin and /ghgremlin. Reads every ~/.local/state/claude-gremlins/<id>/state.json on the machine and prints one line per active gremlin with its kind, current stage, liveness (running / stalled / dead), description, and age. Use to check progress, spot crashed gremlins, close finished ones, stop a running gremlin, rescue a dead/stalled one, or land a finished gremlin onto its target branch. Not a project filter by default — set --here to restrict to the current repo.
argument-hint: [stop|rescue|rm|close|land <id>] [--here] [--running] [--dead] [--stalled] [--kind local|gh] [--since <dur>] [--recent [N]] [--watch [sec]] [<id-prefix>]
allowed-tools: Bash(~/.claude/skills/gremlins/gremlins.py:*)
---

You are running the `gremlins` status command. It reads the persistent state under `~/.local/state/claude-gremlins/` (or `$XDG_STATE_HOME/claude-gremlins/` if `XDG_STATE_HOME` is set) and summarizes every active (not-yet-closed) background gremlin across every project on this machine.

## What to do

Run the script and print its output verbatim to the user — do not paraphrase or summarize:

```
~/.claude/skills/gremlins/gremlins.py $ARGUMENTS
```

The script produces a small table. Each row is one gremlin:

- `KIND` — `local` (from `/localgremlin`) or `gh` (from `/ghgremlin`).
- `ID` — short (6-hex) gremlin identifier. Use it with `close <id>` to mark a dead gremlin as closed (which hides it from future runs of this command).
- `STAGE` — the gremlin's current stage name (e.g. `plan`, `implement`, `review-code`). For parallel reviewers, a parenthesized sub-stage shows each reviewer's state (e.g. `review-code (opus=done,sonnet=running)`).
- `LIVENESS` — `running`, `stalled:<reason>`, or `dead:<reason>`.
- `AGE` — time since launch.
- `DESCRIPTION` — the short human phrase captured at launch.

## Flags

- (default, no flag): list all active gremlins on this machine.
- `--here`: restrict to gremlins whose project_root matches the current working directory's git toplevel.
- `--running`: show only gremlins whose liveness is `running`.
- `--dead`: show only gremlins whose liveness starts with `dead:`.
- `--stalled`: show only gremlins whose liveness starts with `stalled:`.
- `--kind local|gh`: filter to a specific gremlin kind (`local` from `/localgremlin`, `gh` from `/ghgremlin`). Composable with all other list flags.
- `--since <duration>`: show only gremlins started within the given duration of now. Duration format: integer followed by `s`, `m`, `h`, or `d` (e.g. `30m`, `2h`, `1d`). Composable with all other list flags.
- `--recent [N]`: show recently-finished (`dead:*`) gremlins started within N hours (default 24), including closed ones (marked with `[closed]`). Mutually exclusive with `--running`/`--stalled`. Composes with `--here`, `--dead`, and `--kind`.
- `--watch [sec]`: refresh the view every `sec` seconds (default 2). Press Ctrl-C to stop cleanly. Mutually exclusive with the `<id-prefix>` positional argument. Composable with all listing flags and `--recent`.
- `<id-prefix>`: substring to drill into a single gremlin — prints every field from `state.json` plus computed liveness, age, and local start time. Mutually exclusive with `--watch`.
- `stop <id>`: send SIGTERM to a running (or stalled) gremlin's process group and wait up to 6 seconds for it to exit cleanly. If the process doesn't exit in time, marks it stopped manually. Use when you want to cancel an in-progress gremlin.
- `rescue <id>`: diagnose and resume a dead or stalled gremlin. Reads the failure log and existing artifacts, then spawns a `claude -p` agent **inline** (synchronous, output visible in terminal) in the gremlin's worktree. The agent diagnoses the failure, fixes the underlying issue, and completes the remaining stages. Runs synchronously — Ctrl-C to abort. After a successful rescue, run `/gremlins land <id>` to merge the branch if satisfied. Note: if the gremlin is still running, `rescue` will refuse and suggest using `stop` first.
- `rm <id>`: delete a dead or finished gremlin's state directory from disk (the log, plan, reviews, and `state.json`). Refuses with an error if the gremlin is still running or stalled — use `stop` first, then `rm`. This is permanent; use when you want to fully clean up a gremlin rather than just hide it with `close`.
- `close <id>`: mark a dead or finished gremlin as closed (hides it from the default list view). Artifacts, logs, state directory, and branch remain on disk until `rm` is run. If the gremlin is still running or stalled, `close` will refuse and suggest using `stop` first.
- `land <id>`: land a finished gremlin onto its target branch, then clean up. Behavior depends on gremlin kind:
  - **local** (`/localgremlin`): validates that the gremlin branch is finished and the working tree is clean, squash-merges the branch onto the current branch using a commit message distilled from `plan.md`'s `## Context` section, then deletes the branch and state directory.
  - **gh** (`/ghgremlin`): reads `pr_url` from state, checks for merge blockers (changes requested, failed CI), merges the PR with `--squash --delete-branch`, fast-forwards local `main`, then removes the worktree and state directory.
  - Refuses if the gremlin is still running or stalled — use `stop` first.
  - Refuses for `CHANGES_REQUESTED` reviews or failed CI checks; prints the PR URL so you can act.
  - `--force`: for a closed (not merged) gh PR, skips the merge and goes straight to cleanup.

## Do not

- Do not re-render or summarize the output — print the script's stdout verbatim inside a code block.
- Do not chain this with other commands to "help" the user parse it; the tabular format is already scannable.
