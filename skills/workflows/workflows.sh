#!/usr/bin/env bash
# /workflows — on-demand status of background workflow pipelines.
# Reads every ~/.claude/workflows/<id>/state.json, applies the shared liveness
# classifier, and prints one scannable line per workflow.
#
# Exit 0 always: an unexpected error logs to stderr and falls through. Same
# "never break a session" principle as the session-summary hook.
set -u

STATE_ROOT="$HOME/.claude/workflows"

command -v jq >/dev/null 2>&1 || { echo "jq not found" >&2; exit 0; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Try script-relative first (running out of the repo worktree), then the
# installed ~/.claude path. Keep going if neither exists — we'll degrade
# to a minimal "can't classify" rendering.
LIVENESS_LIB=""
for p in \
    "$SCRIPT_DIR/../_bg/liveness.sh" \
    "$HOME/.claude/skills/_bg/liveness.sh"; do
    if [[ -f "$p" ]]; then
        LIVENESS_LIB="$p"
        break
    fi
done
if [[ -n "$LIVENESS_LIB" ]]; then
    # shellcheck disable=SC1090
    source "$LIVENESS_LIB"
else
    liveness_of_state_file() { echo "unknown"; }
fi

MODE="list"
TARGET=""
HERE_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)     MODE="list"; shift ;;
        --here)    HERE_ONLY=1; shift ;;
        --ack)     MODE="ack"; TARGET="${2:-}"; shift; [[ -n "$TARGET" ]] && shift ;;
        --ack-all) MODE="ack-all"; shift ;;
        -h|--help)
            cat <<'EOF'
