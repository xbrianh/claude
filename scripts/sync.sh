#!/usr/bin/env bash
# Bidirectional sync between the user's home config and this repo.
# Tracks:
#   ~/.claude/CLAUDE.md, ~/.claude/settings.json
#   ~/.claude/skills/, ~/.claude/agents/, ~/.claude/commands/
#   ~/bin/ghimplement.sh
# Excludes: settings.local.json (local-only)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_DIR="$HOME/.claude"

# "<repo-relative-path>:<absolute-home-path>"
FILE_PAIRS=(
    "CLAUDE.md:$CLAUDE_DIR/CLAUDE.md"
    "settings.json:$CLAUDE_DIR/settings.json"
    "bin/ghimplement.sh:$HOME/bin/ghimplement.sh"
)
DIR_PAIRS=(
    "skills:$CLAUDE_DIR/skills"
    "agents:$CLAUDE_DIR/agents"
    "commands:$CLAUDE_DIR/commands"
)

usage() {
    cat <<EOF
Usage: $(basename "$0") <command> [options]

Commands:
  pull        Copy from ~/.claude/ into this repo (mirror: deletes extras in repo)
  push        Copy from this repo into ~/.claude/ (mirror: deletes extras in ~/.claude)
  diff        Show differences between repo and ~/.claude/
  status      Alias for diff

Options:
  -y, --yes       Skip confirmation prompt
  -n, --dry-run   Show what would change without writing
EOF
    exit 1
}

[[ $# -lt 1 ]] && usage

CMD="$1"; shift || true
YES=0
DRY=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)     YES=1 ;;
        -n|--dry-run) DRY=1 ;;
        -h|--help)    usage ;;
        *) echo "Unknown option: $arg" >&2; usage ;;
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

# Split "repo_rel:home_abs" into globals REPO_PATH and HOME_PATH.
split_pair() {
    REPO_PATH="$REPO_ROOT/${1%%:*}"
    HOME_PATH="${1#*:}"
}

do_pull() {
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
            diff -u "$REPO_PATH" "$HOME_PATH" || rc=1
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
        confirm "Pull tracked files into $REPO_ROOT/ (will delete extras in repo dirs)?" || { echo "aborted"; exit 1; }
        do_pull
        ;;
    push)
        confirm "Push tracked files from $REPO_ROOT/ into \$HOME (will delete extras in home dirs)?" || { echo "aborted"; exit 1; }
        do_push
        ;;
    diff|status)
        do_diff || true
        ;;
    *)
        usage ;;
esac
