---
name: localreview
description: Run the three parallel code-review lenses (holistic, detail, scope) over local changes without the full gremlin pipeline. Writes review-code-*.md files into --dir (cwd by default). Foreground, not backgrounded.
argument-hint: [--dir <path>] [--plan <path>] [-a <holistic-model>] [-b <detail-model>] [-c <scope-model>]
allowed-tools: Bash(~/.claude/skills/localgremlin/localreview.py:*)
---

Run a code review on the current local changes using the same triple-lens parallel reviewer fan-out as `/localgremlin`'s review-code stage, without planning or implementing anything.

This skill runs **in the foreground** — it blocks until the three reviews finish. It does not spawn a background gremlin, does not create a worktree, and does not write to `~/.local/state/claude-gremlins/`.

## What it reviews

- In a git repo: the most recent commit (`HEAD~1..HEAD`) plus any uncommitted working-tree changes. If both are empty, it errors rather than run reviewers on nothing.
- Outside a git repo: uncommitted/recently-modified files in the directory (the reviewer is told to run `git diff` if available, otherwise inspect recent files).

## Output

Three markdown files are written into `--dir` (default: cwd):

- `review-code-holistic-<model>.md`
- `review-code-detail-<model>.md`
- `review-code-scope-<model>.md`

Filenames match `/localgremlin`'s review-code stage exactly, so the resulting files can be fed straight into `/localaddress` (point its `--dir` at the same path).

## Arguments

$ARGUMENTS

Forward them verbatim to the script:

```
~/.claude/skills/localgremlin/localreview.py $ARGUMENTS
```

Flags:

- `--dir <path>` — destination directory for the three review files. Must already exist (no silent mkdir). Defaults to cwd.
- `--plan <path>` — optional path to an implementation plan; its contents are passed to the reviewers as context. Errors if the path is missing or empty.
- `-a <model>` / `-b <model>` / `-c <model>` — models for the holistic / detail / scope lenses. Each defaults to `sonnet`.

After the script exits, report the three output filenames to the user.
