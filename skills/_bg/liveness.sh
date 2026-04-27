#!/usr/bin/env bash
# Shared helper: classify a background gremlin's liveness from its state.json.
# Sourced by session-summary.sh (hook); the on-demand /gremlins listing
# (pipeline/fleet.py) replicates the same classifier inline so its logic
# stays in lockstep. Keeping one bash source of truth means the two
# never disagree on whether a gremlin is running, dead, or stalled.
#
# Requires: jq on PATH. If jq is missing the caller has bigger problems; this
# file's functions will echo empty strings rather than fail loudly.

# Stall threshold in seconds. 45 min default — high enough that a slow
# planning stage doesn't flap, low enough that a truly wedged gremlin is
# caught within an hour. Tune via env.
: "${BG_STALL_SECS:=2700}"

# _liveness_file_mtime <path> — echoes seconds-since-epoch or empty.
# Portable across BSD (macOS) and GNU (Linux) stat.
_liveness_file_mtime() {
    local f="$1" m
    m=$(stat -f %m "$f" 2>/dev/null) || m=$(stat -c %Y "$f" 2>/dev/null) || m=""
    echo "$m"
}

# liveness_of_state_file <state_file_path>
# Echoes one of:
#   running
#   dead:<reason>
#   stalled:<reason>
# Never exits nonzero; empty output means the caller passed garbage.
liveness_of_state_file() {
    local sf="$1"
    [[ -f "$sf" ]] || return 0
    # Note: avoid `status` as a local name — it's a special/readonly in zsh,
    # and this file is intended to be sourced from either bash or zsh hooks.
    local wdir gr_status gr_pid gr_exit_code gr_bail_reason
    wdir=$(dirname "$sf")

    # US (\x1f) separator, matching session-summary.sh and pipeline/fleet.py:
    # bash treats tab as IFS-whitespace and collapses consecutive empty
    # columns, so a future 5th field could silently lose a value. US is
    # non-whitespace.
    IFS=$'\x1f' read -r gr_status gr_pid gr_exit_code gr_bail_reason < <(
        jq -r '[.status, (.pid // "" | tostring),
                (.exit_code // "" | tostring),
                (.bail_reason // "")] | join("\u001f")' "$sf" 2>/dev/null || true
    )

    # Terminal: finish.sh (or headless rescue's bail path) wrote the
    # `finished` marker. A bail_reason takes precedence over the generic
    # exit code so listings show *why* rescue gave up rather than just
    # "dead:exit 2".
    if [[ -f "$wdir/finished" ]]; then
        if [[ -n "$gr_bail_reason" ]]; then
            echo "dead:bailed:$gr_bail_reason"
        elif [[ -n "$gr_exit_code" && "$gr_exit_code" != "0" && "$gr_exit_code" != "null" ]]; then
            echo "dead:exit $gr_exit_code"
        else
            echo "dead:finished"
        fi
        return 0
    fi

    if [[ "$gr_status" == "running" ]]; then
        # PID gone but no finish marker → crashed silently.
        if [[ -n "$gr_pid" && "$gr_pid" != "null" ]] && ! kill -0 "$gr_pid" 2>/dev/null; then
            echo "dead:crashed (pid $gr_pid gone)"
            return 0
        fi

        # Stall heuristic: log file hasn't moved in BG_STALL_SECS.
        local log="$wdir/log" mtime now age
        if [[ -f "$log" ]]; then
            mtime=$(_liveness_file_mtime "$log")
            if [[ -n "$mtime" ]]; then
                now=$(date +%s)
                age=$(( now - mtime ))
                if (( age > BG_STALL_SECS )); then
                    echo "stalled:no log update $((age / 60))m"
                    return 0
                fi
            fi
        fi

        echo "running"
        return 0
    fi

    # Non-running status without a finished marker is unusual. Report it
    # literally so the user can see something is off.
    if [[ -n "$gr_exit_code" && "$gr_exit_code" != "0" && "$gr_exit_code" != "null" ]]; then
        echo "dead:exit $gr_exit_code"
    else
        echo "dead:${gr_status:-unknown}"
    fi
}
