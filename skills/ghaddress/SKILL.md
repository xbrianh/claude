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
2. For each comment, decide whether it is in-scope to address in this PR or out-of-scope (OOS). For each in-scope comment:
   a. Understand what the reviewer is asking for.
   b. Read the relevant code to understand the current state.
   c. Make the fix or change requested.
3. After making all code changes, stage, commit, and push:
   a. `git add` the changed files (by name, not `-A`).
   b. `git commit` with a message summarizing what was addressed.
   c. `git push` to the PR branch.
4. Only after the push succeeds, reply to each comment thread. Skip comments that have already been resolved.
   - For in-scope comments you addressed: reply briefly acknowledging the fix.
   - For questions or acknowledgements that need no code change: reply briefly.
   - For OOS comments: run the OOS triage in the next section before replying.
   - Post replies to review comments with `gh api repos/{owner}/{repo}/pulls/<number>/comments/{comment_id}/replies -f body="<reply>"`.
5. Summarize what was done, including any issues filed for OOS comments and any `gh issue create` failures.

## Out-of-scope triage (file issues for real defects)

For each comment you marked OOS, decide whether it looks like a real defect or noise. The goal is that real defects survive the OOS decision as filed issues, while pure noise doesn't pollute the issue tracker.

**File a new issue when the OOS comment flags any of:**

- A bug or regression
- A security or data-correctness concern
- A performance pathology
- A hidden invariant being violated
- Anything else that, if true, would warrant a fix in some future PR

**Don't file an issue when:**

- The reviewer's claim is wrong — they misread the code, missed context, or are factually incorrect.
- The comment is a subjective style nit, naming bikeshed, or "consider extracting…" preference with no defect claim.
- The reviewer themselves marked the comment as non-blocking, nit, or fyi.
- The comment is already addressed elsewhere (e.g., another comment in the same review covers the same point).

**Tie-breaker:** If you're genuinely unsure whether the comment is a real defect or the reviewer is wrong, **file the issue**. Over-filing is cheap; losing a real bug is not. A human triaging the issue can close it as invalid.

### Filing the issue

Use `gh issue create` with:

- **Title**: a short distillation of the comment (no special prefix). Aim for a sentence fragment that names the defect, not the comment ID.
- **Body**: must stand on its own — a reader should not need to chase the PR to understand the issue. Include:
  - A short summary of the defect in your own words.
  - The reviewer's comment, quoted or summarized, so the original framing is preserved.
  - `Ref #<pr-number>` so GitHub cross-links the PR.
  - A permalink to the originating review comment (the comment's `html_url` from the API response).

Example invocation (pass body via heredoc to preserve formatting):

```
gh issue create --title "<short distillation>" --body "$(cat <<'EOF'
<summary>

Reviewer comment:
> <quoted or summarized comment>

Ref #<pr-number>
<permalink to review comment>
EOF
)"
```

Capture the issue number/URL from the command's output for the reply.

### Replying on the PR thread

- **Issue filed**: reply `Filed as #N` (or `Filed as <issue-url>`) so the reviewer and any later reader can find it.
- **No issue (noise / reviewer wrong / already addressed)**: reply with a brief dismissal reason — enough that a human skimming the PR understands why no further action was taken.

### When `gh issue create` fails

Issue-creation failure is not a reason to stop addressing the rest of the PR. Do **not** write a bail marker.

- Log the failure prominently in the run output (a clear `ERROR: failed to file issue for comment <id>: <error>` line, and include it in the final summary).
- Fall back to a dismissal-style reply on the PR thread that names what the issue *would* have said (intended title and a one-line summary), so the comment is not silently dropped.
- Continue with the remaining comments.

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
