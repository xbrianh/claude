#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Kill background reviewer subprocesses (and any other children) if the user
# Ctrl-C's a long run, so orphaned `claude -p` calls don't keep burning tokens
# after the script exits.
trap 'trap - INT TERM; kill -- -$$ 2>/dev/null; exit 130' INT TERM

die() { echo "error: $*" >&2; exit 1; }

# Stage helper: only meaningful when this script runs under the _bg launcher
# (which exports WF_ID and creates ~/.claude/workflows/<WF_ID>/state.json).
# A direct CLI invocation has no WF_ID and set_stage no-ops.
SET_STAGE_SH="$HOME/.claude/skills/_bg/set-stage.sh"
set_stage() {
  [[ -n "${WF_ID:-}" ]] || return 0
  [[ -x "$SET_STAGE_SH" ]] || return 0
  "$SET_STAGE_SH" "$WF_ID" "$@" >/dev/null 2>&1 || true
}

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

MODEL_PLAN="sonnet"
MODEL_IMPL="sonnet"
MODEL_ADDR="sonnet"
MODEL_A="sonnet"
MODEL_B="sonnet"
USAGE="usage: localimplement.sh [-p <plan-model>] [-i <impl-model>] [-x <address-model>] [-a <holistic-review-model>] [-b <detail-review-model>] \"<instructions>\""
while getopts "p:i:x:a:b:" opt; do
  case "$opt" in
    p) MODEL_PLAN="$OPTARG" ;;
    i) MODEL_IMPL="$OPTARG" ;;
    x) MODEL_ADDR="$OPTARG" ;;
    a) MODEL_A="$OPTARG" ;;
    b) MODEL_B="$OPTARG" ;;
    *) die "$USAGE" ;;
  esac
