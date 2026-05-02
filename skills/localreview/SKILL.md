---
name: localreview
description: Run the detail code review over local changes without the full gremlin pipeline. Writes review-code-detail-*.md into --dir (cwd by default). Foreground, not backgrounded.
argument-hint: [--dir <path>] [--plan <path>] [-b <detail-model>]
allowed-tools: Bash(~/.claude/skills/localgremlin/localreview.sh:*)
---

Run a code review on the current local changes using the same detail reviewer as `/localgremlin`'s review-code stage, without planning or implementing anything.

This skill runs **in the foreground** — it blocks until the review finishes. It does not spawn a background gremlin, does not create a worktree, and does not write to `~/.local/state/claude-gremlins/`.

## What it reviews

- In a git repo: the most recent commit (`HEAD~1..HEAD`) plus any uncommitted working-tree changes. If both are empty, it errors rather than run a reviewer on nothing.
- Outside a git repo: uncommitted/recently-modified files in the directory (the reviewer is told to run `git diff` if available, otherwise inspect recent files).

## Output

One markdown file is written into `--dir` (default: cwd):

- `review-code-detail-<model>.md`

The filename matches `/localgremlin`'s review-code stage exactly, so the resulting file can be fed straight into `/localaddress` (point its `--dir` at the same path).

## Arguments

$ARGUMENTS

Forward them verbatim to the script:

```
~/.claude/skills/localgremlin/localreview.sh $ARGUMENTS
```

Flags:

- `--dir <path>` — destination directory for the review file. Must already exist (no silent mkdir). Defaults to cwd.
- `--plan <path>` — optional path to an implementation plan; its contents are passed to the reviewer as context. Errors if the path is missing or empty.
- `-b <model>` — model for the detail reviewer. Defaults to `sonnet`.

After the script exits, report the output filename to the user.
