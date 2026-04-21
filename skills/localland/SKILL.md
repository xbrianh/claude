---
name: localland
description: Squash-merge a finished /localimplement workflow branch onto the current branch as a single well-messaged commit, then delete the workflow branch and state directory.
argument-hint: <workflow-id>
allowed-tools: Bash(~/.claude/skills/localland/localland.sh:*), Bash(git *)
---

You are running the `localland` command. It lands a finished `/localimplement` workflow onto your current branch as one clean, squashed commit, then removes all traces of the workflow.

## What to do

The workflow id is `$ARGUMENTS`. Fail fast at every step — if a command exits nonzero, stop and report the error to the user; do not continue.

**Step 1 — validate preconditions**

```
~/.claude/skills/localland/localland.sh --check "$ARGUMENTS"
```

Parse `branch=` and `plan=` from stdout. If the script exits nonzero, report the error and stop.

**Step 2 — read the plan**

Read the file at the `plan=` path. You will use its `## Context` section to write the commit subject and its remaining sections to inform the body.

**Step 3 — stage the changes**

```
~/.claude/skills/localland/localland.sh --squash "$ARGUMENTS"
```

If the script exits nonzero (conflict or other failure), report the error and stop. Do not attempt cleanup.

**Step 4 — inspect the staged diff**

```
git diff --cached
```

Read the diff to understand what actually changed. Use it to trim or sharpen the commit body — if the diff is minimal or self-explanatory, a subject-only commit is fine.

**Step 5 — compose the commit message**

- **Subject**: distill the plan's `## Context` paragraph to ~50–72 chars. Drop scaffolding phrases ("implement", "add support for"); prefer the concrete outcome ("squash-land /localland skill to workflow").
- **Body** (optional): bullet points drawn from the plan's Tasks and any signal from the diff. Prose only if a single sentence captures something the subject can't. Omit if the subject is sufficient.
- No "🤖 Generated…" footer. No ceremony.

**Step 6 — commit**

```
git commit -m "<message>"
```

**Step 7 — cleanup** (only after step 6 succeeds)

```
~/.claude/skills/localland/localland.sh --cleanup "$ARGUMENTS"
```

**Step 8 — report**

Tell the user:
- Which branch was landed and onto what.
- The commit hash (from `git rev-parse HEAD`).
- That the workflow branch and state directory have been removed.

## Do not

- Do not run `--cleanup` before step 6 has succeeded.
- Do not add `--no-verify` to the commit.
- Do not push.
- Do not squash or amend any commits other than the workflow's staged changes.
