---
name: localimplement
description: Run the end-to-end plan → review-plan → address-plan → implement → review-code → address-code workflow in the background by invoking ~/.claude/skills/_bg/launch.sh. All artifacts (original plan, revised plan, both review rounds) land inside the isolated workdir's `.claude-workflow/<timestamp>/`; each review round uses two different models in parallel. The launcher returns immediately; you'll be notified when the pipeline finishes.
argument-hint: [-a <model>] [-b <model>] <instructions>
allowed-tools: Bash(~/.claude/skills/_bg/launch.sh:*)
---

You are running the `localimplement` workflow **in the background**. The skill is a thin wrapper over `~/.claude/skills/_bg/launch.sh`, which:

1. Creates an isolated git worktree of the current project on a fresh branch named `bg/localimplement/<workflow-id>` (or `cp -a` copies the tree for non-git projects).
2. Spawns the real pipeline (`~/.claude/skills/localimplement/localimplement.sh`) detached from this session — it survives Ctrl-C, shell exit, and Claude Code quitting.
3. Records per-workflow state under `~/.claude/workflows/<workflow-id>/` (`state.json`, combined `log`, markers).
4. Returns within ~1s.

A `SessionStart` / `UserPromptSubmit` hook notifies a future Claude session for this project when the workflow finishes.

## Where artifacts go

The pipeline commits both code changes and `.claude-workflow/<timestamp>/` artifacts to the `bg/localimplement/<workflow-id>` branch in the user's main repo. Point the user at:

- `~/.claude/workflows/<workflow-id>/log` — combined stdout/stderr of the pipeline.
- `~/.claude/workflows/<workflow-id>/state.json` — workflow status, exit code, workdir path, branch name.
- `bg/localimplement/<workflow-id>` — durable branch with the code changes plus `.claude-workflow/<ts>/` (plan.md, plan-revised.md, review-plan-*.md, review-code-*.md). From the main working tree: `git checkout bg/localimplement/<workflow-id>` to inspect, merge, or discard.

Commits on the branch, in order: planning artifacts → implementation → "Address review feedback" (absent if reviewers found nothing) → code-review artifacts.

On success the isolated worktree is removed — `state.json`'s `workdir` field will point to a nonexistent path, which is expected (the branch is the durable artifact). On failure the worktree is preserved for debugging at the path still recorded in `state.json`.

## Arguments

$ARGUMENTS

Forward them verbatim to the launcher. Quote the instructions string so shell word-splitting doesn't break it.

## What to do

Run the launcher:

```
~/.claude/skills/_bg/launch.sh localimplement $ARGUMENTS
```

Report the workflow id, workdir, and log path that it prints. Make clear to the user:

- The pipeline is running in the background — their session is free immediately.
- They do **not** need to keep this Claude Code session open.
- They will see a notification in a future session (any project-scoped session) once the pipeline finishes.

## Do not

- Do not tail the log or block waiting for the pipeline to finish.
- Do not pass extra flags the launcher doesn't accept.
- Do not invoke the pipeline script (`localimplement.sh`) directly — always go through the launcher.
- Do not run the individual stages inline.
