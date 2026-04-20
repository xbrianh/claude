#!/usr/bin/env bash
# Generic launcher for background skill workflows (ghimplement, localimplement).
# Sets up an isolated workdir, writes per-workflow state under ~/.claude/workflows/,
# spawns the real pipeline detached from the caller's session, and returns fast.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

usage() {
    cat >&2 <<'EOF'
usage: launch.sh <kind> [pipeline-args...]
       kind ∈ {ghimplement, localimplement}
EOF
    exit 1
}

[[ $# -ge 1 ]] || usage
KIND="$1"
shift

case "$KIND" in
    ghimplement|localimplement) ;;
    *) die "invalid kind: $KIND (allowed: ghimplement, localimplement)" ;;
esac

command -v jq     >/dev/null 2>&1 || die "jq not found"
command -v claude >/dev/null 2>&1 || die "claude CLI not found"
if [[ "$KIND" == "ghimplement" ]]; then
    command -v gh >/dev/null 2>&1 || die "gh CLI not found"
fi

PIPELINE="$HOME/.claude/skills/$KIND/$KIND.sh"
[[ -x "$PIPELINE" ]] || die "pipeline script not executable: $PIPELINE"

PROJECT_ROOT="$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null || pwd)"
[[ -n "$PROJECT_ROOT" && -d "$PROJECT_ROOT" ]] || die "could not resolve project root"

RANDHEX=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c 6 || true)
[[ -n "$RANDHEX" ]] || RANDHEX="xxxxxx"
WF_ID="$(date -u +%Y%m%d-%H%M%S)-$$-$RANDHEX"

STATE_ROOT="$HOME/.claude/workflows"
STATE_DIR="$STATE_ROOT/$WF_ID"
mkdir -p "$STATE_DIR" || die "could not create state dir: $STATE_DIR"

# Isolated workdir: git worktree (detached HEAD) for git repos, cp -a otherwise.
if git -C "$PROJECT_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    SETUP_KIND="worktree"
    WORKDIR=$(mktemp -d -t "aibg-$KIND.XXXXXX") || die "mktemp failed"
    rmdir "$WORKDIR" || die "rmdir $WORKDIR failed"
    git -C "$PROJECT_ROOT" worktree add --detach "$WORKDIR" HEAD >/dev/null \
        || die "git worktree add failed"
else
    SETUP_KIND="copy"
    WORKDIR=$(mktemp -d -t "aibg-$KIND.XXXXXX") || die "mktemp failed"
    cp -a "$PROJECT_ROOT/." "$WORKDIR/" || die "cp -a failed"
fi

INSTR_RAW="$*"
INSTR_SUMMARY="${INSTR_RAW:0:200}"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

STATE_FILE="$STATE_DIR/state.json"
STATE_TMP="$STATE_FILE.tmp"
jq -n \
    --arg id            "$WF_ID" \
    --arg kind          "$KIND" \
    --arg project_root  "$PROJECT_ROOT" \
    --arg workdir       "$WORKDIR" \
    --arg setup_kind    "$SETUP_KIND" \
    --arg status        "running" \
    --arg started_at    "$NOW_ISO" \
    --arg instructions  "$INSTR_SUMMARY" \
    '{id: $id, kind: $kind, project_root: $project_root, workdir: $workdir,
      setup_kind: $setup_kind, status: $status, started_at: $started_at,
      instructions: $instructions, pid: null}' \
    > "$STATE_TMP" || die "failed to write initial state"
mv "$STATE_TMP" "$STATE_FILE"

# Export vars the child bash references via its single-quoted -c string.
export PIPELINE WF_ID

# Subshell + nohup detaches the pipeline from the caller's session: when the
# subshell exits, the backgrounded child is reparented to init (PPID=1), so it
# survives both parent-shell exit and Claude Code quit. nohup belt-and-
# suspenders the SIGHUP case. finish.sh is invoked after the pipeline to write
# the terminal state.
(
    cd "$WORKDIR"
    nohup bash -c '"$PIPELINE" "$@"; EC=$?; "$HOME/.claude/skills/_bg/finish.sh" "$WF_ID" "$EC"' \
        -- "$@" </dev/null >"$STATE_DIR/log" 2>&1 &
    echo $! >"$STATE_DIR/pid"
)

# Patch the captured PID into state.json atomically.
PID=$(cat "$STATE_DIR/pid" 2>/dev/null || true)
if [[ -n "$PID" ]]; then
    jq --argjson pid "$PID" '.pid = $pid' "$STATE_FILE" > "$STATE_TMP" \
        && mv "$STATE_TMP" "$STATE_FILE"
fi

cat <<EOF
workflow id: $WF_ID
workdir:     $WORKDIR
log:         $STATE_DIR/log
state file:  $STATE_FILE
pid:         ${PID:-unknown}

The $KIND pipeline is running in the background. You'll be notified in a future
Claude session for this project when it finishes.
EOF
