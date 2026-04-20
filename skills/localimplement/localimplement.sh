#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Kill background reviewer subprocesses (and any other children) if the user
# Ctrl-C's a long run, so orphaned `claude -p` calls don't keep burning tokens
# after the script exits.
trap 'trap - INT TERM; kill -- -$$ 2>/dev/null; exit 130' INT TERM

die() { echo "error: $*" >&2; exit 1; }

# Tee stream-json to stdout while printing a live progress trace of tool_use
# events to stderr. Optional LABEL prefix lets parallel reviewers be told apart.
progress_tee() {
  local label="${1:-}"
  local prefix="    ·"
  [[ -n "$label" ]] && prefix="    [$label] ·"
  tee >(jq -r --unbuffered --arg prefix "$prefix" '
    select(.type=="assistant") | .message.content[]?
    | select(.type=="tool_use")
    | "\($prefix) \(.name) \(.input.file_path // .input.command // .input.pattern // "")"
  ' 2>/dev/null >&2)
}

MODEL_A="opus"
MODEL_B="sonnet"
while getopts "a:b:" opt; do
  case "$opt" in
    a) MODEL_A="$OPTARG" ;;
    b) MODEL_B="$OPTARG" ;;
    *) die "usage: localimplement.sh [-a <model>] [-b <model>] \"<instructions>\"" ;;
  esac
