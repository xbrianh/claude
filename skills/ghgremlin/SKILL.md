---
name: ghgremlin
description: Run the end-to-end plan → implement → review → address workflow in the background by invoking `gremlins launch`. Creates a GitHub issue, opens a PR implementing it, collects Copilot + Claude reviews, and addresses them. The launcher returns immediately; you'll be notified when the gremlin finishes.
argument-hint: [-r <ref>] [--plan <path|issue-ref> | --instructions <instructions>]
allowed-tools: Bash(gremlins launch:*)
---

You are running the `ghgremlin` workflow **in the background**. The skill is a thin wrapper over `gremlins launch`, which:

1. Creates an isolated git worktree of the current project (detached HEAD).
2. Invokes `gremlins gh` in the isolated worktree, detached from this session — it survives Ctrl-C, shell exit, and Claude Code quitting.
3. Records per-gremlin state under `~/.local/state/claude-gremlins/<gremlin-id>/` (or `$XDG_STATE_HOME/claude-gremlins/<gremlin-id>/` if `XDG_STATE_HOME` is set) — `state.json`, combined `log`, markers.
4. Returns within ~1s.

A `SessionStart` / `UserPromptSubmit` hook notifies a future Claude session for this project when the gremlin finishes.

## Where artifacts go

Gremlin artifacts live outside the product branch. Point the user at:

- `~/.local/state/claude-gremlins/<gremlin-id>/artifacts/spec.md` — the spec file, if one was passed as the first positional argument.
- `~/.local/state/claude-gremlins/<gremlin-id>/log` — combined stdout/stderr of the gremlin.
- `~/.local/state/claude-gremlins/<gremlin-id>/state.json` — gremlin status, exit code, workdir path.

## Arguments

$ARGUMENTS

Forward them verbatim to the launcher. Quote the instructions string so shell word-splitting doesn't break it.

## What to do

Before invoking the launcher, compose a short (≤60 characters) human-readable phrase that summarizes the task — this becomes the gremlin's `description` in status views (`/gremlins`, session-summary hook). Examples:

- task "refactor the auth middleware to drop the session-token caching layer" → `"drop auth middleware session caching"`
- task "fix regression where empty PRs pass review" → `"fix empty-PR review regression"`

Pass it as `--description "<phrase>"` before the `ghgremlin` kind argument:

```
gremlins launch --description "<phrase>" ghgremlin $ARGUMENTS
```

If $ARGUMENTS is so terse that a distilled phrase wouldn't add anything, you may omit `--description` — the launcher falls back to the first 60 chars of the instructions.

Report the gremlin id, workdir, and log path that it prints. Make clear to the user:

- The gremlin is running in the background — their session is free immediately.
- They do **not** need to keep this Claude Code session open.
- They will see a notification in a future session (any project-scoped session) once the gremlin finishes; the log path is where the final PR URL and per-stage output will be.

## `--plan <source>`

If the user already has a plan, pass `--plan <source>` to skip the `/ghplan`
stage (no new GitHub issue is created). The plan body is copied into the
gremlin's session as `plan.md` and fed to the implement stage. Four forms:

- **Local file path** — the file contents become the plan. The PR is opened
  with no `Closes #N` link since there's no issue to close.
- **`42` or `#42`** — resolves to issue #42 in the current repo. The PR
  includes `Closes #42`, same as a fresh `/ghplan` run.
- **`owner/repo#42`** — resolves to issue #42 in a different repo. Uses the
  issue body as plan content only; the PR is opened in the current repo
  without a `Closes` link (to avoid auto-closing an unrelated issue in the
  PR's target repo).
- **Full URL `https://github.com/owner/repo/issues/42`** — same cross-repo
  semantics. `github.com` only; GitHub Enterprise hosts are not recognized
  as URLs — use the `owner/repo#42` form instead (which works for any host
  `gh` is configured to reach).

Rules:

- Mutually exclusive with the positional `<instructions>`. Pass exactly one.
- Files must be non-empty; issues must exist and have a non-empty body.
- Failure modes split by where they fire:
  - **Clean (no state dir):** mutex violations (`--plan` + `--instructions`, or
    neither) and missing / empty local file are caught in `gremlins launch`
    before the state directory is created.
  - **Dirty (failed state dir left behind):** issue-ref errors (unreachable
    issue, unrecognized shape, empty issue body) fire in `ghgremlin.sh`
    after the state dir has already been created. Use `/gremlins rm <id>`
    to clean up.

## Do not

- Do not tail the log or block waiting for the gremlin to finish.
- Do not pass extra flags the launcher doesn't accept.
- Do not invoke the gremlin script (`ghgremlin.sh`) directly — always go through `gremlins launch`.
- Do not run the individual skills (`/ghplan`, `/ghreview`, `/ghaddress`) inline — the backgrounded gremlin already chains them.
