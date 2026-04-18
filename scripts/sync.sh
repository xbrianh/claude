#!/usr/bin/env bash
# Bidirectional sync between the user's home config and this repo.
# Tracks:
#   ~/.claude/CLAUDE.md, ~/.claude/settings.json
#   ~/.claude/skills/, ~/.claude/agents/, ~/.claude/commands/
# Excludes: settings.local.json (local-only)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_DIR="$HOME/.claude"

# Threshold above which `push`/`pull` refuse to delete without --force.
DELETE_THRESHOLD=5

# "<repo-relative-path>:<absolute-home-path>"
FILE_PAIRS=(
    "home/CLAUDE.md:$CLAUDE_DIR/CLAUDE.md"
    "settings.json:$CLAUDE_DIR/settings.json"
)
DIR_PAIRS=(
    "skills:$CLAUDE_DIR/skills"
    "agents:$CLAUDE_DIR/agents"
    "commands:$CLAUDE_DIR/commands"
)

help_text() {
    cat <<EOF
Usage: $(basename "$0") <command> [options]

Commands:
  pull        Copy from ~/.claude/ into this repo (mirror: deletes extras in repo dirs)
  push        Copy from this repo into ~/.claude/ (mirror: deletes extras in ~/.claude dirs)
  diff        Show differences between repo and ~/.claude/
  status      Alias for diff

Options:
  -y, --yes       Skip confirmation prompt
  -n, --dry-run   Show what would change without writing
  -f, --force     Allow deleting more than $DELETE_THRESHOLD files during push/pull
  -h, --help      Show this help
EOF
}

die_usage() {
    [[ -n "${1:-}" ]] && echo "$1" >&2
    help_text >&2
    exit 1
}

# Handle help flag before extracting CMD so `./sync.sh --help` works.
case "${1:-}" in
    -h|--help)
        help_text
        exit 0
        ;;
    "")
        die_usage
        ;;
esac

CMD="$1"; shift || true
YES=0
DRY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)     YES=1 ;;
        -n|--dry-run) DRY=1 ;;
        -f|--force)   FORCE=1 ;;
        -h|--help)    help_text; exit 0 ;;
        *) die_usage "Unknown option: $arg" ;;
    esac
done

confirm() {
    [[ $YES -eq 1 ]] && return 0
    read -r -p "$1 [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

BASE_FLAGS=(-a --itemize-changes)
[[ $DRY -eq 1 ]] && BASE_FLAGS+=(--dry-run)

sync_file() {
    local src="$1" dst="$2"
    if [[ ! -e "$src" ]]; then
        echo "skip: $src (not present)"
        return
    fi
    mkdir -p "$(dirname "$dst")"
    rsync "${BASE_FLAGS[@]}" "$src" "$dst"
}

sync_dir() {
    local src="$1" dst="$2"
    if [[ ! -d "$src" ]]; then
        echo "skip: $src/ (not present)"
        return
    fi
    mkdir -p "$dst"
    rsync "${BASE_FLAGS[@]}" --delete "$src/" "$dst/"
}

# Count files rsync would delete from dst when syncing src -> dst.
count_deletions_dir() {
    local src="$1" dst="$2"
    [[ ! -d "$src" || ! -d "$dst" ]] && { echo 0; return; }
    rsync -a --delete --dry-run --itemize-changes "$src/" "$dst/" 2>/dev/null \
        | grep -c '^\*deleting' || true
}

# Split "repo_rel:home_abs" into globals REPO_PATH and HOME_PATH.
split_pair() {
    REPO_PATH="$REPO_ROOT/${1%%:*}"
    HOME_PATH="${1#*:}"
}

# Count total pending deletions for a direction ("pull" or "push") across DIR_PAIRS.
# Prints a number and a human-readable per-dir breakdown on stderr.
count_pending_deletions() {
    local direction="$1"
    local total=0 n
    for pair in "${DIR_PAIRS[@]}"; do
        split_pair "$pair"
        if [[ "$direction" == "push" ]]; then
            n=$(count_deletions_dir "$REPO_PATH" "$HOME_PATH")
        else
            n=$(count_deletions_dir "$HOME_PATH" "$REPO_PATH")
        fi
        if [[ "$n" -gt 0 ]]; then
            echo "  $HOME_PATH <- $REPO_PATH: $n file(s) would be deleted" >&2
        fi
        total=$((total + n))
    done
    echo "$total"
}

# Warn about pending deletions and enforce --force threshold.
# Args: direction ("push"|"pull")
check_deletions() {
    local direction="$1"
    local n
    echo "Checking pending deletions..." >&2
    n=$(count_pending_deletions "$direction")
    if [[ "$n" -gt 0 ]]; then
        echo "Total files to be deleted: $n" >&2
        if [[ "$n" -gt "$DELETE_THRESHOLD" && $FORCE -ne 1 && $DRY -ne 1 ]]; then
            echo "Refusing to delete more than $DELETE_THRESHOLD files; pass --force to override." >&2
            exit 1
        fi
    else
        echo "No deletions pending." >&2
    fi
}

# Warn about stale files left behind by past FILE_PAIRS entries that have since
# been removed. FILE_PAIRS has no delete semantics, so users need a nudge.
warn_stale_files() {
    if [[ -f "$HOME/bin/ghimplement.sh" ]]; then
        echo "note: $HOME/bin/ghimplement.sh is no longer tracked; remove it with: rm $HOME/bin/ghimplement.sh" >&2
    fi
}

do_pull() {
    warn_stale_files
    for pair in "${FILE_PAIRS[@]}"; do
        split_pair "$pair"
        sync_file "$HOME_PATH" "$REPO_PATH"
    done
    for pair in "${DIR_PAIRS[@]}"; do
        split_pair "$pair"
        sync_dir "$HOME_PATH" "$REPO_PATH"
    done
}

do_push() {
    warn_stale_files
    for pair in "${FILE_PAIRS[@]}"; do
        split_pair "$pair"
        sync_file "$REPO_PATH" "$HOME_PATH"
    done
    for pair in "${DIR_PAIRS[@]}"; do
        split_pair "$pair"
        sync_dir "$REPO_PATH" "$HOME_PATH"
    done
}

do_diff() {
    local rc=0
    for pair in "${FILE_PAIRS[@]}"; do
        split_pair "$pair"
        if [[ -e "$REPO_PATH" || -e "$HOME_PATH" ]]; then
            diff -uN "$REPO_PATH" "$HOME_PATH" || rc=1
        fi
    done
    for pair in "${DIR_PAIRS[@]}"; do
        split_pair "$pair"
        if [[ -d "$REPO_PATH" || -d "$HOME_PATH" ]]; then
            diff -ruN "$REPO_PATH" "$HOME_PATH" || rc=1
        fi
    done
    return $rc
}

case "$CMD" in
    pull)
        check_deletions pull
        confirm "Pull tracked files into $REPO_ROOT/ (will mirror skills/agents/commands into repo, deleting extras in those dirs)?" || { echo "aborted"; exit 1; }
        do_pull
        ;;
    push)
        check_deletions push
        confirm "Push tracked files from $REPO_ROOT/ into \$HOME (will mirror skills/agents/commands into \$HOME/.claude, deleting extras in those dirs)?" || { echo "aborted"; exit 1; }
        do_push
        ;;
    diff|status)
        do_diff || true
        ;;
    *)
        die_usage "Unknown command: $CMD"
        ;;
esac
