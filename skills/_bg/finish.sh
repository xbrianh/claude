#!/usr/bin/env bash
# Terminal bookkeeping for a background workflow: invoked by launch.sh's spawned
# child once the pipeline exits. Touches a `finished` marker (always — that's
# the signal session-summary.sh watches for), then best-effort updates
# state.json with the final status/exit_code/ended_at. For ghimplement only,
# best-effort removes the git worktree afterwards — localimplement keeps its
# worktree and named branch so the user can inspect commits and artifacts.
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

# Always write the `finished` marker first. It's the one signal the
# session-summary hook actually checks; a failing jq update below must not
# leave the workflow wedged in "running" forever (and also suppresses the
# crashed-detection race: the marker precedes the visible status change).
touch "$STATE_DIR/finished"

STATE_TMP="$STATE_FILE.tmp"
(jq \
    --arg     status    "$STATUS" \
    --arg     ended_at  "$NOW_ISO" \
    --argjson exit_code "$EC" \
    '.status = $status | .ended_at = $ended_at | .exit_code = $exit_code' \
    "$STATE_FILE" > "$STATE_TMP" && mv "$STATE_TMP" "$STATE_FILE") || true

KIND=$(jq        -r '.kind         // empty' "$STATE_FILE" 2>/dev/null || true)
PROJECT_ROOT=$(jq -r '.project_root // empty' "$STATE_FILE" 2>/dev/null || true)
WORKDIR=$(jq     -r '.workdir      // empty' "$STATE_FILE" 2>/dev/null || true)
SETUP_KIND=$(jq  -r '.setup_kind   // empty' "$STATE_FILE" 2>/dev/null || true)

# Worktree cleanup: only for ghimplement (which has already pushed its branch).
# For localimplement we deliberately leave the worktree and its named branch
# in place so the user can inspect commits and .claude-workflow/<ts>/ artifacts.
if [[ "$KIND" != "localimplement" && "$SETUP_KIND" == "worktree" \
      && -n "$PROJECT_ROOT" && -n "$WORKDIR" ]]; then
    git -C "$PROJECT_ROOT" worktree remove --force "$WORKDIR" 2>/dev/null || true
    git -C "$PROJECT_ROOT" worktree prune                     2>/dev/null || true
fi
