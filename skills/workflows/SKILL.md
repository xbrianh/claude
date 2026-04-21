---
name: workflows
description: On-demand status of background pipelines launched by /localimplement and /ghimplement. Reads every ~/.local/state/claude-workflows/<id>/state.json on the machine and prints one line per active workflow with its kind, current stage, liveness (running / stalled / dead), description, and age. Use to check progress, spot crashed pipelines, or acknowledge (hide) finished ones. Not a project filter by default — set --here to restrict to the current repo.
argument-hint: [--here] [--ack <id>] [--ack-all] [--running] [--dead] [--stalled] [--kind local|gh] [--since <dur>] [--recent [N]] [--watch [sec]] [<id-prefix>]
allowed-tools: Bash(~/.claude/skills/workflows/workflows.py:*)
---

You are running the `workflows` status command. It reads the persistent state under `~/.local/state/claude-workflows/` (or `$XDG_STATE_HOME/claude-workflows/` if `XDG_STATE_HOME` is set) and summarizes every active (not-yet-acknowledged) background workflow across every project on this machine.

## What to do

Run the script and print its output verbatim to the user — do not paraphrase or summarize:

```
~/.claude/skills/workflows/workflows.py $ARGUMENTS
```

The script produces a small table. Each row is one workflow:

- `KIND` — `local` (from `/localimplement`) or `gh` (from `/ghimplement`).
- `ID` — short (6-hex) workflow identifier. Use it with `--ack <id>` to mark a dead workflow acknowledged (which hides it from future runs of this command).
- `STAGE` — the pipeline's current stage name (e.g. `plan`, `implement`, `review-code`). For parallel reviewers, a parenthesized sub-stage shows each reviewer's state (e.g. `review-code (opus=done,sonnet=running)`).
- `LIVENESS` — `running`, `stalled:<reason>`, or `dead:<reason>`.
- `AGE` — time since launch.
- `DESCRIPTION` — the short human phrase captured at launch.

## Flags

- (default, no flag): list all active workflows on this machine.
- `--here`: restrict to workflows whose project_root matches the current working directory's git toplevel.
- `--ack <id>`: mark a finished/dead workflow as acknowledged (hides it from subsequent lists). Accepts full id or a substring.
- `--ack-all`: acknowledge every dead/finished workflow.
- `--running`: show only workflows whose liveness is `running`.
- `--dead`: show only workflows whose liveness starts with `dead:`.
- `--stalled`: show only workflows whose liveness starts with `stalled:`.
- `--kind local|gh`: filter to a specific workflow kind (`local` from `/localimplement`, `gh` from `/ghimplement`). Composable with all other list flags.
- `--since <duration>`: show only workflows started within the given duration of now. Duration format: integer followed by `s`, `m`, `h`, or `d` (e.g. `30m`, `2h`, `1d`). Composable with all other list flags.
- `--recent [N]`: show recently-finished (`dead:*`) workflows started within N hours (default 24), including acknowledged ones. Mutually exclusive with `--running`/`--dead`/`--stalled`. Composes with `--here` and `--kind`.
- `--watch [sec]`: refresh the view every `sec` seconds (default 2). Press Ctrl-C to stop cleanly. Mutually exclusive with the `<id-prefix>` positional argument. Composable with all listing flags and `--recent`.
- `<id-prefix>`: substring to drill into a single workflow — prints every field from `state.json` plus computed liveness, age, and local start time. Mutually exclusive with `--watch`.

## Do not

- Do not re-render or summarize the output — print the script's stdout verbatim inside a code block.
- Do not chain this with other commands to "help" the user parse it; the tabular format is already scannable.
