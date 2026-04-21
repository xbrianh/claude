---
name: localgremlin
description: Run the end-to-end plan → implement → review-code → address-code workflow in the background by invoking ~/.claude/skills/_bg/launch.sh. Plan and code reviews land in `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/` alongside the run log (kept off the product branch); the code review uses two different models in parallel. The launcher returns immediately; you'll be notified when the gremlin finishes.
argument-hint: [--design] [-a <model>] [-b <model>] [-c <model>] <instructions>
allowed-tools: Bash(~/.claude/skills/_bg/launch.sh:*)
---

You are running the `localgremlin` workflow **in the background**. The skill is a thin wrapper over `~/.claude/skills/_bg/launch.sh`, which:

1. Creates an isolated git worktree of the current project on a fresh branch named `bg/localgremlin/<gremlin-id>` (or `cp -a` copies the tree for non-git projects).
2. Spawns the real gremlin (`~/.claude/skills/localgremlin/localgremlin.py`) detached from this session — it survives Ctrl-C, shell exit, and Claude Code quitting.
3. Records per-gremlin state under `~/.local/state/claude-gremlins/<gremlin-id>/` (or `$XDG_STATE_HOME/claude-gremlins/<gremlin-id>/` if `XDG_STATE_HOME` is set) — `state.json`, combined `log`, markers.
4. Returns within ~1s.

A `SessionStart` / `UserPromptSubmit` hook notifies a future Claude session for this project when the gremlin finishes.

## Where artifacts go

Plan and code-review artifacts live outside the product branch — they are scaffolding, not product. Point the user at:

- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/spec.md` — the finalized design spec (only if launched with `--design`).
- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/plan.md` — the implementation plan.
- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/review-code-holistic-<model>.md`, `review-code-detail-<model>.md`, and `review-code-scope-<model>.md` — the three parallel code reviews.
- `~/.local/state/claude-gremlins/<gremlin-id>/log` — combined stdout/stderr of the gremlin.
- `~/.local/state/claude-gremlins/<gremlin-id>/state.json` — gremlin status, exit code, workdir path, branch name.
- `bg/localgremlin/<gremlin-id>` — durable branch with **only** the code changes (no scaffolding). From the main working tree: `git checkout bg/localgremlin/<gremlin-id>` to inspect, merge, or discard. A squash-merge pulls in product code cleanly.

Commits on the branch, in order: implementation → "Address review feedback" (absent if reviewers found nothing).

On success the isolated worktree is removed — `state.json`'s `workdir` field will point to a nonexistent path, which is expected (the branch is the durable code record; the artifacts directory is the durable review record). On failure the worktree is preserved for debugging at the path still recorded in `state.json`.

## Arguments

$ARGUMENTS

Forward them verbatim to the launcher. Quote the instructions string so shell word-splitting doesn't break it.

## What to do

If `$ARGUMENTS` begins with `--design` (it must be the first token), strip that flag and invoke the design skill instead of the launcher:

```
Skill(skill="design", args="--target localgremlin <remaining-args>")
```

Do not proceed to the launcher invocation. The design skill will run an interactive design conversation and, when the user is ready, will invoke the launcher automatically.

---

Before invoking the launcher, compose a short (≤60 characters) human-readable phrase that summarizes the task — this becomes the gremlin's `description` in status views (`/gremlins`, session-summary hook). Examples:

- task "add a /gremlins skill that prints status of background gremlins" → `"add /gremlins status command"`
- task "fix the race in the dual-reviewer wait loop" → `"fix dual-reviewer race"`

Pass it as `--description "<phrase>"` before the `localgremlin` kind argument:

```
~/.claude/skills/_bg/launch.sh --description "<phrase>" localgremlin $ARGUMENTS
```

If $ARGUMENTS is so terse that a distilled phrase wouldn't add anything, you may omit `--description` — the launcher falls back to the first 60 chars of the instructions.

Report the gremlin id, workdir, and log path that it prints. Make clear to the user:

- The gremlin is running in the background — their session is free immediately.
- They do **not** need to keep this Claude Code session open.
- They will see a notification in a future session (any project-scoped session) once the gremlin finishes.

## Do not

- Do not tail the log or block waiting for the gremlin to finish.
- Do not pass extra flags the launcher doesn't accept.
- Do not invoke the gremlin script (`localgremlin.py`) directly — always go through the launcher.
- Do not run the individual stages inline.