done
shift $((OPTIND - 1))
[[ $# -ge 1 ]] || die "usage: localimplement.sh [-a <model>] [-b <model>] \"<instructions>\""
INSTRUCTIONS="$*"

# Validate model aliases conservatively. INSTRUCTIONS is intentionally *not*
# sanitized: it is the prompt this tool exists to send. Callers should treat
# INSTRUCTIONS as a prompt to an unrestricted agent (we run with
# --permission-mode bypassPermissions below), not an opaque arg.
for m in "$MODEL_A" "$MODEL_B"; do
  [[ "$m" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid model: $m"
done

command -v claude >/dev/null || die "claude CLI not found"
command -v jq >/dev/null     || die "jq not found"

# Session directory under CWD. Everything produced by this run — the original
# plan, the revised plan, both rounds of reviews, and implicitly any
# working-tree diff — lives here so the user can inspect or discard the full
# artifact set as a unit.
TS=$(date +%Y%m%d-%H%M%S)
SESSION_DIR=".claude-workflow/$TS"
mkdir -p "$SESSION_DIR"
PLAN_FILE="$SESSION_DIR/plan.md"
REVISED_PLAN_FILE="$SESSION_DIR/plan-revised.md"
REVIEW_PLAN_A="$SESSION_DIR/review-plan-holistic-$MODEL_A.md"
REVIEW_PLAN_B="$SESSION_DIR/review-plan-detail-$MODEL_B.md"
REVIEW_CODE_A="$SESSION_DIR/review-code-holistic-$MODEL_A.md"
REVIEW_CODE_B="$SESSION_DIR/review-code-detail-$MODEL_B.md"

echo "==> session: $SESSION_DIR"

CLAUDE_FLAGS=(--permission-mode bypassPermissions --output-format stream-json --verbose)

IN_GIT=0
git rev-parse --git-dir >/dev/null 2>&1 && IN_GIT=1

# Reviewer focuses. Same pair of lenses (holistic / detail) is reused for the
# plan-review round and the code-review round, but the language is adapted to
# the artifact actually under review. Each lens is told to stay out of the
# other's lane so the two reviews are complementary, not redundant. The lens
# prose lives in four sibling files (lens-{holistic,detail}-{plan,code}.md) so
# prompt edits don't churn this script.

for lens in lens-holistic-plan.md lens-detail-plan.md lens-holistic-code.md lens-detail-code.md; do
  [[ -s "$SCRIPT_DIR/$lens" ]] || die "missing or empty lens file: $SCRIPT_DIR/$lens"
done
FOCUS_PLAN_A=$(cat "$SCRIPT_DIR/lens-holistic-plan.md")
FOCUS_PLAN_B=$(cat "$SCRIPT_DIR/lens-detail-plan.md")
FOCUS_CODE_A=$(cat "$SCRIPT_DIR/lens-holistic-code.md")
FOCUS_CODE_B=$(cat "$SCRIPT_DIR/lens-detail-code.md")

# Generic reviewer runner. CONTEXT describes what is being reviewed (the plan,
# or an implementation diff against the plan); FOCUS is the lens prompt above;
# WHERE_FIELD is the field label used to cite findings (e.g. "**File:** path:line"
# for code reviews, "**Where:** plan section / task" for plan reviews).
# Both rounds funnel through here so the holistic/detail split is identical
# across plan-review and code-review.
run_review() {
  local model="$1" out_file="$2" focus="$3" context="$4" where_field="$5"
  claude -p --model "$model" "${CLAUDE_FLAGS[@]}" \
    "$context

$focus

Read surrounding code as needed — don't review in isolation.

Write your review to \`$out_file\` as markdown, structured as:

# Review ($model)

## Summary
2-4 sentences overall.

## Findings
For each actionable finding:
### <short title>
- $where_field
- **Severity:** blocker | major | minor | nit
- **What:** what's wrong
- **Fix:** concrete suggestion

If there are no issues worth raising, write a Findings section that says so explicitly.

Do NOT make any code changes — only write the review file."
}

# Run two reviewers in parallel with the same context but different lenses,
# then validate that both produced non-empty output. The two-reviewer parallel
# pattern is identical for plan-review and code-review, so factor it out.
#
# The `( ... ; exit ${PIPESTATUS[0]} ) &` wrapping is deliberate: without it,
# `$!` captures the PID of `progress_tee` (the last stage of the pipeline),
# and `tee` exits 0 whenever its stdin closes — so a non-zero exit from
# `claude -p` would be silently swallowed. PIPESTATUS[0] propagates the
# reviewer's exit code out of the subshell instead.
run_dual_review() {
  local context="$1" focus_a="$2" focus_b="$3" out_a="$4" out_b="$5" where_field="$6"
  ( run_review "$MODEL_A" "$out_a" "$focus_a" "$context" "$where_field" | progress_tee "$MODEL_A" >/dev/null; exit "${PIPESTATUS[0]}" ) &
  local pid_a=$!
  ( run_review "$MODEL_B" "$out_b" "$focus_b" "$context" "$where_field" | progress_tee "$MODEL_B" >/dev/null; exit "${PIPESTATUS[0]}" ) &
  local pid_b=$!
  local fail=0
  wait "$pid_a" || { echo "review $MODEL_A failed" >&2; fail=1; }
  wait "$pid_b" || { echo "review $MODEL_B failed" >&2; fail=1; }
  [[ $fail -eq 0 ]] || die "one or more reviews failed"
  [[ -s "$out_a" ]] || die "review $MODEL_A did not produce $out_a"
  [[ -s "$out_b" ]] || die "review $MODEL_B did not produce $out_b"
}

echo "==> [1/6] planning -> $PLAN_FILE"
claude -p "${CLAUDE_FLAGS[@]}" \
  "Create a detailed implementation plan for the following task and write it to the file \`$PLAN_FILE\`. Use this structure:

## Context
What problem are we solving and why.

## Approach
High-level strategy. Why this approach over alternatives.

## Tasks
- [ ] Task 1: concrete, specific description
- [ ] Task 2: concrete, specific description

## Open questions
Anything that needs discussion before implementation.

Read any relevant code in the repo to inform the plan. Do NOT make any code changes yet — only write the plan file.

Task: $INSTRUCTIONS" \
  | progress_tee >/dev/null
[[ -s "$PLAN_FILE" ]] || die "plan stage did not produce $PLAN_FILE"

echo "==> [2/6] reviewing plan in parallel (models: $MODEL_A, $MODEL_B)"
PLAN_REVIEW_CONTEXT="You are reviewing the implementation plan at \`$PLAN_FILE\`. Read the plan first, then read whatever code in the repo it touches so your review reflects what the implementation will actually have to deal with."
run_dual_review "$PLAN_REVIEW_CONTEXT" "$FOCUS_PLAN_A" "$FOCUS_PLAN_B" "$REVIEW_PLAN_A" "$REVIEW_PLAN_B" "**Where:** plan section / task title"
echo "    holistic plan review ($MODEL_A): $REVIEW_PLAN_A"
echo "    detail plan review   ($MODEL_B): $REVIEW_PLAN_B"

echo "==> [3/6] addressing plan reviews -> $REVISED_PLAN_FILE"
claude -p "${CLAUDE_FLAGS[@]}" \
  "Two independent reviews of the implementation plan at \`$PLAN_FILE\` are at:
- \`$REVIEW_PLAN_A\` — **holistic** reviewer (model: $MODEL_A).
- \`$REVIEW_PLAN_B\` — **detail** reviewer (model: $MODEL_B).

Read the original plan and both reviews. The reviewers have different lenses by design, so their findings will mostly be complementary — still deduplicate where they overlap. For every finding you agree with, revise the plan accordingly. For findings you disagree with or choose to skip, record them briefly with a reason.

Write the revised plan to \`$REVISED_PLAN_FILE\`. You may restructure freely — the original \`$PLAN_FILE\` is preserved separately, so a human can diff the two to see what changed. End \`$REVISED_PLAN_FILE\` with a short '## Address notes' section listing what you incorporated, what you skipped, and why.

Do NOT make any code changes — only write \`$REVISED_PLAN_FILE\`." \
  | progress_tee >/dev/null
[[ -s "$REVISED_PLAN_FILE" ]] || die "address-plan stage did not produce $REVISED_PLAN_FILE"

# Commit planning artifacts to the branch so the session dir survives worktree
# removal. -f bypasses a user-side .gitignore that lists .claude-workflow/.
if [[ $IN_GIT -eq 1 ]]; then
  git add -f "$SESSION_DIR"
  git diff --cached --quiet \
    || git commit -m "Add planning artifacts (localimplement $TS)" >/dev/null
fi

echo "==> [4/6] implementing (from $REVISED_PLAN_FILE)"
PRE_HEAD=""
PRE_IMPL_SENTINEL=""
if [[ $IN_GIT -eq 1 ]]; then
  PRE_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
else
  PRE_IMPL_SENTINEL="$SESSION_DIR/.pre-impl"
  touch "$PRE_IMPL_SENTINEL"
fi

IMPL_COMMIT_INSTR="."
[[ $IN_GIT -eq 1 ]] && IMPL_COMMIT_INSTR=", stage the changed files by name and create a single git commit with a clear message that references \`$REVISED_PLAN_FILE\`. Do NOT stage anything under \`.claude-workflow/\` — those files are owned by the workflow script and will be committed separately. Do not push."

claude -p "${CLAUDE_FLAGS[@]}" \
  "Read the revised implementation plan at \`$REVISED_PLAN_FILE\` and implement every task in it by editing code in this repo. The original (un-revised) plan is at \`$PLAN_FILE\` for reference, but the revised plan is the source of truth. When the implementation is complete${IMPL_COMMIT_INSTR}" \
  | progress_tee >/dev/null

# Guard: the implement stage must have actually changed something (spec
# invariant: "an empty implementation should never flow into code review").
# In a git repo we check for a new HEAD commit or uncommitted changes; outside
# git we look for any file mtime newer than a sentinel we touched above,
# excluding the session dir (which collects its own artifacts) and .git.
if [[ $IN_GIT -eq 1 ]]; then
  POST_HEAD=$(git rev-parse HEAD)
  if [[ "$POST_HEAD" == "$PRE_HEAD" ]] && [[ -z "$(git status --porcelain)" ]]; then
    die "implementation stage produced no changes; aborting"
  fi
else
  if [[ -z "$(find . -newer "$PRE_IMPL_SENTINEL" -type f -not -path "./$SESSION_DIR/*" -not -path "./.git/*" -print -quit 2>/dev/null)" ]]; then
    die "implementation stage produced no changes; aborting"
  fi
fi

echo "==> [5/6] reviewing code in parallel (models: $MODEL_A, $MODEL_B)"

CODE_SCOPE=""
if [[ $IN_GIT -eq 1 ]]; then
  CODE_SCOPE="Review the changes introduced by the most recent commit (HEAD vs HEAD~1) plus any uncommitted working-tree changes. Use \`git diff HEAD~1 HEAD\` and \`git diff\` to see the scope."
else
  CODE_SCOPE="Review the uncommitted changes in this directory (\`git diff\` if available, otherwise inspect recently modified files)."
fi
CODE_REVIEW_CONTEXT="You are reviewing an implementation of the revised plan at \`$REVISED_PLAN_FILE\` (the original plan is at \`$PLAN_FILE\` for reference, but the revised plan is the source of truth). Read the revised plan first for context.

$CODE_SCOPE"
run_dual_review "$CODE_REVIEW_CONTEXT" "$FOCUS_CODE_A" "$FOCUS_CODE_B" "$REVIEW_CODE_A" "$REVIEW_CODE_B" "**File:** \`path/to/file.ext:<line>\`"
echo "    holistic code review ($MODEL_A): $REVIEW_CODE_A"
echo "    detail code review   ($MODEL_B): $REVIEW_CODE_B"

echo "==> [6/6] addressing code reviews"
ADDRESS_COMMIT_INSTR=""
[[ $IN_GIT -eq 1 ]] && ADDRESS_COMMIT_INSTR="After making all fixes, stage the changed files by name and create a single git commit titled 'Address review feedback' whose body references both review files. Do not push."

claude -p "${CLAUDE_FLAGS[@]}" \
  "Two independent code reviews of the most recent implementation are at:
- \`$REVIEW_CODE_A\` — **holistic** reviewer (model: $MODEL_A).
- \`$REVIEW_CODE_B\` — **detail** reviewer (model: $MODEL_B).

Read both reviews. The two reviewers have different lenses by design, so their findings will mostly be complementary rather than overlapping — still deduplicate where they do overlap. For every actionable finding you agree with, make the fix in the code. For findings you disagree with or choose to skip, note them briefly in your final summary with a reason.

$ADDRESS_COMMIT_INSTR

End with a short summary (to stdout) of: what you addressed, what you skipped and why." \
  | progress_tee >/dev/null

# Commit code-review artifacts. The guard is load-bearing: the model's
# "Address review feedback" commit above may already have swept in review files.
if [[ $IN_GIT -eq 1 ]]; then
  git add -f "$SESSION_DIR"
  git diff --cached --quiet \
    || git commit -m "Add code-review artifacts (localimplement $TS)" >/dev/null
fi

echo ""
echo "done. session artifacts in: $SESSION_DIR"
