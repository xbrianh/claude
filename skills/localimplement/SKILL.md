---
name: localimplement
description: Run the end-to-end plan → review-plan → address-plan → implement → review-code → address-code workflow entirely locally, invoking ~/.claude/skills/localimplement/localimplement.sh. All artifacts (original plan, revised plan, both review rounds) are written to .claude-workflow/<timestamp>/ in the current working directory; each review round uses two different models in parallel.
argument-hint: [-a <model>] [-b <model>] <instructions>
allowed-tools: Bash(~/.claude/skills/localimplement/localimplement.sh:*)
---

You are running the `localimplement` workflow. This is a thin wrapper over the shell script at `~/.claude/skills/localimplement/localimplement.sh`, which orchestrates six `claude -p` stages end-to-end with no GitHub involvement.

## Arguments

$ARGUMENTS

Forward them verbatim to the script. Quote the instructions string so shell word-splitting doesn't break it.

## What the script does

1. **plan** — writes `plan.md` to `.claude-workflow/<timestamp>/` in CWD.
2. **review plan × 2 (parallel)** — two reviewers read `plan.md` on different models with different lenses:
   - Reviewer **A** (default `opus`) — *holistic* lens, defined in [`lens-holistic-plan.md`](lens-holistic-plan.md).
   - Reviewer **B** (default `sonnet`) — *detail* lens, defined in [`lens-detail-plan.md`](lens-detail-plan.md).
   - Outputs `review-plan-holistic-<model-a>.md` and `review-plan-detail-<model-b>.md`.
3. **address plan reviews** — reads both plan reviews and writes the revised plan to `plan-revised.md`. The original `plan.md` is preserved so the diff between the two is inspectable.
4. **implement** — reads `plan-revised.md` (the revised plan, not the original) and edits code per its tasks; commits if in a git repo.
5. **review code × 2 (parallel)** — same two-model, two-lens pair as stage 2, but reviewing the implementation diff:
   - Reviewer **A** — *holistic* lens, defined in [`lens-holistic-code.md`](lens-holistic-code.md).
   - Reviewer **B** — *detail* lens, defined in [`lens-detail-code.md`](lens-detail-code.md).
   - Outputs `review-code-holistic-<model-a>.md` and `review-code-detail-<model-b>.md`.
6. **address code reviews** — reads both code reviews, fixes findings, commits.

Models are configurable with `-a <model>` (holistic reviewer) and `-b <model>` (detail reviewer); the same pair is used for both review rounds. Aliases like `opus` / `sonnet` / `haiku`, or full model IDs.

Nothing is ever pushed to a remote. An empty plan, empty review, empty revised plan, or no-op implementation each abort the run.

## What to do

Run the script:

```
~/.claude/skills/localimplement/localimplement.sh $ARGUMENTS
```

Stream its output directly so the user can see per-step progress (`[1/6] planning`, `[2/6] reviewing plan`, etc.). When it finishes, the script prints the session directory path — echo that back to the user as your answer.

If the script exits non-zero, report which stage failed (based on the last `==> [N/6]` line printed before the error) and include the stderr output so the user can diagnose.

## Do not

- Do not re-implement the workflow inline. The script is the source of truth.
- Do not pass extra flags the script doesn't accept.
- Do not run the individual stages yourself — the script invokes them via nested `claude -p` calls.
