#!/usr/bin/env bash
# Atomically patch the stage (and optional sub_stage) fields in a background
# workflow's state.json. Called by pipeline scripts at each stage boundary
# so `/workflows` and the session-summary hook can report where a workflow is.
#
# Usage: set-stage.sh <wf_id> <stage> [sub_stage_json]
#
# Fails silently on any error — stage bookkeeping must never break a running
# pipeline. Callers should also guard on [[ -n "${WF_ID:-}" ]] so direct
# invocations of the pipeline (outside the launcher) are no-ops.
set -u

WF_ID="${1:-}"
STAGE="${2:-}"
SUB_STAGE="${3:-}"

[[ -n "$WF_ID" && -n "$STAGE" ]] || exit 0

STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows/$WF_ID/state.json"
[[ -f "$STATE_FILE" ]] || exit 0

command -v jq >/dev/null 2>&1 || exit 0

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TMP="$STATE_FILE.stage.tmp.$$"

if [[ -n "$SUB_STAGE" ]]; then
    jq --arg stage "$STAGE" \
       --arg updated "$NOW_ISO" \
       --argjson sub "$SUB_STAGE" \
       '.stage = $stage | .sub_stage = $sub | .stage_updated_at = $updated' \
       "$STATE_FILE" > "$TMP" 2>/dev/null \
        && mv "$TMP" "$STATE_FILE" 2>/dev/null
else
    jq --arg stage "$STAGE" \
       --arg updated "$NOW_ISO" \
       '.stage = $stage | del(.sub_stage) | .stage_updated_at = $updated' \
       "$STATE_FILE" > "$TMP" 2>/dev/null \
        && mv "$TMP" "$STATE_FILE" 2>/dev/null
fi

rm -f "$TMP" 2>/dev/null || true
exit 0
