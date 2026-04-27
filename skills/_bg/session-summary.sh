#!/usr/bin/env bash
# SessionStart / UserPromptSubmit hook: reports on background gremlins for the
# current project. Running gremlins are shown at session start; newly-finished
# gremlins are shown in both hooks (and marked `summarized` on first show so
# they aren't re-announced). The `closed` marker is reserved for the explicit
# user action (`/gremlins close <id>`) and hides the gremlin from `/gremlins`.
#
# Degrades silently on any unexpected condition: hooks must never break a
# session.
set -u

# Opt-out for callers that invoke `claude -p` for structured text output (e.g.
# commit-message synthesis in gremlins/fleet.py `land`). The hook's "surface
# this verbatim" directive otherwise prepends the status block to the model's
# reply and corrupts the caller's parse. Repo-scoped prefix (not `CLAUDE_`)
# so we don't collide with Claude Code's own env-var namespace.
[[ "${GREMLIN_SKIP_SUMMARY:-0}" == "1" ]] && exit 0

# Missing jq → nothing to report, so exit quietly.
command -v jq >/dev/null 2>&1 || exit 0

STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins"
[[ -d "$STATE_ROOT" ]] || exit 0

# Source the shared liveness classifier. Both this hook and the on-demand
# `/gremlins` listing (`gremlins/fleet.py`, which inlines an equivalent
# classifier) should agree on "is this gremlin still alive". Degrade
# gracefully if the library isn't installed yet — hooks must never break a
# session.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/liveness.sh" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$SCRIPT_DIR/liveness.sh"
elif [[ -f "$HOME/.claude/skills/_bg/liveness.sh" ]]; then
    # shellcheck disable=SC1090
    source "$HOME/.claude/skills/_bg/liveness.sh"
