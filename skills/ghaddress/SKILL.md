---
name: ghaddress
description: Address review comments on a GitHub PR. Fixes issues raised by reviewers and replies to each comment thread.
argument-hint: <pr-reference> [instructions]
context: fork
agent: general-purpose
allowed-tools: Bash(gh *) Bash(~/.claude/skills/_bg/set-bail.sh:*) Read Glob Grep Edit
---

You are addressing review comments on a GitHub pull request. Your job is to fix the issues raised by reviewers and reply to each comment thread.

## Pull Request

Reference (may be empty): $ARGUMENTS[0]

If no reference was provided, infer the PR from the current branch by running `gh pr view --json number,title,body,author,baseRefName,headRefName`. If a reference was provided, run `gh pr view $ARGUMENTS[0] --json number,title,body,author,baseRefName,headRefName` instead. Store the PR number for use in API calls below.

## Review comments

Fetch review comments by running: `gh api repos/{owner}/{repo}/pulls/<number>/comments --paginate` (strip any `#` prefix from the PR number).

## Issue comments

Fetch issue comments using `gh pr view <number-or-ref> --comments`.

## Instructions

$ARGUMENTS[1:]

## Process

1. Read all review comments above carefully.
2. For each actionable comment:
   a. Understand what the reviewer is asking for.
   b. Read the relevant code to understand the current state.
   c. Make the fix or change requested.
3. After making all code changes, stage, commit, and push:
   a. `git add` the changed files (by name, not `-A`).
   b. `git commit` with a message summarizing what was addressed.
   c. `git push` to the PR branch.
4. Only after the push succeeds, reply to each comment thread:
   - For review comments, use `gh api repos/{owner}/{repo}/pulls/<number>/comments/{comment_id}/replies -f body="<reply>"`.
   - For questions or acknowledgements (no code change needed), reply briefly.
   - Skip comments that have already been resolved.
5. Summarize what was done.

## Bail markers (only when running under a gremlin)

If the env var `GR_ID` is set, you are running inside a background gremlin pipeline. If you cannot safely address one or more comments, write a structured bail marker before finishing — `/gremlins rescue --headless` reads it to decide whether to attempt automated recovery. Do NOT make speculative changes when bailing; just record why and stop.

Use the helper:

- A comment touches **secrets** (credential management, API keys, encryption material) and you cannot safely make the requested change:

  ```
  ~/.claude/skills/_bg/set-bail.sh "$GR_ID" secrets "<one-line reason>"
  ```

- For any other reason you decline to proceed (ambiguous reviewer ask, conflicting comments, scope outside this PR, etc.):

  ```
  ~/.claude/skills/_bg/set-bail.sh "$GR_ID" other "<one-line reason>"
  ```

In both cases, also state in your final summary that you bailed and why. If you successfully addressed every actionable comment, do not write a bail marker — just exit normally.
