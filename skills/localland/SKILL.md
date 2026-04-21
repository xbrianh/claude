---
name: localland
description: Squash-merge a finished /localimplement workflow branch onto the current branch as a single well-messaged commit, then delete the workflow branch and state directory.
argument-hint: [--gh] <workflow-id>
allowed-tools: Bash(~/.claude/skills/localland/localland.sh:*), Bash(git *), Bash(gh *)
---

You are running the `localland` command. It lands a finished `/localimplement` workflow onto your current branch as one clean, squashed commit, then removes all traces of the workflow. With `--gh`, it pushes the result as a new GitHub PR against `main` instead of committing locally.

## What to do

Parse `$ARGUMENTS` to detect the optional `--gh` flag:
- If `$ARGUMENTS` starts with `--gh `, set `GH_MODE=true` and treat the remainder as `WF_ID`.
- Otherwise set `GH_MODE=false` and treat `$ARGUMENTS` as `WF_ID`.

Fail fast at every step — if a command exits nonzero, stop and report the error to the user; do not continue.

**Step 1 — validate preconditions**

```
~/.claude/skills/localland/localland.sh --check "$WF_ID"
```

Parse `branch=` and `plan=` from stdout. If the script exits nonzero, report the error and stop.

**Step 1b — `--gh` preflight** *(only when `GH_MODE=true`)*

Derive `pr_branch=pr/$WF_ID`. Then:

```
~/.claude/skills/localland/localland.sh --gh-preflight "$WF_ID" "$pr_branch"
```

If the script exits nonzero, report the error and stop. Do not proceed to squash.

**Step 2 — read the plan**

Read the file at the `plan=` path. You will use its `## Context` section to write the commit subject and its remaining sections to inform the body.

**Step 3 — stage the changes**

*If `GH_MODE=false`:*

```
~/.claude/skills/localland/localland.sh --squash "$WF_ID"
```

*If `GH_MODE=true`:*

```
~/.claude/skills/localland/localland.sh --gh-squash "$WF_ID" "$pr_branch"
```

After `--gh-squash` succeeds, the working tree is on `$pr_branch` (not the original branch).

If either script exits nonzero (conflict or other failure), report the error and stop. Do not attempt cleanup.

**Step 4 — inspect the staged diff**

```
git diff --cached
```

Read the diff to understand what actually changed. Use it to trim or sharpen the commit body — if the diff is minimal or self-explanatory, a subject-only commit is fine.

**Step 5 — compose the commit message**

- **Subject**: distill the plan's `## Context` paragraph to ~50–72 chars. Drop scaffolding phrases ("implement", "add support for"); prefer the concrete outcome ("squash-land /localland skill to workflow").
- **Body** (optional): bullet points drawn from the plan's Tasks and any signal from the diff. Prose only if a single sentence captures something the subject can't. Omit if the subject is sufficient.
- No "🤖 Generated…" footer. No ceremony.

**Step 6 — commit (and push + PR when `GH_MODE=true`)**

*If `GH_MODE=false`:*

```
git commit -m "<message>"
```

*If `GH_MODE=true`:*

```
git commit -m "<message>"
git push -u origin "$pr_branch"
printf '%s' "<body>" > /tmp/pr-body-$$.md
gh pr create --base main --title "<subject>" --body-file /tmp/pr-body-$$.md
rm -f /tmp/pr-body-$$.md
```

Write the PR body to a temp file before calling `gh pr create` to avoid shell-quoting hazards with newlines, backticks, and `$` signs in the body.

Capture the PR URL from `gh pr create` stdout. If `git push` or `gh pr create` exits nonzero, stop and report the error — do not run cleanup.

**Step 7 — cleanup** (only after step 6 succeeds)

```
~/.claude/skills/localland/localland.sh --cleanup "$WF_ID"
```

**Step 8 — report**

*If `GH_MODE=false`:*

Tell the user:
- Which branch was landed and onto what.
- The commit hash (from `git rev-parse HEAD`).
- That the workflow branch and state directory have been removed.

*If `GH_MODE=true`:*

Tell the user:
- The PR URL.
- That the PR branch `$pr_branch` was pushed and kept.
- That the workflow branch and state directory have been removed.
- That the working tree is now on `$pr_branch`; they can return to their previous branch with `git checkout <original_branch>`.

## Do not

- Do not run `--cleanup` before step 6 has succeeded.
- Do not add `--no-verify` to the commit.
- Do not push (except as part of the `--gh` flow in step 6).
- Do not squash or amend any commits other than the workflow's staged changes.
