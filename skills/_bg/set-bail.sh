#!/usr/bin/env bash
# Atomically write bail_class (and optional bail_detail) to a background
# gremlin's state.json. Stages — both shell (ghgremlin.sh, /ghreview,
# /ghaddress) and python (localgremlin.py) — call this when they decline
# to proceed so /gremlins rescue --headless can decide whether to attempt
# recovery.
#
# Usage: set-bail.sh <gr_id> <bail_class> [bail_detail]
#
# Vocabulary for bail_class:
#   reviewer_requested_changes  — code review flagged blocker findings
#   security                    — review flagged security concern(s)
#   secrets                     — change touches secrets/credentials
#   other                       — generic; pair with a useful bail_detail
#
# Headless rescue refuses to run for the first three classes. `other` is
# attempted with the full diagnose-and-fix path.
#
# Fails silently on any error: writing the marker must never break the
# stage that's bailing.
set -u

GR_ID="${1:-}"
BAIL_CLASS="${2:-}"
BAIL_DETAIL="${3:-}"

[[ -n "$GR_ID" && -n "$BAIL_CLASS" ]] || exit 0

STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins/$GR_ID/state.json"
[[ -f "$STATE_FILE" ]] || exit 0

command -v jq >/dev/null 2>&1 || exit 0

TMP="$STATE_FILE.bail.tmp.$$"
if jq --arg cls "$BAIL_CLASS" \
      --arg det "$BAIL_DETAIL" \
      '.bail_class = $cls
       | (if $det == "" then . else .bail_detail = $det end)' \
      "$STATE_FILE" > "$TMP" 2>/dev/null; then
    mv "$TMP" "$STATE_FILE" 2>/dev/null || rm -f "$TMP" 2>/dev/null
else
    rm -f "$TMP" 2>/dev/null
fi
exit 0
