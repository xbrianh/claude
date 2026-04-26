---
name: gremlins
description: On-demand status of background gremlins launched by /localgremlin and /ghgremlin. Reads every ~/.local/state/claude-gremlins/<id>/state.json on the machine and prints one line per active gremlin with its kind, current stage, liveness (running / stalled / dead), description, and age. Use to check progress, spot crashed gremlins, close finished ones, stop a running gremlin, rescue a dead/stalled one, or land a finished gremlin onto its target branch. Not a project filter by default — set --here to restrict to the current repo.
argument-hint: [stop|rescue [--headless]|rm|close|land [--squash|--ff] <id>] [--here] [--running] [--dead] [--stalled] [--kind local|gh|boss] [--since <dur>] [--recent [N]] [--watch [sec]] [<id-prefix>]
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
- `ID` — short (6-hex) gremlin identifier. Use it with `close <id>` to mark a dead gremlin as closed (which hides it from future runs of this command). When a gremlin has a `parent_id` (i.e. it was launched by a bossgremlin), its ID column is suffixed with ` [boss:<id>]` where `<id>` is the short (6-hex) form of the owning gremlin's id.
- `STAGE` — the gremlin's current stage name (e.g. `plan`, `implement`, `review-code`). For parallel reviewers, a parenthesized sub-stage shows each reviewer's state (e.g. `review-code (opus=done,sonnet=running)`).
- `LIVENESS` — `running`, `stalled:<reason>`, `dead:<reason>`, or `dead:bailed:<bail-reason>` (set when `/gremlins rescue --headless` declined to proceed; see `rescue` below). An upstream stage writing `bail_class` (via `set-bail.sh`) halts the pipeline but surfaces here as `dead:exit <N>` — drill in with `/gremlins <id>` to see the `bail:` block explaining which class fired and why.
- `AGE` — time since launch.
- `DESCRIPTION` — the short human phrase captured at launch.

## Flags

