#!/usr/bin/env bash
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

progress_tee() {
  tee >(jq -r --unbuffered '
    select(.type=="assistant") | .message.content[]?
    | select(.type=="tool_use")
    | "    · \(.name) \(.input.file_path // .input.command // .input.pattern // "")"
  ' 2>/dev/null >&2)
}

REF=""
while getopts "r:" opt; do
  case "$opt" in
    r) REF="$OPTARG" ;;
    *) die "usage: ghimplement.sh [-r <ref>] \"<instructions>\"" ;;
  esac
done
shift $((OPTIND - 1))
[[ $# -ge 1 ]] || die "usage: ghimplement.sh [-r <ref>] \"<instructions>\""
INSTRUCTIONS="$*"

command -v claude >/dev/null || die "claude CLI not found"
command -v gh >/dev/null     || die "gh CLI not found"
command -v jq >/dev/null     || die "jq not found"

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) \
  || die "not in a gh-recognized repo"

CLAUDE_FLAGS=(--permission-mode bypassPermissions --output-format stream-json --verbose)

extract_url() {
  local out="$1" pattern="$2" label="$3"
  local result url
  result=$(jq -r 'select(.type=="result") | .result' <<<"$out" | tail -1)
  url=$(grep -oE "$pattern" <<<"$result" | tail -1 || true)
  if [[ -z "$url" ]]; then
    echo "--- raw claude output ($label) ---" >&2
    echo "$out" >&2
    echo "--- end raw output ---" >&2
    die "failed to extract $label URL"
  fi
  echo "$url"
}

echo "==> [1/6] running /ghplan"
PLAN_OUT=$(claude -p "${CLAUDE_FLAGS[@]}" "/ghplan ${REF} ${INSTRUCTIONS}" | progress_tee)
ISSUE_URL=$(extract_url "$PLAN_OUT" 'https://github\.com/[^ )]+/issues/[0-9]+' "issue")
ISSUE_NUM=$(basename "$ISSUE_URL")
echo "    issue: $ISSUE_URL"

echo "==> [2a/6] implementing plan"
claude -p "${CLAUDE_FLAGS[@]}" \
  "Implement the plan in GitHub issue $ISSUE_URL. Read the issue body for the full plan, then make the code changes in this repo. Do not commit or push yet." \
  | progress_tee >/dev/null

echo "==> [2b/6] committing + opening PR"
PR_OUT=$(claude -p "${CLAUDE_FLAGS[@]}" --continue \
  "Now create a new branch named 'issue-${ISSUE_NUM}-<short-slug>' from the default branch, commit the changes with a descriptive message ending in 'Closes #${ISSUE_NUM}', push the branch, and open a PR with 'gh pr create' whose body contains 'Closes #${ISSUE_NUM}'. Print ONLY the PR URL on the final line of your response." | progress_tee)
PR_URL=$(extract_url "$PR_OUT" 'https://github\.com/[^ )]+/pull/[0-9]+' "PR")
PR_NUM=$(basename "$PR_URL")
echo "    PR: $PR_URL"

echo "==> [3/6] requesting Copilot review"
gh pr edit "$PR_NUM" --repo "$REPO" --add-reviewer copilot-pull-request-reviewer >/dev/null \
  || die "could not request Copilot review (is it enabled in repo settings?)"

echo "==> [4/6] running /ghreview"
claude -p "${CLAUDE_FLAGS[@]}" "/ghreview $PR_URL" | progress_tee >/dev/null

echo "==> [5/6] waiting for Copilot review (20s interval, 10min timeout)"
deadline=$(( $(date +%s) + 600 ))
while true; do
  state=$(gh api "repos/$REPO/pulls/$PR_NUM/reviews" \
    --jq '.[] | select(.user.login | test("[Cc]opilot")) | .state' 2>/dev/null \
    | grep -E 'COMMENTED|CHANGES_REQUESTED|APPROVED' | head -1 || true)
  if [[ -n "$state" ]]; then
    echo "    Copilot review: $state"
    break
  fi
  [[ $(date +%s) -lt $deadline ]] || die "Copilot review timed out after 10 min"
  sleep 20
done

echo "==> [6/6] running /ghaddress"
claude -p "${CLAUDE_FLAGS[@]}" "/ghaddress $PR_URL" | progress_tee >/dev/null

echo ""
echo "done. PR: $PR_URL"
