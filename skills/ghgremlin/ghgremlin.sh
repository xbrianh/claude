#!/usr/bin/env bash
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Stage helper: only meaningful when invoked via the _bg launcher (GR_ID set).
SET_STAGE_SH="$HOME/.claude/skills/_bg/set-stage.sh"
set_stage() {
  [[ -n "${GR_ID:-}" ]] || return 0
  [[ -x "$SET_STAGE_SH" ]] || return 0
  "$SET_STAGE_SH" "$GR_ID" "$@" >/dev/null 2>&1 || true
}

# Tee stream-json to stdout while printing a live progress trace of tool_use
# events to stderr.
progress_tee() {
  tee >(jq -r --unbuffered '
    select(.type=="assistant") | .message.content[]?
    | select(.type=="tool_use")
    | "    · \(.name) \(.input.file_path // .input.command // .input.pattern // "")"
  ' 2>/dev/null >&2)
}

REF=""
RESUME_FROM=""
USAGE='usage: ghgremlin.sh [-r <ref>] [--resume-from <stage>] "<instructions>"'
while [[ $# -gt 0 ]]; do
  case "$1" in
    -r)
      [[ $# -ge 2 ]] || die "$USAGE"
      REF="$2"; shift 2 ;;
    --resume-from)
      [[ $# -ge 2 ]] || die "$USAGE"
      RESUME_FROM="$2"; shift 2 ;;
    --) shift; break ;;
    -*) die "unknown flag: $1" ;;
    *) break ;;
  esac
done
[[ $# -ge 1 ]] || die "$USAGE"
INSTRUCTIONS="$*"

# Validate REF against a conservative git-ref charset. INSTRUCTIONS is
# intentionally *not* sanitized: it is the prompt this tool exists to send.
# Callers should treat INSTRUCTIONS as a prompt to an unrestricted agent
# (we run with --permission-mode bypassPermissions below), not an opaque arg.
if [[ -n "$REF" && ! "$REF" =~ ^[A-Za-z0-9._/#-]+$ ]]; then
  die "invalid -r ref: $REF (allowed: A-Z a-z 0-9 . _ / # -)"
fi

# Stage ordering drives --resume-from: a stage index < target stage's index is
# skipped; rehydration happens inline in each skip branch.
STAGES=(plan implement commit-pr request-copilot ghreview wait-copilot ghaddress)
if [[ -n "$RESUME_FROM" ]]; then
  _found=0
  for _s in "${STAGES[@]}"; do [[ "$_s" == "$RESUME_FROM" ]] && _found=1; done
  [[ $_found -eq 1 ]] || die "invalid --resume-from: $RESUME_FROM (allowed: ${STAGES[*]})"
  # commit-pr resume needs IMPL_SESSION (--resume target) which we don't
  # persist. Rewind to `implement` so the commit-pr stage can run against a
  # fresh session. Persisting impl_session_id is deferred — revisit if
  # rewinding a full implement proves painful in practice.
  if [[ "$RESUME_FROM" == "commit-pr" ]]; then
    echo "    note: --resume-from commit-pr requires IMPL_SESSION which isn't persisted; rewinding to implement" >&2
    RESUME_FROM="implement"
  fi
fi

_stage_idx() {
  local target="$1" i=0
  for s in "${STAGES[@]}"; do
    [[ "$s" == "$target" ]] && { echo "$i"; return 0; }
    i=$((i + 1))
  done
  echo -1
}

# run_stage <stage> — 0 (run) when no --resume-from, or when this stage's
# index is at or past the resume target. Else 1 (skip).
run_stage() {
  [[ -z "$RESUME_FROM" ]] && return 0
  local cur tgt
  cur=$(_stage_idx "$1")
  tgt=$(_stage_idx "$RESUME_FROM")
  (( cur >= tgt ))
}

STATE_FILE=""
if [[ -n "${GR_ID:-}" ]]; then
  STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins/$GR_ID/state.json"
fi

# patch_state <jq-filter> [--arg k v]... — atomically update state.json.
# No-ops when not running under a gremlin (no GR_ID), so direct invocations
# work unchanged.
patch_state() {
  [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]] || return 0
  local filter="$1"; shift
  local tmp="${STATE_FILE}.tmp.$$"
  if jq "$@" "$filter" "$STATE_FILE" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$STATE_FILE"
  else
    rm -f "$tmp"
    echo "warning: could not patch state.json" >&2
  fi
}

command -v claude >/dev/null || die "claude CLI not found"
command -v gh >/dev/null     || die "gh CLI not found"
command -v jq >/dev/null     || die "jq not found"

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) \
  || die "not in a gh-recognized repo"

CLAUDE_FLAGS=(--permission-mode bypassPermissions --output-format stream-json --verbose)
# Stages run sequentially; the wait-copilot sleep 20 loop should not be extended beyond ~5 min intervals or the Anthropic prompt cache TTL expires and cache benefits between the review and address stages are lost.

# Extract a URL from a Bash-tool_result event matching a regex, preferring
# the most recent match. Scans stream-json tool_use/tool_result pairs for
# `gh issue create` / `gh pr create` invocations, falling back to the final
# result text only if no such tool call is found. This is much more robust
# than scraping the whole result for a URL regex, which could pick up a
# linked ticket or unrelated reference.
#
# Args:
#   $1 stream-json output
#   $2 regex pattern for the URL
#   $3 regex pattern matching the gh command (e.g. 'gh issue create|gh pr create')
#   $4 human label for error messages
extract_gh_url() {
  local out="$1" url_pat="$2" cmd_pat="$3" label="$4"
  local tool_result_url result_url url

  # Find Bash tool_use events whose command matches cmd_pat, pair them with
  # their tool_result by tool_use_id, and grep the URL out of the tool_result
  # content. This is much more precise than scanning the entire final message
  # for a URL regex.
  tool_result_url=$(jq -rn --arg cmd_pat "$cmd_pat" '
    [ inputs ] as $events
    | ($events
       | map(select(.type=="assistant") | .message.content[]?
             | select(.type=="tool_use" and .name=="Bash"
                      and ((.input.command // "") | test($cmd_pat))))
       | map(.id)) as $ids
    | $events
    | map(select(.type=="user") | .message.content[]?
          | select(.type=="tool_result" and ((.tool_use_id // "") as $tid | $ids | index($tid))))
    | map(if (.content|type)=="array"
          then (.content | map(.text // "") | join("\n"))
          else (.content // "" | tostring) end)
    | .[]
  ' <<<"$out" 2>/dev/null | grep -oE "$url_pat" | tail -1 || true)

  if [[ -n "$tool_result_url" ]]; then
    echo "$tool_result_url"
    return 0
  fi

  # Fallback: scan the final result message for a URL matching the pattern.
  # Prefer the last match under the assumption that the model prints the
  # final URL at the end of its response.
  result_url=$(jq -r 'select(.type=="result") | .result' <<<"$out" | tail -1)
  url=$(grep -oE "$url_pat" <<<"$result_url" | tail -1 || true)
  if [[ -z "$url" ]]; then
    echo "--- raw claude output ($label) ---" >&2
    echo "$out" >&2
    echo "--- end raw output ---" >&2
    die "failed to extract $label URL"
  fi
  echo "$url"
}

# Extract the session_id from the stream-json `init` system event so later
# stages can resume the exact same session explicitly, instead of relying
# on `claude --continue` (which picks the most-recent session on the box
# and breaks under parallel invocation).
extract_session_id() {
  local out="$1" sid
  sid=$(jq -r 'select(.type=="system" and .subtype=="init") | .session_id' <<<"$out" \
        | head -1 || true)
  [[ -n "$sid" && "$sid" != "null" ]] || die "could not extract session_id from stream-json"
  echo "$sid"
}

if run_stage plan; then
  set_stage plan
  echo "==> [1/6] running /ghplan"
  PLAN_OUT=$(claude -p "${CLAUDE_FLAGS[@]}" "/ghplan ${REF:+$REF }${INSTRUCTIONS}" | progress_tee)
  ISSUE_URL=$(extract_gh_url "$PLAN_OUT" \
    'https://github\.com/[^ )]+/issues/[0-9]+' \
    'gh issue create' \
    "issue")
  ISSUE_NUM=$(basename "$ISSUE_URL")
  echo "    issue: $ISSUE_URL"
  # Persist issue_url so --resume-from implement (or later) can rehydrate
  # ISSUE_URL without re-running /ghplan.
  patch_state '.issue_url = $url' --arg url "$ISSUE_URL"
else
  [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]] \
    || die "--resume-from $RESUME_FROM requires GR_ID and state.json"
  ISSUE_URL=$(jq -r '.issue_url // ""' "$STATE_FILE")
  [[ -n "$ISSUE_URL" ]] \
    || die "--resume-from $RESUME_FROM: no issue_url in state.json (rewind to plan?)"
  ISSUE_NUM=$(basename "$ISSUE_URL")
  echo "    resumed issue: $ISSUE_URL"
fi
ISSUE_BODY=$(gh issue view "$ISSUE_NUM" --repo "$REPO" --json body --jq .body)
[[ -n "$ISSUE_BODY" ]] || die "issue $ISSUE_NUM has an empty body"

PRAGMATIC_DEV_FILE="$SCRIPT_DIR/../../agents/pragmatic-developer.md"
[[ -f "$PRAGMATIC_DEV_FILE" ]] || die "missing agent file: $PRAGMATIC_DEV_FILE"
# CORE_PRINCIPLES must not contain shell metacharacters (backticks, $(...), etc.)
# — safe as long as pragmatic-developer.md contains only prose and markdown.
CORE_PRINCIPLES=$(awk '/^## Core Principles/{found=1; next} found && /^## /{found=0} found{print}' "$PRAGMATIC_DEV_FILE")
[[ -n "$CORE_PRINCIPLES" ]] || die "could not find '## Core Principles' section in pragmatic-developer.md"

if run_stage implement; then
  # Record state before 2a so we can verify the model actually did work.
  PRE_HEAD=$(git rev-parse HEAD)

  set_stage implement
  echo "==> [2a/6] implementing plan"
  IMPL_OUT=$(claude -p "${CLAUDE_FLAGS[@]}" \
    "When writing code, follow these principles:

${CORE_PRINCIPLES}

The following is the implementation plan from the GitHub issue:

${ISSUE_BODY}

Implement the plan above by making the code changes in this repo. Do not commit or push yet. Do NOT create any meta/scaffolding files in the repo — no \`.claude-workflow/\` directory, no \`plan.md\`, no review docs, no notes-to-self. The plan lives in the GitHub issue and reviews go to PR comments; the only changes in this working tree should be product code." \
    | progress_tee)
  IMPL_SESSION=$(extract_session_id "$IMPL_OUT")

  # Guard: 2a should have produced uncommitted changes and should NOT have
  # advanced HEAD. If either invariant is broken, bail out now rather than
  # letting 2b open an empty PR or silently drop work.
  if [[ "$(git rev-parse HEAD)" != "$PRE_HEAD" ]]; then
    die "implementation step advanced HEAD (expected uncommitted changes only); refusing to continue"
  fi
  if [[ -z "$(git status --porcelain)" ]]; then
    die "implementation step produced no changes; refusing to open empty PR"
  fi
fi

if run_stage commit-pr; then
  set_stage commit-pr
  echo "==> [2b/6] committing + opening PR"
  # The --resume "$IMPL_SESSION" pairing means commit-pr only runs in the same
  # pipeline invocation as its implement stage. Resumes that start here are
  # rewound to implement above.
  PR_OUT=$(claude -p "${CLAUDE_FLAGS[@]}" --resume "$IMPL_SESSION" \
    "Create a new branch from the default branch, commit all changes with a descriptive message, push the branch, and open a PR with 'gh pr create'. Name the branch 'issue-${ISSUE_NUM}-<short-slug>', end the commit message with 'Closes #${ISSUE_NUM}', and include 'Closes #${ISSUE_NUM}' in the PR body. Print ONLY the PR URL on the final line of your response." | progress_tee)
  PR_URL=$(extract_gh_url "$PR_OUT" \
    'https://github\.com/[^ )]+/pull/[0-9]+' \
    'gh pr create' \
    "PR")
  PR_NUM=$(basename "$PR_URL")
  echo "    PR: $PR_URL"
  patch_state '.pr_url = $url' --arg url "$PR_URL"
else
  [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]] \
    || die "--resume-from $RESUME_FROM requires GR_ID and state.json"
  PR_URL=$(jq -r '.pr_url // ""' "$STATE_FILE")
  [[ -n "$PR_URL" ]] \
    || die "--resume-from $RESUME_FROM: no pr_url in state.json (rewind to implement?)"
  PR_NUM=$(basename "$PR_URL")
  echo "    resumed PR: $PR_URL"
fi

if run_stage request-copilot; then
  set_stage request-copilot
  echo "==> [3/6] requesting Copilot review"
  gh pr edit "$PR_NUM" --repo "$REPO" --add-reviewer copilot-pull-request-reviewer >/dev/null \
    || die "could not request Copilot review (is it enabled in repo settings?)"
fi

if run_stage ghreview; then
  set_stage ghreview
  echo "==> [4/6] running reviews in parallel (/ghreview + scope)"
  SCOPE_ISSUE_BODY="$ISSUE_BODY"
  SCOPE_PR_DIFF=$(gh pr diff "$PR_URL")
  ( claude -p "${CLAUDE_FLAGS[@]}" "/ghreview $PR_URL" | progress_tee >/dev/null ) &
  pid_ghreview=$!

  [[ -s "$SCRIPT_DIR/../localgremlin/lens-scope-code.md" ]] || die "missing or empty lens file: lens-scope-code.md"
  SCOPE_LENS=$(cat "$SCRIPT_DIR/../localgremlin/lens-scope-code.md")
  SCOPE_REVIEW_TMP=$(mktemp /tmp/scope-review-XXXXXX.md)
  ( claude -p "${CLAUDE_FLAGS[@]}" \
    "You are a scope reviewer for a pull request. Your task is to assess whether the diff is the right size and shape for the plan.

Lens:
$SCOPE_LENS

Implementation plan (from the GitHub issue):

$SCOPE_ISSUE_BODY

PR diff:

$SCOPE_PR_DIFF

Apply the scope lens above to the diff vs the plan. Write your findings to $SCOPE_REVIEW_TMP, then post them via: \`gh pr review $PR_URL --comment --body-file $SCOPE_REVIEW_TMP\`.

If the diff is scoped correctly, write exactly: 'Scoped correctly — nothing to flag.' to $SCOPE_REVIEW_TMP, then post it." \
    | progress_tee >/dev/null ) &
  pid_scope=$!

  wait "$pid_ghreview" || { kill "$pid_scope" 2>/dev/null; wait "$pid_scope" 2>/dev/null; die "/ghreview failed"; }
  wait "$pid_scope"    || die "scope reviewer failed"
fi

if run_stage wait-copilot; then
  set_stage wait-copilot
  echo "==> [5/6] waiting for Copilot review (20s interval, 10min timeout)"
  deadline=$(( $(date +%s) + 600 ))
  while true; do
    # Treat any non-empty, non-PENDING review state as terminal. GitHub
    # documents COMMENTED / CHANGES_REQUESTED / APPROVED / DISMISSED today,
    # and may add more; enumerating them exactly would make us wait out the
    # full timeout on any new state even though the review is already done.
    state=$(gh api "repos/$REPO/pulls/$PR_NUM/reviews" \
      --jq '.[] | select(.user.login | test("[Cc]opilot")) | .state' 2>/dev/null \
      | grep -vE '^(PENDING)?$' | head -1 || true)
    if [[ -n "$state" ]]; then
      echo "    Copilot review: $state"
      break
    fi
    [[ $(date +%s) -lt $deadline ]] || die "Copilot review timed out after 10 min"
    sleep 20
  done
fi

if run_stage ghaddress; then
  set_stage ghaddress
  echo "==> [6/6] running /ghaddress"
  claude -p "${CLAUDE_FLAGS[@]}" "/ghaddress $PR_URL" | progress_tee >/dev/null
fi

echo ""
echo "done. PR: $PR_URL"