else
    # Library not installed yet (can happen during a partial sync between this
    # repo and ~/.claude/). Inline a minimal classifier so a crashed gremlin
    # doesn't get reported as "running" forever. The full library adds a stall
    # heuristic on top of this; we skip it in the fallback.
    liveness_of_state_file() {
        local sf="$1" wdir st p ec
        [[ -f "$sf" ]] || return 0
        wdir=$(dirname "$sf")
        if [[ -f "$wdir/finished" ]]; then
            echo "dead:finished"
            return 0
        fi
        IFS=$'\x1f' read -r st p ec < <(
            jq -r '[.status, (.pid // "" | tostring),
                    (.exit_code // "" | tostring)] | join("\u001f")' \
                "$sf" 2>/dev/null || true
        )
        if [[ "$st" == "running" && -n "$p" && "$p" != "null" ]] \
           && ! kill -0 "$p" 2>/dev/null; then
            echo "dead:crashed (pid $p gone)"
            return 0
        fi
        echo "${st:-running}"
    }
fi

# Read hook input JSON from stdin if present.
INPUT=""
if [[ ! -t 0 ]]; then
    INPUT=$(cat 2>/dev/null || true)
fi

HOOK_EVENT=""
CWD_FROM_INPUT=""
if [[ -n "$INPUT" ]]; then
    HOOK_EVENT=$(jq -r '.hook_event_name // empty' <<<"$INPUT" 2>/dev/null || true)
    CWD_FROM_INPUT=$(jq -r '.cwd // empty'          <<<"$INPUT" 2>/dev/null || true)
fi

# Project root resolution: $CLAUDE_PROJECT_DIR (per Claude Code hook docs) →
# stdin cwd → pwd. Normalize to git toplevel when possible so comparisons
# against state.project_root (also git-toplevel) match.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-}"
[[ -z "$PROJECT_ROOT" ]] && PROJECT_ROOT="$CWD_FROM_INPUT"
[[ -z "$PROJECT_ROOT" ]] && PROJECT_ROOT="$(pwd)"
if [[ -n "$PROJECT_ROOT" ]]; then
    TOP=$(git -C "$PROJECT_ROOT" rev-parse --show-toplevel 2>/dev/null || true)
    [[ -n "$TOP" ]] && PROJECT_ROOT="$TOP"
fi

NL=$'\n'
RUNNING_BLOCK=""
FINISHED_BLOCK=""
NEWLY_SUMMARIZED_DIRS=()
FINISHED_COUNT=0

shopt -s nullglob
for sf in "$STATE_ROOT"/*/state.json; do
    [[ -f "$sf" ]] || continue
    wdir=$(dirname "$sf")

    # One jq fork per state file. Fields joined by ASCII Unit Separator
    # (\x1f) rather than tab: bash classifies tab as IFS-whitespace, so
    # consecutive tabs (e.g. two empty fields in a row) collapse and silently
    # lose columns. US is non-whitespace, so `read` preserves empty fields.
    IFS=$'\x1f' read -r pr id kind status workdir pid exit_code stage description < <(
        jq -r '[.project_root, .id, .kind, .status, .workdir,
                (.pid // "" | tostring),
                (.exit_code // "" | tostring),
                (.stage // ""),
                (.description // .instructions // "")] | join("\u001f")' "$sf" 2>/dev/null || true
    )
    [[ "$pr" == "$PROJECT_ROOT" ]] || continue

    finished_marker="$wdir/finished"
    summarized_marker="$wdir/summarized"
    closed_marker="$wdir/closed"
    log="$wdir/log"

    desc_suffix=""
    [[ -n "$description" ]] && desc_suffix=" — _${description}_"

    if [[ -f "$finished_marker" && ! -f "$summarized_marker" && ! -f "$closed_marker" ]]; then
        FINISHED_BLOCK+="- \`$id\` ($kind): **$status**${exit_code:+ (exit $exit_code)}${desc_suffix} — log: $log${NL}"
        NEWLY_SUMMARIZED_DIRS+=("$wdir")
        FINISHED_COUNT=$((FINISHED_COUNT + 1))
        continue
    fi

    if [[ "$status" == "running" ]]; then
        live=$(liveness_of_state_file "$sf")
        stage_disp="${stage:-?}"
        case "$live" in
            dead:*)
                reason="${live#dead:}"
                RUNNING_BLOCK+="- \`$id\` ($kind): **$reason** (stage: $stage_disp)${desc_suffix} — log: $log${NL}"
                ;;
            stalled:*)
                reason="${live#stalled:}"
                RUNNING_BLOCK+="- \`$id\` ($kind): **stalled?** ($reason, stage: $stage_disp, pid ${pid:-?})${desc_suffix} — log: $log${NL}"
                ;;
            *)
                RUNNING_BLOCK+="- \`$id\` ($kind): running (stage: $stage_disp, pid ${pid:-?})${desc_suffix} — log: $log${NL}"
                ;;
        esac
    fi
done

# Decide what to include per hook event.
SHOW_RUNNING=0
SHOW_FINISHED=0
case "$HOOK_EVENT" in
    SessionStart)      SHOW_RUNNING=1; SHOW_FINISHED=1 ;;
    UserPromptSubmit)  SHOW_FINISHED=1 ;;
    *)                 SHOW_RUNNING=1; SHOW_FINISHED=1 ;;
esac

# On UserPromptSubmit with no new finishes, emit nothing (don't spam every prompt).
if [[ "$HOOK_EVENT" == "UserPromptSubmit" && $FINISHED_COUNT -eq 0 ]]; then
    exit 0
fi

SUMMARY=""
if [[ $SHOW_RUNNING -eq 1 && -n "$RUNNING_BLOCK" ]]; then
    SUMMARY+="**Background gremlins — running:**${NL}${RUNNING_BLOCK}"
fi
if [[ $SHOW_FINISHED -eq 1 && -n "$FINISHED_BLOCK" ]]; then
    [[ -n "$SUMMARY" ]] && SUMMARY+="${NL}"
    SUMMARY+="**Background gremlins — finished since last check:**${NL}${FINISHED_BLOCK}"
fi

if [[ -n "$SUMMARY" ]]; then
    # additionalContext is model-visible, not user-visible. Prefix an explicit
    # directive so the model surfaces it verbatim to the user. Also write the
    # raw summary to stderr: Claude Code routes non-blocking hook stderr into
    # the transcript, giving us a second user-visible channel.
    DIRECTIVE="IMPORTANT: Before doing anything else in your next response, surface the following background-gremlin status to the user verbatim (as a markdown block, no paraphrasing):${NL}${NL}"
    FULL="${DIRECTIVE}${SUMMARY}"

    printf '%s' "$SUMMARY" >&2

    # Schema verified against https://code.claude.com/docs/en/hooks (fetched
    # 2026-04-20): both SessionStart and UserPromptSubmit accept
    # {hookSpecificOutput: {hookEventName, additionalContext}} and inject
    # additionalContext into Claude's context.
    EVENT_OUT="${HOOK_EVENT:-SessionStart}"
    jq -n \
        --arg event "$EVENT_OUT" \
        --arg ctx   "$FULL" \
        '{hookSpecificOutput: {hookEventName: $event, additionalContext: $ctx}}'

    for d in "${NEWLY_SUMMARIZED_DIRS[@]}"; do
        touch "$d/summarized" 2>/dev/null || true
    done
fi

# Prune old closed state dirs (>14 days by mtime of `closed`).
# Safety-guard the rm inside the state-root path.
while IFS= read -r ack; do
    d=$(dirname "$ack")
    case "$d" in
        "$STATE_ROOT"/*) rm -rf "$d" 2>/dev/null || true ;;
    esac
done < <(find "$STATE_ROOT" -maxdepth 2 -name closed -mtime +14 -print 2>/dev/null || true)

# Prune old direct-CLI session dirs (>14 days by dir mtime). Direct
# invocations of `localgremlin.sh` write artifacts under
# `$STATE_ROOT/direct/<ts>-<rand>/` but never drop a `state.json` or
# `closed` marker, so the closed-based sweep above would leave
# them to accumulate forever. Match by dir mtime instead; the artifacts
# subtree under each direct session is self-contained.
if [[ -d "$STATE_ROOT/direct" ]]; then
    while IFS= read -r d; do
        case "$d" in
            "$STATE_ROOT"/direct/*) rm -rf "$d" 2>/dev/null || true ;;
        esac
    done < <(find "$STATE_ROOT/direct" -mindepth 1 -maxdepth 1 -type d -mtime +14 -print 2>/dev/null || true)
fi

exit 0