done
shift $((OPTIND - 1))
[[ $# -ge 1 ]] || die "$USAGE"
INSTRUCTIONS="$*"

# Validate model aliases conservatively. INSTRUCTIONS is intentionally *not*
# sanitized: it is the prompt this tool exists to send. Callers should treat
# INSTRUCTIONS as a prompt to an unrestricted agent (we run with
# --permission-mode bypassPermissions below), not an opaque arg.
for m in "$MODEL_PLAN" "$MODEL_IMPL" "$MODEL_ADDR" "$MODEL_A" "$MODEL_B"; do
  [[ "$m" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid model: $m"
done

command -v claude >/dev/null || die "claude CLI not found"
command -v jq >/dev/null     || die "jq not found"

# Session directory. Under the _bg launcher (WF_ID set) artifacts live under
# ~/.claude/workflows/<WF_ID>/artifacts/ so they stay out of the product branch
# and survive worktree removal. Direct CLI invocation falls back to the old
# in-tree `.claude-workflow/<ts>/` path — convenient for ad-hoc use, but the
# user is on their own to gitignore or clean it up.
#
# WF_ID is interpolated into a filesystem path under $HOME, so validate it
# against a conservative charset to prevent path traversal (e.g. "../") or
# embedded slashes when it's set externally. The _bg launcher produces IDs
# matching ^[a-z0-9-]+$, so this pattern is a strict superset.
TS=$(date +%Y%m%d-%H%M%S)
if [[ -n "${WF_ID:-}" ]]; then
  [[ "$WF_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid WF_ID: $WF_ID"
  SESSION_DIR="$HOME/.claude/workflows/$WF_ID/artifacts"
else
  SESSION_DIR=".claude-workflow/$TS"
fi
mkdir -p "$SESSION_DIR"
PLAN_FILE="$SESSION_DIR/plan.md"
REVIEW_CODE_A="$SESSION_DIR/review-code-holistic-$MODEL_A.md"
REVIEW_CODE_B="$SESSION_DIR/review-code-detail-$MODEL_B.md"

echo "==> session: $SESSION_DIR"

CLAUDE_FLAGS=(--permission-mode bypassPermissions --output-format stream-json --verbose)

IN_GIT=0
git rev-parse --git-dir >/dev/null 2>&1 && IN_GIT=1

# Reviewer focuses. Two complementary lenses (holistic / detail) are told to
# stay out of each other's lane so the two code reviews are complementary, not
# redundant. The lens prose lives in sibling files so prompt edits don't churn
# this script.

for lens in lens-holistic-code.md lens-detail-code.md; do
  [[ -s "$SCRIPT_DIR/$lens" ]] || die "missing or empty lens file: $SCRIPT_DIR/$lens"
done
FOCUS_CODE_A=$(cat "$SCRIPT_DIR/lens-holistic-code.md")
FOCUS_CODE_B=$(cat "$SCRIPT_DIR/lens-detail-code.md")

# Generic reviewer runner. CONTEXT describes what is being reviewed (an
# implementation diff against the plan); FOCUS is the lens prompt above;
# WHERE_FIELD is the field label used to cite findings (e.g.
# "**File:** path:line" for code reviews).
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
# then validate that both produced non-empty output.
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

  # Inner function names leak to the global namespace in bash — prefix with
  # the outer function name so a grep for `emit_sub_stage` doesn't land here
  # and so redefinitions from anywhere else can't collide.
  _run_dual_review_emit_sub() {
    set_stage review-code "$(jq -cn \
        --arg a "$MODEL_A" --arg b "$MODEL_B" \
        --arg as "$1"      --arg bs "$2" \
        '{($a): $as, ($b): $bs}')"
  }

  local fail=0 a_status="running" b_status="running"
  _run_dual_review_emit_sub "$a_status" "$b_status"

  # Poll: whenever a reviewer process exits, harvest its exit code and emit a
  # sub-stage update so the status command can show mid-flight progress.
  # Correctness depends on bash auto-reaping backgrounded children in
  # non-interactive script mode, so `kill -0 $pid` on an exited child returns
  # ESRCH (we treat that as "exited") rather than succeeding against a zombie.
  while [[ "$a_status" == "running" || "$b_status" == "running" ]]; do
    if [[ "$a_status" == "running" ]] && ! kill -0 "$pid_a" 2>/dev/null; then
      wait "$pid_a" || { echo "review $MODEL_A failed" >&2; fail=1; }
      a_status="done"
      _run_dual_review_emit_sub "$a_status" "$b_status"
    fi
    if [[ "$b_status" == "running" ]] && ! kill -0 "$pid_b" 2>/dev/null; then
      wait "$pid_b" || { echo "review $MODEL_B failed" >&2; fail=1; }
      b_status="done"
      _run_dual_review_emit_sub "$a_status" "$b_status"
    fi
    [[ "$a_status" == "running" || "$b_status" == "running" ]] && sleep 2
  done

  [[ $fail -eq 0 ]] || die "one or more reviews failed"
  [[ -s "$out_a" ]] || die "review $MODEL_A did not produce $out_a"
  [[ -s "$out_b" ]] || die "review $MODEL_B did not produce $out_b"
}

set_stage plan
echo "==> [1/4] planning (model: $MODEL_PLAN) -> $PLAN_FILE"
claude -p --model "$MODEL_PLAN" "${CLAUDE_FLAGS[@]}" \
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

set_stage implement
echo "==> [2/4] implementing (model: $MODEL_IMPL, from $PLAN_FILE)"
PRE_HEAD=""
PRE_IMPL_SENTINEL=""
if [[ $IN_GIT -eq 1 ]]; then
  PRE_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
else
  PRE_IMPL_SENTINEL="$SESSION_DIR/.pre-impl"
  touch "$PRE_IMPL_SENTINEL"
fi

IMPL_COMMIT_INSTR="."
# The commit message references `plan.md` (the artifact basename) rather than
# `$PLAN_FILE` — under the _bg launcher the latter is an absolute user-specific
# path under ~/.claude/workflows/ that would end up in git history otherwise.
[[ $IN_GIT -eq 1 ]] && IMPL_COMMIT_INSTR=", stage the changed files by name and create a single git commit with a clear message that references the implementation plan (refer to it as \`plan.md\` in the commit message, not by absolute path). Do NOT create any meta/scaffolding files in the repo — no \`.claude-workflow/\` directory, no \`plan.md\`, no review docs, no notes-to-self. Do not push."

claude -p --model "$MODEL_IMPL" "${CLAUDE_FLAGS[@]}" \
  "Read the implementation plan at \`$PLAN_FILE\` and implement every task in it by editing code in this repo. When the implementation is complete${IMPL_COMMIT_INSTR}" \
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

set_stage review-code
echo "==> [3/4] reviewing code in parallel (models: $MODEL_A, $MODEL_B)"

CODE_SCOPE=""
if [[ $IN_GIT -eq 1 ]]; then
  CODE_SCOPE="Review the changes introduced by the most recent commit (HEAD vs HEAD~1) plus any uncommitted working-tree changes. Use \`git diff HEAD~1 HEAD\` and \`git diff\` to see the scope."
else
  CODE_SCOPE="Review the uncommitted changes in this directory (\`git diff\` if available, otherwise inspect recently modified files)."
fi
CODE_REVIEW_CONTEXT="You are reviewing an implementation of the plan at \`$PLAN_FILE\`. Read the plan first for context.

$CODE_SCOPE"
run_dual_review "$CODE_REVIEW_CONTEXT" "$FOCUS_CODE_A" "$FOCUS_CODE_B" "$REVIEW_CODE_A" "$REVIEW_CODE_B" "**File:** \`path/to/file.ext:<line>\`"
echo "    holistic code review ($MODEL_A): $REVIEW_CODE_A"
echo "    detail code review   ($MODEL_B): $REVIEW_CODE_B"

set_stage address-code
echo "==> [4/4] addressing code reviews (model: $MODEL_ADDR)"
ADDRESS_COMMIT_INSTR=""
[[ $IN_GIT -eq 1 ]] && ADDRESS_COMMIT_INSTR="After making all fixes, stage the changed files by name and create a single git commit titled 'Address review feedback' whose body references both review files. Do not push."

claude -p --model "$MODEL_ADDR" "${CLAUDE_FLAGS[@]}" \
  "Two independent code reviews of the most recent implementation are at:
- \`$REVIEW_CODE_A\` — **holistic** reviewer (model: $MODEL_A).
- \`$REVIEW_CODE_B\` — **detail** reviewer (model: $MODEL_B).

Read both reviews. The two reviewers have different lenses by design, so their findings will mostly be complementary rather than overlapping — still deduplicate where they do overlap. For every actionable finding you agree with, make the fix in the code. For findings you disagree with or choose to skip, note them briefly in your final summary with a reason.

$ADDRESS_COMMIT_INSTR

End with a short summary (to stdout) of: what you addressed, what you skipped and why." \
  | progress_tee >/dev/null

echo ""
echo "done. session artifacts in: $SESSION_DIR"
