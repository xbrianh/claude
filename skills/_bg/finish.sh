#!/usr/bin/env bash
# Terminal bookkeeping for a background workflow: invoked by launch.sh's spawned
# child once the pipeline exits. Writes the final status/exit_code/ended_at into
# state.json atomically, touches a `finished` marker, and best-effort removes the
# git worktree so it doesn't linger in the parent repo's metadata.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[[ $# -eq 2 ]] || die "usage: finish.sh <workflow-id> <exit-code>"
WF_ID="$1"
EC="$2"

# Guard against weird exit codes (shouldn't happen — "$?" is always numeric —
# but --argjson will hard-fail on non-JSON input).
[[ "$EC" =~ ^-?[0-9]+$ ]] || EC=1

STATE_DIR="$HOME/.claude/workflows/$WF_ID"
STATE_FILE="$STATE_DIR/state.json"
[[ -f "$STATE_FILE" ]] || die "no state file at $STATE_FILE"

STATUS="stopped"
[[ "$EC" == "0" ]] && STATUS="done"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

STATE_TMP="$STATE_FILE.tmp"
jq \
    --arg     status    "$STATUS" \
    --arg     ended_at  "$NOW_ISO" \
    --argjson exit_code "$EC" \
    '.status = $status | .ended_at = $ended_at | .exit_code = $exit_code' \
    "$STATE_FILE" > "$STATE_TMP" && mv "$STATE_TMP" "$STATE_FILE"

touch "$STATE_DIR/finished"

PROJECT_ROOT=$(jq -r '.project_root // empty' "$STATE_FILE" 2>/dev/null || true)
WORKDIR=$(jq     -r '.workdir      // empty' "$STATE_FILE" 2>/dev/null || true)
SETUP_KIND=$(jq  -r '.setup_kind   // empty' "$STATE_FILE" 2>/dev/null || true)

if [[ "$SETUP_KIND" == "worktree" && -n "$PROJECT_ROOT" && -n "$WORKDIR" ]]; then
    git -C "$PROJECT_ROOT" worktree remove --force "$WORKDIR" 2>/dev/null || true
    git -C "$PROJECT_ROOT" worktree prune                     2>/dev/null || true
fi

exit 0
