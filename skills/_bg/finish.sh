#!/usr/bin/env bash
# Terminal bookkeeping for a background workflow: invoked by launch.sh's spawned
# child once the pipeline exits. Touches a `finished` marker (always — that's
# the signal session-summary.sh watches for), then best-effort updates
# state.json with the final status/exit_code/ended_at. On success (EC == 0),
# best-effort removes the git worktree for both ghimplement and localimplement.
# On failure the worktree is preserved so the user can debug. The branch is
# always preserved — refs outlive `git worktree remove`.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[[ $# -eq 2 ]] || die "usage: finish.sh <workflow-id> <exit-code>"
WF_ID="$1"
EC="$2"

# Guard against weird exit codes (shouldn't happen — "$?" is always numeric —
# but --argjson will hard-fail on non-JSON input).
[[ "$EC" =~ ^-?[0-9]+$ ]] || EC=1

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows/$WF_ID"
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

# Worktree cleanup: on success only, for both setup kinds. ghimplement
# (SETUP_KIND=worktree) has already pushed its branch; localimplement
# (SETUP_KIND=worktree-branch) has committed code changes to its branch, and
# its plan/review artifacts live under $STATE_DIR/artifacts/ — outside the
# worktree, so they survive removal independently.
# On failure (EC != 0) we leave the worktree in place for debugging.
if [[ "$EC" == "0" \
      && ( "$SETUP_KIND" == "worktree" || "$SETUP_KIND" == "worktree-branch" ) \
      && -n "$PROJECT_ROOT" && -n "$WORKDIR" ]]; then
    git -C "$PROJECT_ROOT" worktree remove --force "$WORKDIR" 2>/dev/null || true
    git -C "$PROJECT_ROOT" worktree prune                     2>/dev/null || true
fi