- (default, no flag): list every unclosed gremlin on this machine — running, stalled, and dead-but-not-closed — with running ones first.
- `--here`: restrict to gremlins whose project_root matches the current working directory's git toplevel.
- `--running`: show only gremlins whose liveness is `running` (the pre-change default view, for when you only want live processes).
- `--dead`: show only gremlins whose liveness starts with `dead:`.
- `--stalled`: show only gremlins whose liveness starts with `stalled:`.
- `--kind local|gh`: filter to a specific gremlin kind (`local` from `/localgremlin`, `gh` from `/ghgremlin`). Composable with all other list flags.
- `--since <duration>`: show only gremlins started within the given duration of now. Duration format: integer followed by `s`, `m`, `h`, or `d` (e.g. `30m`, `2h`, `1d`). Composable with all other list flags.
- `--recent [N]`: show recently-finished (`dead:*`) gremlins started within N hours (default 24), including closed ones (marked with `[closed]`). Mutually exclusive with `--running`/`--stalled`. Composes with `--here`, `--dead`, and `--kind`.
- `--watch [sec]`: refresh the view every `sec` seconds (default 2). Press Ctrl-C to stop cleanly. Mutually exclusive with the `<id-prefix>` positional argument. Composable with all listing flags and `--recent`.
- `<id-prefix>`: substring to drill into a single gremlin — prints every field from `state.json` plus computed liveness, age, and local start time. Mutually exclusive with `--watch`.
- `stop <id>`: send SIGTERM to a running (or stalled) gremlin's process group and wait up to 6 seconds for it to exit cleanly. If the process doesn't exit in time, marks it stopped manually. Use when you want to cancel an in-progress gremlin.
- `rescue <id>` (interactive, default): two-phase diagnose-then-resume for a dead or stalled gremlin. **Phase A (foreground):** reads the failure log and existing artifacts from the gremlin, then spawns a `claude -p` agent in a separate scratch working directory to diagnose and fix the underlying issue. Do not assume rescue-created or rescue-modified files will appear only in the gremlin's worktree; Phase A may write in that scratch directory as part of the rescue process. The agent's prompt requires it to write a marker file at `<state-dir>/artifacts/rescue-<ts>.done` containing `{"status": "fixed" | "transient" | "structural" | "unsalvageable", "summary": "..."}` before exiting — this is the agent → wrapper handoff signal, used by both interactive and headless rescue. The four verdicts are deliberately separate; the wrapper acts on each:
  - `fixed` → the agent edited `state.json` and/or files inside the gremlin's worktree to address the root cause; proceed to Phase B. Pipeline source under `~/.claude/skills/` is read-only for rescues (it lives outside the gremlin's worktree, so edits there can't land in the PR diff, and may also be overwritten by future syncs from its upstream source repo); fixes that belong there must come back as `structural` instead.
  - `transient` → no edits needed; the failure was a flake (network, tool timeout, retriable infra) or a fix has already landed elsewhere (in `main`, in a `~/.claude/skills/` file outside the gremlin's worktree) that the chain's pre-fix base ref doesn't see. Proceed to Phase B as a relaunch-only attempt.
  - `structural` → the agent recognized a real bug in the pipeline source (`~/.claude/skills/<kind>/*.sh`, `~/.claude/skills/_bg/*.sh`, etc.) or in a sibling artifact (e.g. a malformed child plan under the parent boss's state dir) that requires a human edit elsewhere. Write `bail_reason=structural`, store the agent's diagnosis in `bail_detail`, do NOT invoke Phase B. Distinct from `unsalvageable`: an operator should look at the named pipeline file or sibling plan, not write off the run.
  - `unsalvageable` → reserved for genuinely unrecoverable states (corrupted state dir, missing worktree, conflicting git state with no clean rewind). Write `bail_reason=unsalvageable` and `bail_detail` (the agent's summary) to `state.json`, do NOT invoke Phase B. The gremlin's liveness becomes `dead:bailed:unsalvageable`. Should be rare; if the agent picks `unsalvageable` because the fix isn't in `state.json`, it almost certainly meant `structural`.
  - missing marker / unparseable marker / non-zero claude exit → write a corresponding `phase_a_no_marker` / `phase_a_bad_marker` / `phase_a_claude_error` `bail_reason`, do NOT invoke Phase B. (A clean exit without a marker is treated as a protocol violation: the agent abdicated its responsibility, so the gremlin is bailed rather than silently relaunched into the same broken state.)

  Ctrl-C to abort — state is preserved, no marker is required, no handoff happens. **Phase B (background):** on `fixed`/`transient`, invokes `~/.claude/skills/_bg/launch.sh --resume <id>` which relaunches the gremlin pipeline at the failed stage under the original gremlin id (visible again under `/gremlins`). The `LIVENESS` column is decorated with ` (rescue)` or ` (rescue x<N>)` so rescued gremlins are distinguishable at a glance. After the resumed gremlin finishes, run `/gremlins land <id>` to merge. If the gremlin is still running, `rescue` refuses and suggests `stop` first. If the gremlin has already been rescued ≥ 3 times, interactive rescue prints a warning and proceeds anyway (the cap is for unattended callers — see `--headless`).
- `rescue --headless <id>`: same two-phase flow and same marker contract as interactive, but run end-to-end with no TTY and no user input — for unattended callers (humans walking away, future bossgremlin). Differences from interactive:
  - **Excluded bail classes:** before running Phase A, reads `bail_class` from `state.json`. If it's `reviewer_requested_changes`, `security`, or `secrets`, refuses immediately, writes `bail_reason=excluded_class:<class>` and `bail_detail` to state.json, touches the `finished` marker, and exits non-zero. The gremlin's liveness becomes `dead:bailed:excluded_class:<class>`.
  - **Attempt cap:** before running Phase A, checks `rescue_count` (the same field interactive rescue increments via `launch.sh --resume`). At ≥ 3, hard-refuses with `bail_reason=attempts_exhausted` and exits non-zero.
  - **Phase A wrapper:** runs `claude -p --permission-mode bypassPermissions --output-format text` with stdin closed and a wall-clock timeout (default 1800s, override via `HEADLESS_RESCUE_TIMEOUT_SECS`). On timeout, writes `bail_reason=phase_a_timeout`. The shared rescue prompt also points the agent at the pipeline source under `~/.claude/skills/`, the product repo at `origin/HEAD`, and — for boss children — the parent boss's state dir (`boss_state.json`, rolling plan documents, sibling child plans). `summary` in the marker must be a string (no objects/arrays); the wrapper collapses whitespace and caps length so a chatty diagnosis can't injection-newline the boss log.
  - **Bail visibility:** every bail path writes `bail_reason` (machine-readable) and `bail_detail` (human-readable) to `state.json` and marks the gremlin terminal so listings make clear it isn't coming back. Use the drill-in view (`/gremlins <id>`) to inspect both fields plus any upstream `bail_class`.

  The bail-class vocabulary the upstream stages may write is documented in `~/.claude/skills/_bg/set-bail.sh`. The current set is `reviewer_requested_changes`, `security`, `secrets`, `other` — only `other` is attempted by headless rescue.
- `rm <id>`: delete a dead or finished gremlin's state directory from disk (the log, plan, reviews, and `state.json`). Refuses with an error if the gremlin is still running or stalled — use `stop` first, then `rm`. This is permanent; use when you want to fully clean up a gremlin rather than just hide it with `close`.
- `close <id>`: mark a dead or finished gremlin as closed (hides it from the default list view). Artifacts, logs, state directory, and branch remain on disk until `rm` is run. If the gremlin is still running or stalled, `close` will refuse and suggest using `stop` first.
- `land <id>`: land a finished gremlin onto its target branch, then clean up. Preserves the state directory (`artifacts/plan.md`, review artifacts under `artifacts/`, `log`, `state.json`) so you can inspect the gremlin's record after merge — use `rm <id>` for full cleanup. Behavior depends on gremlin kind:
  - **local** (`/localgremlin`): validates that the gremlin branch is finished and the working tree is clean, then (by default) squash-merges the branch onto the current branch using a commit message distilled from `plan.md`'s `## Context` section, and deletes the branch. State directory is preserved.
  - **boss** (`/bossgremlin`): fast-forwards the current branch to the boss's worktree HEAD by default, preserving each child gremlin's squash commit as a discrete step in history. Removes the boss worktree on success. State directory is preserved.
  - **gh** (`/ghgremlin`): reads `pr_url` from state, checks for merge blockers (changes requested, failed CI), merges the PR with `--squash --delete-branch`, fast-forwards local `main`, then removes the worktree. State directory is preserved.
  - Refuses if the gremlin is still running or stalled — use `stop` first.
  - Refuses for `CHANGES_REQUESTED` reviews or failed CI checks; prints the PR URL so you can act.
  - `--squash`: collapse all commits above the merge base into a single commit before landing. Default for local; for boss this condenses the whole chain into one commit. Not applicable to gh (always PR-merged).
  - `--ff`: fast-forward the current branch to the gremlin's HEAD. Default for boss; for local this preserves the implementation commits. Hard-fails if the current branch is not an ancestor of the gremlin's HEAD — re-run with `--squash` or rebase manually. Not applicable to gh.
  - `--squash` and `--ff` are mutually exclusive.
  - `--force`: for a closed (not merged) gh PR, skips the merge and goes straight to cleanup.

## Do not

- Do not re-render or summarize the output — print the script's stdout verbatim inside a code block.
- Do not chain this with other commands to "help" the user parse it; the tabular format is already scannable.
