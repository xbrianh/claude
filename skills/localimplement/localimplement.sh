#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Kill background reviewer subprocesses (and any other children) if the user
# Ctrl-C's a long run, so orphaned `claude -p` calls don't keep burning tokens
# after the script exits.
trap 'trap - INT TERM; kill -- -$$ 2>/dev/null; exit 130' INT TERM

die() { echo "error: $*" >&2; exit 1; }

# Stage helper: only meaningful when this script runs under the _bg launcher
# (which exports WF_ID and creates the workflow's state.json under
# ${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows/<WF_ID>/).
# A direct CLI invocation has no WF_ID and set_stage no-ops.
SET_STAGE_SH="$HOME/.claude/skills/_bg/set-stage.sh"
set_stage() {
  [[ -n "${WF_ID:-}" ]] || return 0
  [[ -x "$SET_STAGE_SH" ]] || return 0
  "$SET_STAGE_SH" "$WF_ID" "$@" >/dev/null 2>&1 || true
}

# Tee stream-json to a per-stage raw file under $SESSION_DIR while writing a
# human-readable trace of every meaningful event (init / assistant text /
# tool_use / tool_result / final result) to stderr. Under the _bg launcher
# stderr is redirected to the workflow's log under
# ${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows/<id>/log; the raw
# file is the diagnostic artifact that survives even when the trace is lost.
#
# Replaces an older `progress_tee` that used `tee >(jq ... >&2)` — the
# process-substitution stderr empirically didn't reach the log under _bg, so
# the prior trace was effectively a no-op. The synchronous `tee | jq`
# pipeline here keeps fd inheritance simple.
#
# The inner pipeline is terminated with `|| true` so a jq parse failure
# (e.g. on a truncated JSON line emitted by a crashing `claude -p`) cannot
# abort the caller's stage under `set -euo pipefail`. Logging is
# observational; the raw file is already flushed by `tee` regardless.
#
# Args: label (e.g. "plan" or "review-code:opus"; may be empty), raw_file path.
log_stream() {
  local label="${1:-}"
  local raw_file="${2:-}"
  [[ -n "$raw_file" ]] || die "log_stream: raw_file required"
  tee "$raw_file" | jq -r --unbuffered --arg label "$label" '
    def trunc: if (length // 0) > 200 then .[:200] + "..." else . end;
    ($label | if length > 0 then "[\(.)] " else "" end) as $prefix
    | if .type == "system" then
        select(.subtype == "init")
        | "\($prefix)init session=\(.session_id // "?") model=\(.model // "?") cwd=\(.cwd // "?")"
      elif .type == "assistant" then
        .message.content[]?
        | if .type == "text" then
            "\($prefix)text: \((.text // "") | gsub("\n"; " ") | trunc)"
          elif .type == "tool_use" then
            ((.input.file_path // .input.command // .input.pattern // .input.url // .input.output_file // "") | tostring | gsub("\n"; " ") | trunc) as $arg
            | "\($prefix)tool: \(.name) \($arg)"
          else empty end
      elif .type == "user" then
        .message.content[]?
        | select(.type == "tool_result")
        | (if .is_error == true then " ERROR" else "" end) as $err
        | (.content | (if type == "string" then . elif type == "array" then (map(.text? // "") | join(" ")) else tostring end) | gsub("\n"; " ") | trunc) as $body
        | "\($prefix)result\($err): \($body)"
      elif .type == "result" then
        "\($prefix)final: subtype=\(.subtype // "?") turns=\(.num_turns // "?") cost=\(.total_cost_usd // .cost_usd // "?")"
      else empty end
  ' >&2 || true
}

MODEL_PLAN="sonnet"
MODEL_IMPL="sonnet"
MODEL_ADDR="sonnet"
MODEL_A="sonnet"
MODEL_B="sonnet"
MODEL_C="sonnet"
USAGE="usage: localimplement.sh [-p <plan-model>] [-i <impl-model>] [-x <address-model>] [-a <holistic-review-model>] [-b <detail-review-model>] [-c <scope-review-model>] \"<instructions>\""
while getopts "p:i:x:a:b:c:" opt; do
  case "$opt" in
    p) MODEL_PLAN="$OPTARG" ;;
    i) MODEL_IMPL="$OPTARG" ;;
    x) MODEL_ADDR="$OPTARG" ;;
    a) MODEL_A="$OPTARG" ;;
    b) MODEL_B="$OPTARG" ;;
    c) MODEL_C="$OPTARG" ;;
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
for m in "$MODEL_PLAN" "$MODEL_IMPL" "$MODEL_ADDR" "$MODEL_A" "$MODEL_B" "$MODEL_C"; do
  [[ "$m" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid model: $m"
done

command -v claude >/dev/null || die "claude CLI not found"
command -v jq >/dev/null     || die "jq not found"

# Session directory. Artifacts always live under the XDG state root so they
# stay out of the product branch and survive worktree removal. Under the _bg
# launcher WF_ID names the workflow dir directly under $STATE_ROOT, and the
# launcher/session-summary hook manage its lifecycle (state.json, finished,
# acknowledged markers, 14-day prune). Direct CLI invocations have no such
# lifecycle, so they're nested under `$STATE_ROOT/direct/<ts>-<rand>/` — a
# dedicated subdir keeps them visually separated from real workflow state
# and lets `session-summary.sh` prune them on a simpler age-based heuristic.
#
# WF_ID is interpolated into a filesystem path, so validate it against a
# conservative charset to prevent path traversal (e.g. "../") or embedded
# slashes when it's set externally. The _bg launcher produces IDs matching
# ^[a-z0-9-]+$, so this pattern is a strict superset.
STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows"
TS=$(date +%Y%m%d-%H%M%S)
if [[ -n "${WF_ID:-}" ]]; then
  [[ "$WF_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid WF_ID: $WF_ID"
  SESSION_DIR="$STATE_ROOT/$WF_ID/artifacts"
else
  RAND=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c 6 || true)
  [[ -n "$RAND" ]] || RAND="xxxxxx"
  SESSION_DIR="$STATE_ROOT/direct/$TS-$RAND/artifacts"
fi
mkdir -p "$SESSION_DIR"
PLAN_FILE="$SESSION_DIR/plan.md"
REVIEW_CODE_A="$SESSION_DIR/review-code-holistic-$MODEL_A.md"
REVIEW_CODE_B="$SESSION_DIR/review-code-detail-$MODEL_B.md"
REVIEW_CODE_C="$SESSION_DIR/review-code-scope-$MODEL_C.md"

echo "==> session: $SESSION_DIR"

CLAUDE_FLAGS=(--permission-mode bypassPermissions --output-format stream-json --verbose)

IN_GIT=0
git rev-parse --git-dir >/dev/null 2>&1 && IN_GIT=1

# Reviewer focuses. Two complementary lenses (holistic / detail) are told to
# stay out of each other's lane so the two code reviews are complementary, not
# redundant. The lens prose lives in sibling files so prompt edits don't churn
# this script.

for lens in lens-holistic-code.md lens-detail-code.md lens-scope-code.md; do
  [[ -s "$SCRIPT_DIR/$lens" ]] || die "missing or empty lens file: $SCRIPT_DIR/$lens"
done
FOCUS_CODE_A=$(cat "$SCRIPT_DIR/lens-holistic-code.md")
FOCUS_CODE_B=$(cat "$SCRIPT_DIR/lens-detail-code.md")
FOCUS_CODE_C=$(cat "$SCRIPT_DIR/lens-scope-code.md")

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

# Run three reviewers in parallel with the same context but different lenses,
# then validate that all three produced non-empty output.
#
# The `( ... ) &` subshell wrapping is deliberate: without it, `$!` captures
# the PID of `log_stream` (the last stage of the pipeline), and `log_stream`
# exits 0 whenever its input closes — so a non-zero exit from `claude -p`
# would be silently swallowed. Wrapping in a subshell makes `$!` the
# subshell PID, whose exit code reflects the pipeline under `set -e pipefail`
# (any non-zero stage fails the pipeline, `set -e` then exits the subshell).
# The trailing `exit "${PIPESTATUS[0]}"` is a defensive belt-and-braces for
# callers running without `set -e`; under the current options it is only
# reached on the success path, where PIPESTATUS[0] is 0.
run_triple_review() {
  local context="$1" focus_a="$2" focus_b="$3" focus_c="$4" out_a="$5" out_b="$6" out_c="$7" where_field="$8"
  ( run_review "$MODEL_A" "$out_a" "$focus_a" "$context" "$where_field" | log_stream "review-code:$MODEL_A" "$SESSION_DIR/stream-review-code-$MODEL_A.jsonl"; exit "${PIPESTATUS[0]}" ) &
  local pid_a=$!
  ( run_review "$MODEL_B" "$out_b" "$focus_b" "$context" "$where_field" | log_stream "review-code:$MODEL_B" "$SESSION_DIR/stream-review-code-$MODEL_B.jsonl"; exit "${PIPESTATUS[0]}" ) &
  local pid_b=$!
  ( run_review "$MODEL_C" "$out_c" "$focus_c" "$context" "$where_field" | log_stream "review-code:$MODEL_C" "$SESSION_DIR/stream-review-code-$MODEL_C.jsonl"; exit "${PIPESTATUS[0]}" ) &
  local pid_c=$!

  # Inner function names leak to the global namespace in bash — prefix with
  # the outer function name so a grep for `emit_sub_stage` doesn't land here
  # and so redefinitions from anywhere else can't collide.
  # Use stable labels "holistic"/"detail"/"scope" as keys (not model names) so
  # the JSON object stays unambiguous even when models share the same value.
  # Include the model name inside each value so it's visible in status output.
  _run_triple_review_emit_sub() {
    set_stage review-code "$(jq -cn \
        --arg am "$MODEL_A" --arg bm "$MODEL_B" --arg cm "$MODEL_C" \
        --arg as "$1"       --arg bs "$2"       --arg cs "$3" \
        '{"holistic": "\($as) (\($am))", "detail": "\($bs) (\($bm))", "scope": "\($cs) (\($cm))"}')"
  }

  local fail=0 a_status="running" b_status="running" c_status="running"
  _run_triple_review_emit_sub "$a_status" "$b_status" "$c_status"

  # Poll: whenever a reviewer process exits, harvest its exit code and emit a
  # sub-stage update so the status command can show mid-flight progress.
  # Correctness depends on bash auto-reaping backgrounded children in
  # non-interactive script mode, so `kill -0 $pid` on an exited child returns
  # ESRCH (we treat that as "exited") rather than succeeding against a zombie.
  while [[ "$a_status" == "running" || "$b_status" == "running" || "$c_status" == "running" ]]; do
    if [[ "$a_status" == "running" ]] && ! kill -0 "$pid_a" 2>/dev/null; then
      wait "$pid_a" || { echo "review $MODEL_A failed" >&2; fail=1; }
      a_status="done"
      _run_triple_review_emit_sub "$a_status" "$b_status" "$c_status"
    fi
    if [[ "$b_status" == "running" ]] && ! kill -0 "$pid_b" 2>/dev/null; then
      wait "$pid_b" || { echo "review $MODEL_B failed" >&2; fail=1; }
      b_status="done"
      _run_triple_review_emit_sub "$a_status" "$b_status" "$c_status"
    fi
    if [[ "$c_status" == "running" ]] && ! kill -0 "$pid_c" 2>/dev/null; then
      wait "$pid_c" || { echo "review $MODEL_C failed" >&2; fail=1; }
      c_status="done"
      _run_triple_review_emit_sub "$a_status" "$b_status" "$c_status"
    fi
    [[ "$a_status" == "running" || "$b_status" == "running" || "$c_status" == "running" ]] && sleep 2
  done

  [[ $fail -eq 0 ]] || die "one or more reviews failed"
  [[ -s "$out_a" ]] || die "review $MODEL_A did not produce $out_a"
  [[ -s "$out_b" ]] || die "review $MODEL_B did not produce $out_b"
  [[ -s "$out_c" ]] || die "review $MODEL_C did not produce $out_c"
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
  | log_stream "plan" "$SESSION_DIR/stream-plan.jsonl"
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
# `$PLAN_FILE` — the latter is an absolute user-specific path under the XDG
# state root that would end up in git history otherwise.
[[ $IN_GIT -eq 1 ]] && IMPL_COMMIT_INSTR=", stage the changed files by name and create a single git commit with a clear message that references the implementation plan (refer to it as \`plan.md\` in the commit message, not by absolute path). Do NOT create any meta/scaffolding files in the repo — no \`.claude-workflow/\` directory, no \`plan.md\`, no review docs, no notes-to-self. Do not push."

claude -p --model "$MODEL_IMPL" "${CLAUDE_FLAGS[@]}" \
  "Read the implementation plan at \`$PLAN_FILE\` and implement every task in it by editing code in this repo. When the implementation is complete${IMPL_COMMIT_INSTR}" \
  | log_stream "implement" "$SESSION_DIR/stream-implement.jsonl"

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
echo "==> [3/4] reviewing code in parallel (models: $MODEL_A, $MODEL_B, $MODEL_C)"

CODE_SCOPE=""
if [[ $IN_GIT -eq 1 ]]; then
  CODE_SCOPE="Review the changes introduced by the most recent commit (HEAD vs HEAD~1) plus any uncommitted working-tree changes. Use \`git diff HEAD~1 HEAD\` and \`git diff\` to see the scope."
else
  CODE_SCOPE="Review the uncommitted changes in this directory (\`git diff\` if available, otherwise inspect recently modified files)."
fi
CODE_REVIEW_CONTEXT="You are reviewing an implementation of the plan at \`$PLAN_FILE\`. Read the plan first for context.

$CODE_SCOPE"
run_triple_review "$CODE_REVIEW_CONTEXT" "$FOCUS_CODE_A" "$FOCUS_CODE_B" "$FOCUS_CODE_C" "$REVIEW_CODE_A" "$REVIEW_CODE_B" "$REVIEW_CODE_C" "**File:** \`path/to/file.ext:<line>\`"
echo "    holistic code review ($MODEL_A): $REVIEW_CODE_A"
echo "    detail code review   ($MODEL_B): $REVIEW_CODE_B"
echo "    scope code review    ($MODEL_C): $REVIEW_CODE_C"

set_stage address-code
echo "==> [4/4] addressing code reviews (model: $MODEL_ADDR)"
ADDRESS_COMMIT_INSTR=""
[[ $IN_GIT -eq 1 ]] && ADDRESS_COMMIT_INSTR="After making all fixes, stage the changed files by name and create a single git commit titled 'Address review feedback' whose body references all three review files. Do not push."

claude -p --model "$MODEL_ADDR" "${CLAUDE_FLAGS[@]}" \
  "Three independent code reviews of the most recent implementation are at:
- \`$REVIEW_CODE_A\` — **holistic** reviewer (model: $MODEL_A).
- \`$REVIEW_CODE_B\` — **detail** reviewer (model: $MODEL_B).
- \`$REVIEW_CODE_C\` — **scope** reviewer (model: $MODEL_C).

Read all three reviews. The reviewers have different lenses by design, so their findings will mostly be complementary rather than overlapping — still deduplicate where they do overlap. For every actionable finding you agree with, make the fix in the code. For findings you disagree with or choose to skip, note them briefly in your final summary with a reason.

$ADDRESS_COMMIT_INSTR

End with a short summary (to stdout) of: what you addressed, what you skipped and why." \
  | log_stream "address-code" "$SESSION_DIR/stream-address.jsonl"

echo ""
echo "done. session artifacts in: $SESSION_DIR"
