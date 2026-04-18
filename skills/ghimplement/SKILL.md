---
name: ghimplement
description: Run the end-to-end plan → implement → review → address workflow by invoking ~/bin/ghimplement.sh. Creates a GitHub issue, opens a PR implementing it, collects Copilot + Claude reviews, and addresses them.
argument-hint: [-r <ref>] <instructions>
allowed-tools: Bash(~/bin/ghimplement.sh:*)
---

You are running the `ghimplement` workflow. This is a thin wrapper over the shell script at `~/bin/ghimplement.sh`, which orchestrates six `claude -p` + `gh` steps end-to-end.

## Arguments

$ARGUMENTS

Forward them verbatim to the script. Quote the instructions string so shell word-splitting doesn't break it.

## What to do

Run the script:

```
~/bin/ghimplement.sh $ARGUMENTS
```

Stream its output directly so the user can see per-step progress (`[1/6] running /ghplan`, etc.). When it finishes, the script prints the final PR URL — echo that URL back to the user as your answer.

If the script exits non-zero, report which step failed (based on the last `==> [N/6]` line printed before the error) and include the stderr output so the user can diagnose.

## Do not

- Do not re-implement the workflow inline. The script is the source of truth.
- Do not pass extra flags the script doesn't accept.
- Do not run the individual skills (`/ghplan`, `/ghreview`, `/ghaddress`) yourself — the script already invokes them via nested `claude -p` calls.
