---
name: ghimplement
description: Run the end-to-end plan → implement → review → address workflow in the background by invoking ~/.claude/skills/_bg/launch.sh. Creates a GitHub issue, opens a PR implementing it, collects Copilot + Claude reviews, and addresses them. The launcher returns immediately; you'll be notified when the pipeline finishes.
argument-hint: [-r <ref>] <instructions>
allowed-tools: Bash(~/.claude/skills/_bg/launch.sh:*)
---

You are running the `ghimplement` workflow **in the background**. The skill is a thin wrapper over `~/.claude/skills/_bg/launch.sh`, which:

1. Creates an isolated git worktree of the current project (detached HEAD).
2. Spawns the real pipeline (`~/.claude/skills/ghimplement/ghimplement.sh`) detached from this session — it survives Ctrl-C, shell exit, and Claude Code quitting.
3. Records per-workflow state under `~/.claude/workflows/<workflow-id>/` (`state.json`, combined `log`, markers).
4. Returns within ~1s.

A `SessionStart` / `UserPromptSubmit` hook notifies a future Claude session for this project when the workflow finishes.

## Arguments

$ARGUMENTS

Forward them verbatim to the launcher. Quote the instructions string so shell word-splitting doesn't break it.

## What to do

Before invoking the launcher, compose a short (≤60 characters) human-readable phrase that summarizes the task — this becomes the workflow's `description` in status views (`/workflows`, session-summary hook). Examples:

- task "refactor the auth middleware to drop the session-token caching layer" → `"drop auth middleware session caching"`
- task "fix regression where empty PRs pass review" → `"fix empty-PR review regression"`

Pass it as `--description "<phrase>"` before the `ghimplement` kind argument:

```
~/.claude/skills/_bg/launch.sh --description "<phrase>" ghimplement $ARGUMENTS
```

If $ARGUMENTS is so terse that a distilled phrase wouldn't add anything, you may omit `--description` — the launcher falls back to the first 60 chars of the instructions.

Report the workflow id, workdir, and log path that it prints. Make clear to the user:

- The pipeline is running in the background — their session is free immediately.
- They do **not** need to keep this Claude Code session open.
- They will see a notification in a future session (any project-scoped session) once the pipeline finishes; the log path is where the final PR URL and per-stage output will be.

## Do not

- Do not tail the log or block waiting for the pipeline to finish.
- Do not pass extra flags the launcher doesn't accept.
- Do not invoke the pipeline script (`ghimplement.sh`) directly — always go through the launcher.
- Do not run the individual skills (`/ghplan`, `/ghreview`, `/ghaddress`) inline — the backgrounded pipeline already chains them.