usage: workflows.sh [--here] [--ack <id>] [--ack-all]

  (default)      List all active workflows on this machine.
  --here         Only workflows whose project_root matches this repo.
  --ack <id>     Acknowledge (hide) a dead/finished workflow. Accepts a
                 full id or a unique substring; ambiguous substrings abort.
  --ack-all      Acknowledge every dead/finished workflow (stalled ones
                 are still alive and must be ack'd individually).
EOF
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; shift ;;
    esac
done

if [[ ! -d "$STATE_ROOT" ]]; then
    echo "No workflows have been launched on this machine."
    exit 0
fi

# Resolve "here" once if needed.
HERE_ROOT=""
if [[ $HERE_ONLY -eq 1 ]]; then
    HERE_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
fi

display_id() {
    # New-format workflow id: "<slug>-<rand6>". The slug is human-readable, so
    # show the full id — it's what correlates with git branches and state
    # directories, and it's what `--ack <id>` matches against.
    # Old-format id: "<YYYYMMDD-HHMMSS>-<pid>-<rand6>". For those, the trailing
    # rand6 is the only compact, unambiguous handle, so keep the historical
    # compact rendering to avoid a wide column full of timestamp noise.
    # Tighten the PID segment to 3–6 digits so a new-format slug composed of
    # pure digits in the same shape (theoretically possible) doesn't get
    # misclassified as old-format.
    local id="$1"
    if [[ "$id" =~ ^[0-9]{8}-[0-9]{6}-[0-9]{3,6}-[a-f0-9]{6}$ ]]; then
        echo "${id##*-}"
    else
        echo "$id"
    fi
}

# Portable ISO-8601 → epoch. Tries GNU `date -d` first, then BSD `date -j -f`.
iso_to_epoch() {
    local iso="$1" e
    [[ -n "$iso" ]] || { echo ""; return; }
    e=$(date -u -d "$iso" +%s 2>/dev/null) || \
        e=$(date -uj -f "%Y-%m-%dT%H:%M:%SZ" "$iso" +%s 2>/dev/null) || e=""
    echo "$e"
}

humanize_age() {
    local started_at="$1" then now diff
    then=$(iso_to_epoch "$started_at")
    [[ -n "$then" ]] || { echo "-"; return; }
    now=$(date -u +%s)
    diff=$(( now - then ))
    if   (( diff < 60    )); then echo "${diff}s"
    elif (( diff < 3600  )); then echo "$((diff/60))m"
    elif (( diff < 86400 )); then echo "$((diff/3600))h"
    else                          echo "$((diff/86400))d"
    fi
}

render_sub_stage() {
    local sub="$1"
    [[ -n "$sub" && "$sub" != "null" ]] || { echo ""; return; }
    # Prefer JSON object rendering; if not valid JSON, echo the string as-is.
    local rendered
    rendered=$(jq -er 'to_entries | map("\(.key)=\(.value)") | join(",")' \
               <<<"$sub" 2>/dev/null) && { echo "$rendered"; return; }
    echo "$sub"
}

shopt -s nullglob

# --ack / --ack-all branches.
if [[ "$MODE" == "ack" ]]; then
    if [[ -z "$TARGET" ]]; then
        echo "usage: workflows.sh --ack <id>" >&2
        exit 0
    fi
    # Collect substring matches first: if >1 workflow id contains TARGET, refuse
    # to ack any of them so a common fragment (e.g. a date) can't silently
    # dismiss a whole batch.
    matches=()
    for sf in "$STATE_ROOT"/*/state.json; do
        d=$(dirname "$sf")
        id=$(basename "$d")
        [[ "$id" == *"$TARGET"* ]] || continue
        matches+=("$sf")
    done
    if (( ${#matches[@]} == 0 )); then
        echo "no workflow matched: $TARGET"
        exit 0
    fi
    if (( ${#matches[@]} > 1 )); then
        echo "ambiguous id '$TARGET' matched ${#matches[@]} workflows — use a longer prefix:"
        for sf in "${matches[@]}"; do
            echo "  $(basename "$(dirname "$sf")")"
        done
        exit 0
    fi
    sf="${matches[0]}"
    d=$(dirname "$sf")
    id=$(basename "$d")
    # Only acknowledge workflows that are actually dead/finished. A stalled
    # workflow is still running (just quiet), so hiding it could bury a
    # slow-but-alive pipeline the user still cares about.
    live=$(liveness_of_state_file "$sf")
    case "$live" in
        dead:*)
            touch "$d/acknowledged" 2>/dev/null || true
            echo "acknowledged $id ($live)"
            ;;
        *)
            echo "skipping $id ($live is still running; only dead/finished workflows can be acknowledged)"
            ;;
    esac
    exit 0
fi

if [[ "$MODE" == "ack-all" ]]; then
    matched=0
    for sf in "$STATE_ROOT"/*/state.json; do
        d=$(dirname "$sf")
        live=$(liveness_of_state_file "$sf")
        # Dead-only: stalled workflows still have a live pid and may still
        # produce output. Users can ack them individually once they crash.
        if [[ "$live" == dead:* ]]; then
            touch "$d/acknowledged" 2>/dev/null || true
            echo "acknowledged $(basename "$d") ($live)"
            matched=$((matched + 1))
        fi
    done
    (( matched == 0 )) && echo "nothing to acknowledge."
    exit 0
fi

# Default MODE=list. Collect rows for sorting by started_at.
rows=()
for sf in "$STATE_ROOT"/*/state.json; do
    d=$(dirname "$sf")
    [[ -f "$sf" ]] || continue
    # Acknowledged entries are hidden from the list view.
    [[ -f "$d/acknowledged" ]] && continue

    # One jq fork, fields joined by ASCII Unit Separator (\x1f). We can't use
    # @tsv + IFS=$'\t' here: bash classifies tab as IFS-whitespace, so a
    # sequence like "a\t\tb" collapses into just two fields, which silently
    # loses empty-string columns (e.g. a workflow with no sub_stage). US is
    # non-whitespace, so consecutive separators preserve empty fields.
    IFS=$'\x1f' read -r id kind pr stage sub desc started_at < <(
        jq -r '[.id,
                .kind,
                (.project_root // ""),
                (.stage // ""),
                (if (.sub_stage|type)=="object" then (.sub_stage|tojson)
                 else (.sub_stage // "" | tostring) end),
                (.description // .instructions // ""),
                (.started_at // "")] | join("\u001f")' "$sf" 2>/dev/null || true
    )
    [[ -n "$id" ]] || continue

    if [[ $HERE_ONLY -eq 1 && -n "$HERE_ROOT" && "$pr" != "$HERE_ROOT" ]]; then
        continue
    fi

    live=$(liveness_of_state_file "$sf")
    stage_disp="${stage:--}"
    sub_disp=$(render_sub_stage "$sub")
    [[ -n "$sub_disp" ]] && stage_disp+=" ($sub_disp)"

    # Trim noisy long fields. Widths rebalanced to budget ~47 chars for the
    # now-human-readable ID column without wrapping on a typical ~180-col
    # terminal. Liveness keeps its historical width because truncating it
    # hides death reasons (e.g. "dead:crashed (pid NNNNN gone)"); stage is
    # shaved instead.
    stage_trim="${stage_disp:0:22}"
    live_trim="${live:0:28}"
    desc_trim="${desc:0:60}"
    age=$(humanize_age "$started_at")
    case "$kind" in
        localimplement) kind_short=local ;;
        ghimplement)    kind_short=gh    ;;
        *)              kind_short="$kind" ;;
    esac
    sid=$(display_id "$id")

    # started_at sorts lexicographically because the format is ISO-8601-Z.
    # Use US (\x1f) as the in-band separator: same rationale as the jq
    # pipeline above — empty columns (e.g. missing stage) must survive `read`.
    rows+=("${started_at}"$'\x1f'"${kind_short}"$'\x1f'"${sid}"$'\x1f'"${stage_trim}"$'\x1f'"${live_trim}"$'\x1f'"${age}"$'\x1f'"${desc_trim}")
done

if (( ${#rows[@]} == 0 )); then
    if [[ $HERE_ONLY -eq 1 ]]; then
        echo "No active workflows for project: $HERE_ROOT"
    else
        echo "No active workflows on this machine."
    fi
    exit 0
fi

# Sort ascending by started_at (oldest first). Read back into an array.
sorted_rows=()
while IFS= read -r line; do
    sorted_rows+=("$line")
done < <(printf '%s\n' "${rows[@]}" | sort)

# Header + rows. Column widths are fixed; columns will overflow gracefully if
# content exceeds them (no truncation beyond the pre-trim above). The ID
# column is sized for the new "<slug>-<rand6>" format (slug up to 40 chars
# + 1 hyphen + 6 hex = 47). Old-format ids render as their 6-hex tail via
# display_id() and fit in the same column.
FMT='%-5s  %-47s  %-22s  %-28s  %-5s  %s\n'
# shellcheck disable=SC2059
printf "$FMT" "KIND" "ID" "STAGE" "LIVENESS" "AGE" "DESCRIPTION"
for row in "${sorted_rows[@]}"; do
    IFS=$'\x1f' read -r _ kind sid stage live age desc <<<"$row"
    # shellcheck disable=SC2059
    printf "$FMT" "$kind" "$sid" "$stage" "$live" "$age" "$desc"
done

exit 0
