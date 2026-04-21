#!/usr/bin/env bash
# Generic launcher for background skill gremlins (ghgremlin, localgremlin).
# Sets up an isolated workdir, writes per-gremlin state under
# ${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins/, spawns the real
# gremlin detached from the caller's session, and returns fast.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

usage() {
    cat >&2 <<'EOF'
usage: launch.sh [--description <phrase>] <kind> [pipeline-args...]
       kind ∈ {ghgremlin, localgremlin}
EOF
    exit 1
}

# slugify <text> — reduce arbitrary input to [a-z0-9-]+ (max 40 chars),
# suitable as both a git-ref component and a filesystem directory name.
# Empty/whitespace-only input produces empty output so the caller can fall
# back. The restricted charset is chosen by construction — no runtime
# validation needed downstream.
slugify() {
    local input="$1" slug max=40
    slug=$(printf '%s' "$input" \
        | LC_ALL=C tr '[:upper:]' '[:lower:]' \
        | LC_ALL=C tr -c 'a-z0-9' '-' \
        | LC_ALL=C sed -e 's/--*/-/g' -e 's/^-//' -e 's/-$//')
    if (( ${#slug} > max )); then
        local trimmed="${slug:0:max}"
        # Prefer trimming at the last hyphen so we don't cut mid-word, but
        # only if doing so leaves a substantial slug (>=20 chars).
        local head="${trimmed%-*}"
        if [[ "$head" != "$trimmed" && ${#head} -ge 20 ]]; then
            trimmed="$head"
        fi
        slug="${trimmed%-}"
    fi
    printf '%s' "$slug"
}

DESCRIPTION=""
RESUME_GR_ID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --description)
            [[ $# -ge 2 ]] || usage
            DESCRIPTION="$2"
            shift 2
            ;;
        --resume)
            [[ $# -ge 2 ]] || usage
            RESUME_GR_ID="$2"
            shift 2
            ;;
        --) shift; break ;;
        -*) die "unknown flag: $1" ;;
        *)  break ;;
    esac
done

# --resume branch: reuse an existing gremlin's state dir, worktree, and branch,
# and relaunch the pipeline with --resume-from <failed-stage> so it skips
# already-completed stages. Phase B of /gremlins rescue drives this path.
if [[ -n "$RESUME_GR_ID" ]]; then
    [[ $# -eq 0 ]] || die "--resume does not take additional arguments"
    [[ -z "$DESCRIPTION" ]] || die "--resume is incompatible with --description"

    command -v jq     >/dev/null 2>&1 || die "jq not found"
    command -v claude >/dev/null 2>&1 || die "claude CLI not found"

    STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins"
    STATE_DIR="$STATE_ROOT/$RESUME_GR_ID"
    STATE_FILE="$STATE_DIR/state.json"
    [[ -d "$STATE_DIR" && -f "$STATE_FILE" ]] || die "no state at $STATE_DIR"

    RESUME_KIND=$(jq   -r '.kind         // ""' "$STATE_FILE")
    WORKDIR=$(jq       -r '.workdir      // ""' "$STATE_FILE")
    BRANCH=$(jq        -r '.branch       // ""' "$STATE_FILE")
    STAGE=$(jq         -r '.stage        // ""' "$STATE_FILE")
    INSTRUCTIONS=$(jq  -r '.instructions // ""' "$STATE_FILE")
    STATUS=$(jq        -r '.status       // ""' "$STATE_FILE")
    OLD_PID=$(jq       -r '.pid          // ""' "$STATE_FILE")
    EXIT_CODE=$(jq     -r '.exit_code    // ""' "$STATE_FILE")

    case "$RESUME_KIND" in
        ghgremlin|localgremlin) ;;
        *) die "invalid kind in state.json: $RESUME_KIND" ;;
    esac
    [[ -n "$WORKDIR" && -d "$WORKDIR" ]] || die "worktree missing: $WORKDIR"
    if [[ "$RESUME_KIND" == "ghgremlin" ]]; then
        command -v gh >/dev/null 2>&1 || die "gh CLI not found"
    fi

    # Defense-in-depth: refuse resuming an already-live or already-successful
    # gremlin. /gremlins rescue enforces the same checks before invoking us.
    if [[ "$STATUS" == "running" && -n "$OLD_PID" && "$OLD_PID" != "null" ]] \
       && kill -0 "$OLD_PID" 2>/dev/null; then
        die "gremlin $RESUME_GR_ID is still running (pid $OLD_PID) — stop it first"
    fi
    if [[ -f "$STATE_DIR/finished" && "$EXIT_CODE" == "0" ]]; then
        die "gremlin $RESUME_GR_ID finished successfully — nothing to resume"
    fi

    PIPELINE=""
    for ext in py sh; do
        candidate="$HOME/.claude/skills/$RESUME_KIND/$RESUME_KIND.$ext"
        if [[ -x "$candidate" ]]; then PIPELINE="$candidate"; break; fi
    done
    [[ -n "$PIPELINE" ]] || die "no executable gremlin at $HOME/.claude/skills/$RESUME_KIND/$RESUME_KIND.{py,sh}"

    # If the gremlin crashed before set-stage was called, .stage is literally
    # "starting" (the initial state.json value). Rewind to the first stage.
    if [[ -z "$STAGE" || "$STAGE" == "starting" ]]; then
        STAGE="plan"
    fi

    # Clear terminal markers so the resumed gremlin gets announced fresh on
    # the next session-summary firing, and so liveness doesn't classify us
    # as dead:finished while we're running again.
    rm -f "$STATE_DIR/finished" "$STATE_DIR/summarized" 2>/dev/null || true

    NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    STATE_TMP="$STATE_FILE.tmp"
    jq --arg    status             "running" \
       --arg    stage              "$STAGE" \
       --arg    rescued_at         "$NOW_ISO" \
       --arg    resumed_from_stage "$STAGE" \
       '.status = $status
        | .stage = $stage
        | del(.exit_code)
        | del(.ended_at)
        | .rescue_count = ((.rescue_count // 0) + 1)
        | .rescued_at = $rescued_at
        | .resumed_from_stage = $resumed_from_stage
        | .pid = null' \
       "$STATE_FILE" > "$STATE_TMP" || die "failed to patch state.json"
    mv "$STATE_TMP" "$STATE_FILE"

    # Append a resume header to the existing log so failure context is
    # preserved above the new run's output.
    {
        printf '\n--- resume at %s (from stage: %s) ---\n' "$NOW_ISO" "$STAGE"
    } >> "$STATE_DIR/log" 2>/dev/null || true

    export PIPELINE
    export GR_ID="$RESUME_GR_ID"

    (
        cd "$WORKDIR"
        nohup bash -c '"$PIPELINE" "$@"; EC=$?; "$HOME/.claude/skills/_bg/finish.sh" "$GR_ID" "$EC"' \
            -- --resume-from "$STAGE" "$INSTRUCTIONS" </dev/null >>"$STATE_DIR/log" 2>&1 &
        echo $! >"$STATE_DIR/pid"
    )

    PID=$(cat "$STATE_DIR/pid" 2>/dev/null || true)
    if [[ -n "$PID" ]]; then
        jq --argjson pid "$PID" '.pid = $pid' "$STATE_FILE" > "$STATE_TMP" \
            && mv "$STATE_TMP" "$STATE_FILE"
    fi

    cat <<EOF
resumed gremlin: $RESUME_GR_ID
from stage:      $STAGE
workdir:         $WORKDIR
log:             $STATE_DIR/log
state file:      $STATE_FILE
pid:             ${PID:-unknown}

The $RESUME_KIND gremlin is running in the background. You'll be notified in a
future Claude session for this project when it finishes.
EOF
    exit 0
fi

[[ $# -ge 1 ]] || usage
KIND="$1"
shift

case "$KIND" in
    ghgremlin|localgremlin) ;;
    *) die "invalid kind: $KIND (allowed: ghgremlin, localgremlin)" ;;
esac

command -v jq     >/dev/null 2>&1 || die "jq not found"
command -v claude >/dev/null 2>&1 || die "claude CLI not found"
if [[ "$KIND" == "ghgremlin" ]]; then
    command -v gh >/dev/null 2>&1 || die "gh CLI not found"
fi

PIPELINE=""
for ext in py sh; do
    candidate="$HOME/.claude/skills/$KIND/$KIND.$ext"
    if [[ -x "$candidate" ]]; then PIPELINE="$candidate"; break; fi
done
[[ -n "$PIPELINE" ]] || die "no executable gremlin at $HOME/.claude/skills/$KIND/$KIND.{py,sh}"

if PROJECT_ROOT=$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null); then
    IS_GIT=1
else
    PROJECT_ROOT=$(pwd)
    IS_GIT=0
fi
[[ -n "$PROJECT_ROOT" && -d "$PROJECT_ROOT" ]] || die "could not resolve project root"

INSTR_RAW="$*"
INSTR_SUMMARY="${INSTR_RAW:0:200}"

# Walk $@ skipping leading flags (and their values) to find the first
# positional arg — this avoids contaminating slugs with "-a opus -b sonnet".
# Heuristic: assumes each flag takes one value. Boolean flags followed
# by a positional will swallow the positional; on miss we still produce a
# usable slug from the raw-instructions fallback below.
# Hoisted before SLUG_SOURCE so spec-copying (after STATE_DIR creation) can
# reuse the same value without repeating the walk.
_args=("$@")
_i=0
while (( _i < ${#_args[@]} )); do
    _a="${_args[_i]}"
    if [[ "$_a" == "--" ]]; then
        _i=$((_i + 1))
        break
    elif [[ "$_a" == -* ]]; then
        _i=$((_i + 1))
        if (( _i < ${#_args[@]} )) && [[ "${_args[_i]}" != -* ]]; then
            _i=$((_i + 1))
        fi
    else
        break
    fi
done
_first_positional=""
(( _i < ${#_args[@]} )) && _first_positional="${_args[_i]}"

# Slug source resolution, in priority order. This runs *before* the
# DESCRIPTION-from-INSTR_RAW fallback so the file-path branch isn't
# shadowed by the fallback (which would feed the slugifier a path like
# `/tmp/test-spec-slug.md` and produce `tmp-test-spec-slug-md`).
#   1. Explicit --description (SKILL.md callers compose a clean ≤60-char phrase).
#   2. If the first positional argument is a readable file, use its first
#      `# heading` line, or fall back to its basename without extension.
#   3. The raw instructions with leading flags stripped, first 80 chars.
#   4. Literal "gremlin" last-resort (applied after slugify if result empty).
SLUG_SOURCE=""
if [[ -n "$DESCRIPTION" ]]; then
    SLUG_SOURCE="$DESCRIPTION"
else
    if [[ -n "$_first_positional" && -f "$_first_positional" ]]; then
        # Quit on first `# …` line — BSD sed doesn't accept a line-range
        # with a nested {…} block, so we use a plain pattern with `q` and
        # cap the scan to 50 lines by piping through head (sed stops reading
        # once `q` fires, so this is only a safety bound).
        _title=$(head -n 50 "$_first_positional" 2>/dev/null \
                 | sed -nE '/^#+[[:space:]]+.+/{s/^#+[[:space:]]+//p;q;}' \
                 || true)
        if [[ -n "$_title" ]]; then
            SLUG_SOURCE="$_title"
        else
            _base="${_first_positional##*/}"
            SLUG_SOURCE="${_base%.*}"
        fi
    else
        _rem=""
        for (( _j=_i; _j<${#_args[@]}; _j++ )); do
            _rem+="${_args[_j]} "
        done
        SLUG_SOURCE="${_rem% }"
        SLUG_SOURCE="${SLUG_SOURCE:0:80}"
    fi
fi

# If the first positional arg is a readable file (e.g. a spec from /design),
# copy it into the artifacts dir as spec.md for durable storage. This happens
# before the gremlin launches so the artifact is preserved even on failure.
_spec_copy_pending=""
if [[ -n "$_first_positional" && -r "$_first_positional" && -f "$_first_positional" ]]; then
    _spec_copy_pending="$_first_positional"
fi

SLUG=$(slugify "$SLUG_SOURCE")
[[ -z "$SLUG" ]] && SLUG="gremlin"

# Description fallback: explicit --description wins; otherwise fall back to
# a truncated slice of the raw instructions so the status views always have
# something to print.
if [[ -z "$DESCRIPTION" ]]; then
    DESCRIPTION="${INSTR_RAW:0:60}"
fi

RANDHEX=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c 6 || true)
[[ -n "$RANDHEX" ]] || RANDHEX="xxxxxx"
GR_ID="${SLUG}-${RANDHEX}"

STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins"
STATE_DIR="$STATE_ROOT/$GR_ID"
mkdir -p "$STATE_DIR" || die "could not create state dir: $STATE_DIR"

if [[ -n "$_spec_copy_pending" ]]; then
    mkdir -p "$STATE_DIR/artifacts"
    cp "$_spec_copy_pending" "$STATE_DIR/artifacts/spec.md" || die "could not copy spec to artifacts: $_spec_copy_pending"
fi

# Isolated workdir setup. For localgremlin in a git repo we create a named
# branch (bg/localgremlin/<GR_ID>) so the commits the gremlin makes stay
# reachable after finish.sh runs. For ghgremlin we use --detach because the
# gremlin's stage 2b creates and pushes its own issue-N-<slug> branch; a
# named bg/* ref would be a no-op.
BRANCH=""
if [[ $IS_GIT -eq 1 ]]; then
    WORKDIR=$(mktemp -d -t "aibg-$KIND.XXXXXX") || die "mktemp failed"
    rmdir "$WORKDIR" || die "rmdir $WORKDIR failed"
    if [[ "$KIND" == "localgremlin" ]]; then
        SETUP_KIND="worktree-branch"
        BRANCH="bg/localgremlin/$GR_ID"
        git -C "$PROJECT_ROOT" worktree add -b "$BRANCH" "$WORKDIR" HEAD >/dev/null \
            || die "git worktree add -b failed"
    else
        SETUP_KIND="worktree"
        git -C "$PROJECT_ROOT" worktree add --detach "$WORKDIR" HEAD >/dev/null \
            || die "git worktree add failed"
    fi
else
    SETUP_KIND="copy"
    WORKDIR=$(mktemp -d -t "aibg-$KIND.XXXXXX") || die "mktemp failed"
    cp -a "$PROJECT_ROOT/." "$WORKDIR/" || die "cp -a failed"
fi

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

STATE_FILE="$STATE_DIR/state.json"
STATE_TMP="$STATE_FILE.tmp"
jq -n \
    --arg id            "$GR_ID" \
    --arg kind          "$KIND" \
    --arg project_root  "$PROJECT_ROOT" \
    --arg workdir       "$WORKDIR" \
    --arg setup_kind    "$SETUP_KIND" \
    --arg branch        "$BRANCH" \
    --arg status        "running" \
    --arg started_at    "$NOW_ISO" \
    --arg instructions  "$INSTR_SUMMARY" \
    --arg description   "$DESCRIPTION" \
    '{id: $id, kind: $kind, project_root: $project_root, workdir: $workdir,
      setup_kind: $setup_kind, branch: $branch, status: $status,
      started_at: $started_at, instructions: $instructions,
      description: $description, stage: "starting", pid: null}' \
    > "$STATE_TMP" || die "failed to write initial state"
mv "$STATE_TMP" "$STATE_FILE"

# Export vars the child bash references via its single-quoted -c string.
export PIPELINE GR_ID

# Subshell + nohup detaches the gremlin from the caller's session: when the
# subshell exits, the backgrounded child is reparented to init (PPID=1), so it
# survives both parent-shell exit and Claude Code quit. nohup belt-and-
# suspenders the SIGHUP case. finish.sh is invoked after the gremlin to write
# the terminal state.
(
    cd "$WORKDIR"
    nohup bash -c '"$PIPELINE" "$@"; EC=$?; "$HOME/.claude/skills/_bg/finish.sh" "$GR_ID" "$EC"' \
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
gremlin id:  $GR_ID
workdir:     $WORKDIR
log:         $STATE_DIR/log
state file:  $STATE_FILE
pid:         ${PID:-unknown}

The $KIND gremlin is running in the background. You'll be notified in a future
Claude session for this project when it finishes.
EOF
