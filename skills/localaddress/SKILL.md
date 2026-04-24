---
name: localaddress
description: Address findings from a set of `review-code-{holistic,detail,scope}-*.md` files in --dir, deduping overlaps and fixing actionable items. In a git repo, creates a single 'Address review feedback' commit (no push). Foreground, not backgrounded.
argument-hint: [--dir <path>] [-x <address-model>]
allowed-tools: Bash(~/.claude/skills/localgremlin/localaddress.py:*)
---

Read the three `review-code-{holistic,detail,scope}-*.md` review files from `--dir` and fix the actionable findings in the local code, using the same prompt and behavior as `/localgremlin`'s address-code stage.

This skill runs **in the foreground** — it blocks until the fixes are made. It does not spawn a background gremlin, does not create a worktree, and does not push.

## Inputs

`--dir` must contain exactly one file matching each of:

- `review-code-holistic-*.md`
- `review-code-detail-*.md`
- `review-code-scope-*.md`

If any lens glob has zero or more than one match, the script errors naming which lens is missing or ambiguous.

## Output

- Edits in the working tree corresponding to findings the address stage agreed with.
- In a git repo: a single `Address review feedback` commit whose body references all three review files. No push.
- Outside a git repo: no commit — the changes are left in the working tree.
- A short stdout summary of what was addressed and what was skipped (and why).

## Arguments

$ARGUMENTS

Forward them verbatim to the script:

```
~/.claude/skills/localgremlin/localaddress.py $ARGUMENTS
```

Flags:

- `--dir <path>` — directory containing the three review files. Must already exist. Defaults to cwd.
- `-x <model>` — model used to apply the fixes. Defaults to `sonnet`.

After the script exits, report the summary and, in a git repo, the new commit.
