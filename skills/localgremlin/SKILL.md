---
name: localgremlin
description: Run the end-to-end plan → implement → review-code → address-code workflow in the background by invoking ~/.claude/skills/_bg/launch.sh. Plan and code review land in `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/` alongside the run log (kept off the product branch); a single detail review is produced. The launcher returns immediately; you'll be notified when the gremlin finishes.
argument-hint: [-p <plan-model>] [-i <impl-model>] [-x <address-model>] [-b <detail-review-model>] [--plan <path> | <instructions>]
allowed-tools: Bash(~/.claude/skills/_bg/launch.sh:*)
---

You are running the `localgremlin` workflow **in the background**. The skill is a thin wrapper over `~/.claude/skills/_bg/launch.sh`, which:

1. Creates an isolated git worktree of the current project on a fresh branch named `bg/localgremlin/<gremlin-id>` (or `cp -a` copies the tree for non-git projects).
2. Invokes `python -m gremlins.cli local` in the isolated worktree, detached from this session — it survives Ctrl-C, shell exit, and Claude Code quitting.
3. Records per-gremlin state under `~/.local/state/claude-gremlins/<gremlin-id>/` (or `$XDG_STATE_HOME/claude-gremlins/<gremlin-id>/` if `XDG_STATE_HOME` is set) — `state.json`, combined `log`, markers.
4. Returns within ~1s.

A `SessionStart` / `UserPromptSubmit` hook notifies a future Claude session for this project when the gremlin finishes.

## Where artifacts go

Plan and code-review artifacts live outside the product branch — they are scaffolding, not product. Point the user at:

- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/spec.md` — the spec file, if one was passed as the first positional argument.
- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/plan.md` — the implementation plan.
- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/review-code-detail-<model>.md` — the detail code review.
- `~/.local/state/claude-gremlins/<gremlin-id>/log` — combined stdout/stderr of the gremlin.
- `~/.local/state/claude-gremlins/<gremlin-id>/state.json` — gremlin status, exit code, workdir path, branch name.
- `bg/localgremlin/<gremlin-id>` — durable branch with **only** the code changes (no scaffolding). From the main working tree: `git checkout bg/localgremlin/<gremlin-id>` to inspect, merge, or discard. A squash-merge pulls in product code cleanly.

Commits on the branch, in order: implementation → "Address review feedback" (absent if reviewers found nothing).

On success the isolated worktree is removed — `state.json`'s `workdir` field will point to a nonexistent path, which is expected (the branch is the durable code record; the artifacts directory is the durable review record). On failure the worktree is preserved for debugging at the path still recorded in `state.json`.

## Arguments

$ARGUMENTS

Forward them verbatim to the launcher. Quote the instructions string so shell word-splitting doesn't break it.

## What to do

Before invoking the launcher, compose a short (≤60 characters) human-readable phrase that summarizes the task — this becomes the gremlin's `description` in status views (`/gremlins`, session-summary hook). Examples:

- task "add a /gremlins skill that prints status of background gremlins" → `"add /gremlins status command"`
- task "fix the off-by-one in the review-code stage exit path" → `"fix review-code stage exit bug"`

Pass it as `--description "<phrase>"` before the `localgremlin` kind argument:

```
~/.claude/skills/_bg/launch.sh --description "<phrase>" localgremlin $ARGUMENTS
```

If $ARGUMENTS is so terse that a distilled phrase wouldn't add anything, you may omit `--description` — the launcher falls back to the first 60 chars of the instructions.

Report the gremlin id, workdir, and log path that it prints. Make clear to the user:

- The gremlin is running in the background — their session is free immediately.
- They do **not** need to keep this Claude Code session open.
- They will see a notification in a future session (any project-scoped session) once the gremlin finishes.

## `--plan <path>`

If the user already has an implementation plan, pass `--plan <path>` to skip
the plan stage. The file's contents are copied into the gremlin's session as
`plan.md` and the implement stage reads them as-is.

- Mutually exclusive with the positional `<instructions>`. Pass exactly one.
- The path must point to a readable, non-empty file.
- The gremlin's description defaults to the first `# heading` in the plan
  file unless `--description` is supplied explicitly.
- Errors (file missing, file empty, both `--plan` and positional supplied,
  neither supplied) are surfaced in `launch.sh` before the state directory
  is created, so a bad invocation leaves no state-dir litter behind.

## Do not

- Do not tail the log or block waiting for the gremlin to finish.
- Do not pass extra flags the launcher doesn't accept.
- Do not invoke `gremlins.cli local` directly — always go through the launcher.
- Do not run the individual stages inline.
