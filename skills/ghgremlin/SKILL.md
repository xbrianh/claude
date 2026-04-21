---
name: ghgremlin
description: Run the end-to-end plan → implement → review → address workflow in the background by invoking ~/.claude/skills/_bg/launch.sh. Creates a GitHub issue, opens a PR implementing it, collects Copilot + Claude reviews, and addresses them. The launcher returns immediately; you'll be notified when the gremlin finishes.
argument-hint: [--design] [-r <ref>] <instructions>
allowed-tools: Bash(~/.claude/skills/_bg/launch.sh:*)
---

You are running the `ghgremlin` workflow **in the background**. The skill is a thin wrapper over `~/.claude/skills/_bg/launch.sh`, which:

1. Creates an isolated git worktree of the current project (detached HEAD).
2. Spawns the real gremlin (`~/.claude/skills/ghgremlin/ghgremlin.sh`) detached from this session — it survives Ctrl-C, shell exit, and Claude Code quitting.
3. Records per-gremlin state under `~/.local/state/claude-gremlins/<gremlin-id>/` (or `$XDG_STATE_HOME/claude-gremlins/<gremlin-id>/` if `XDG_STATE_HOME` is set) — `state.json`, combined `log`, markers.
4. Returns within ~1s.

A `SessionStart` / `UserPromptSubmit` hook notifies a future Claude session for this project when the gremlin finishes.

## Where artifacts go

Gremlin artifacts live outside the product branch. Point the user at:

- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/spec.md` — the finalized design spec (only if launched with `--design`).
- `~/.local/state/claude-gremlins/<gremlin-id>/log` — combined stdout/stderr of the gremlin.
- `~/.local/state/claude-gremlins/<gremlin-id>/state.json` — gremlin status, exit code, workdir path.

## Arguments

$ARGUMENTS

Forward them verbatim to the launcher. Quote the instructions string so shell word-splitting doesn't break it.

## What to do

If `$ARGUMENTS` begins with `--design` (it must be the first token), strip that flag and invoke the design skill instead of the launcher:

```
Skill(skill="design", args="--target ghgremlin <remaining-args>")
```

Do not proceed to the launcher invocation. The design skill will run an interactive design conversation and, when the user is ready, will invoke the launcher automatically.

---

Before invoking the launcher, compose a short (≤60 characters) human-readable phrase that summarizes the task — this becomes the gremlin's `description` in status views (`/gremlins`, session-summary hook). Examples:

- task "refactor the auth middleware to drop the session-token caching layer" → `"drop auth middleware session caching"`
- task "fix regression where empty PRs pass review" → `"fix empty-PR review regression"`

Pass it as `--description "<phrase>"` before the `ghgremlin` kind argument:

```
~/.claude/skills/_bg/launch.sh --description "<phrase>" ghgremlin $ARGUMENTS
```

If $ARGUMENTS is so terse that a distilled phrase wouldn't add anything, you may omit `--description` — the launcher falls back to the first 60 chars of the instructions.

Report the gremlin id, workdir, and log path that it prints. Make clear to the user:

- The gremlin is running in the background — their session is free immediately.
- They do **not** need to keep this Claude Code session open.
- They will see a notification in a future session (any project-scoped session) once the gremlin finishes; the log path is where the final PR URL and per-stage output will be.

## Do not

- Do not tail the log or block waiting for the gremlin to finish.
- Do not pass extra flags the launcher doesn't accept.
- Do not invoke the gremlin script (`ghgremlin.sh`) directly — always go through the launcher.
- Do not run the individual skills (`/ghplan`, `/ghreview`, `/ghaddress`) inline — the backgrounded gremlin already chains them.
