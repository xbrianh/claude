#!/usr/bin/env bash
# Generic launcher for background skill workflows (ghimplement, localimplement).
# Sets up an isolated workdir, writes per-workflow state under
# ${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows/, spawns the real
# pipeline detached from the caller's session, and returns fast.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

usage() {
    cat >&2 <<'EOF'
usage: launch.sh [--description <phrase>] <kind> [pipeline-args...]
       kind ∈ {ghimplement, localimplement}
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
while [[ $# -gt 0 ]]; do
    case "$1" in
        --description)
            [[ $# -ge 2 ]] || usage
            DESCRIPTION="$2"
            shift 2
            ;;
        --) shift; break ;;
        -*) die "unknown flag: $1" ;;
        *)  break ;;
    esac
done

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
#   4. Literal "workflow" last-resort (applied after slugify if result empty).
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
# before the pipeline launches so the artifact is preserved even on failure.
_spec_copy_pending=""
if [[ -n "$_first_positional" && -r "$_first_positional" && -f "$_first_positional" ]]; then
    _spec_copy_pending="$_first_positional"
fi

SLUG=$(slugify "$SLUG_SOURCE")
[[ -z "$SLUG" ]] && SLUG="workflow"

# Description fallback: explicit --description wins; otherwise fall back to
# a truncated slice of the raw instructions so the status views always have
# something to print.
if [[ -z "$DESCRIPTION" ]]; then
    DESCRIPTION="${INSTR_RAW:0:60}"
fi

RANDHEX=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c 6 || true)
[[ -n "$RANDHEX" ]] || RANDHEX="xxxxxx"
WF_ID="${SLUG}-${RANDHEX}"

STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows"
STATE_DIR="$STATE_ROOT/$WF_ID"
mkdir -p "$STATE_DIR" || die "could not create state dir: $STATE_DIR"

if [[ -n "$_spec_copy_pending" ]]; then
    mkdir -p "$STATE_DIR/artifacts"
    cp "$_spec_copy_pending" "$STATE_DIR/artifacts/spec.md" || die "could not copy spec to artifacts: $_spec_copy_pending"
fi

# Isolated workdir setup. For localimplement in a git repo we create a named
# branch (bg/localimplement/<WF_ID>) so the commits the pipeline makes stay
# reachable after finish.sh runs. For ghimplement we use --detach because the
# pipeline's stage 2b creates and pushes its own issue-N-<slug> branch; a
# named bg/* ref would be a no-op.
BRANCH=""
if [[ $IS_GIT -eq 1 ]]; then
    WORKDIR=$(mktemp -d -t "aibg-$KIND.XXXXXX") || die "mktemp failed"
    rmdir "$WORKDIR" || die "rmdir $WORKDIR failed"
    if [[ "$KIND" == "localimplement" ]]; then
        SETUP_KIND="worktree-branch"
        BRANCH="bg/localimplement/$WF_ID"
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
    --arg id            "$WF_ID" \
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
