#!/usr/bin/env bash
# localland.sh — git operations for the /localland skill.
# Subcommands called in sequence by Claude:
#   --check              <id>            Validate preconditions; print branch= and plan= on stdout.
#   --squash             <id>            Stage the workflow's commits via git merge --squash.
#   --cleanup            <id>            Delete the workflow branch and state directory.
#   --gh-preflight       <id> <pr-branch> Pre-flight checks for --gh mode (remote, auth, branch collision).
#   --gh-squash          <id> <pr-branch> Create PR branch off main and squash-merge the workflow branch onto it.
set -euo pipefail

STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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

die() { echo "error: $*" >&2; exit 1; }

[[ $# -ge 2 ]] || die "usage: localland.sh --check|--squash|--cleanup|--gh-preflight|--gh-squash <workflow-id> [pr-branch]"
MODE="$1"
WF_ID="$2"

[[ "$WF_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid workflow id: $WF_ID"

WF_DIR="$STATE_ROOT/$WF_ID"
STATE_FILE="$WF_DIR/state.json"

case "$MODE" in
# ---------------------------------------------------------------------------
--check)
    [[ -f "$STATE_FILE" ]] || die "no state.json found for workflow '$WF_ID' (looked in $WF_DIR)"

    command -v jq >/dev/null 2>&1 || die "jq is required but not found on PATH"

    # Refuse non-git workflows (cp-a setup has no branch to squash).
    setup_kind=$(jq -r '.setup_kind // "unknown"' "$STATE_FILE")
    [[ "$setup_kind" == "worktree-branch" ]] \
        || die "workflow '$WF_ID' is not a git workflow (setup_kind=$setup_kind); nothing to squash"

    # Workflow must be finished, not still running.
    live=$(liveness_of_state_file "$STATE_FILE")
    [[ "$live" == "dead:finished" ]] \
        || die "workflow '$WF_ID' is not finished (liveness=$live); wait for it to complete before landing"

    branch=$(jq -r '.branch // ""' "$STATE_FILE")
    [[ -n "$branch" ]] || die "state.json for '$WF_ID' has no branch field"

    # Working tree must be clean.
    dirty=$(git status --porcelain 2>/dev/null || true)
    [[ -z "$dirty" ]] || die "working tree is not clean; commit or stash changes before running /localland"

    # Cannot land onto the workflow branch itself.
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    [[ "$current_branch" != "$branch" ]] \
        || die "currently on the workflow branch '$branch'; switch to your target branch first"

    # Workflow branch must exist.
    git show-ref --verify --quiet "refs/heads/$branch" \
        || die "workflow branch '$branch' does not exist; it may have already been cleaned up"

    # There must be at least one commit above the merge-base.
    merge_base=$(git merge-base HEAD "$branch" 2>/dev/null) \
        || die "could not compute merge-base between HEAD and '$branch'"
    commit_count=$(git rev-list --count "$merge_base..$branch" 2>/dev/null || echo 0)
    [[ "$commit_count" -ge 1 ]] \
        || die "workflow branch '$branch' has no commits above the merge-base; nothing to land"

    plan_path="$WF_DIR/artifacts/plan.md"
    [[ -r "$plan_path" ]] || die "plan.md not readable at $plan_path"

    echo "branch=$branch"
    echo "plan=$plan_path"
    ;;

# ---------------------------------------------------------------------------
--squash)
    [[ -f "$STATE_FILE" ]] || die "no state.json found for workflow '$WF_ID'"
    command -v jq >/dev/null 2>&1 || die "jq is required but not found on PATH"

    setup_kind=$(jq -r '.setup_kind // "unknown"' "$STATE_FILE")
    [[ "$setup_kind" == "worktree-branch" ]] \
        || die "workflow '$WF_ID' is not a git workflow (setup_kind=$setup_kind); nothing to squash"

    branch=$(jq -r '.branch // ""' "$STATE_FILE")
    [[ -n "$branch" ]] || die "state.json for '$WF_ID' has no branch field"

    echo "Squash-merging $branch onto $(git rev-parse --abbrev-ref HEAD)..."
    if ! git merge --squash "$branch" 2>&1; then
        git reset --hard HEAD 2>/dev/null || true
        git clean -fd 2>/dev/null || true
        die "git merge --squash failed (conflicts); working tree restored — resolve conflicts manually or re-run /localland"
    fi

    echo "--- staged diff summary ---"
    git diff --cached --stat
    ;;

# ---------------------------------------------------------------------------
--cleanup)
    command -v jq >/dev/null 2>&1 || die "jq is required but not found on PATH"

    # Best-effort branch deletion — succeed even if already gone.
    if [[ -f "$STATE_FILE" ]]; then
        branch=$(jq -r '.branch // ""' "$STATE_FILE")
        if [[ -n "$branch" ]]; then
            if git show-ref --verify --quiet "refs/heads/$branch" 2>/dev/null; then
                git worktree prune 2>/dev/null || true
                git branch -D "$branch" && echo "deleted branch $branch" \
                    || echo "warning: could not delete branch $branch" >&2
            else
                echo "branch $branch already gone"
            fi
        fi
    fi

    if [[ -d "$WF_DIR" ]]; then
        rm -rf "$WF_DIR"
        echo "removed $WF_DIR"
    else
        echo "state directory already gone: $WF_DIR"
    fi
    ;;

# ---------------------------------------------------------------------------
--gh-preflight)
    PR_BRANCH="${3:-}"
    [[ -n "$PR_BRANCH" ]] || die "usage: localland.sh --gh-preflight <id> <pr-branch>"

    # Must have a remote named 'origin' (push and remote branch checks both target origin).
    git remote get-url origin >/dev/null 2>&1 \
        || die "--gh requires a remote named 'origin'; none found (remotes: $(git remote | tr '\n' ' '))"

    # gh CLI must be authenticated.
    gh auth status >/dev/null 2>&1 || die "gh is not authenticated; run 'gh auth login' and retry"

    # Fetch origin so origin/main reflects the true remote state.
    fetch_err=$(git fetch origin 2>&1) \
        || die "git fetch origin failed — check your network and remote configuration\n${fetch_err}"
    git show-ref --verify --quiet refs/remotes/origin/main 2>/dev/null \
        || die "origin/main does not exist after fetch — ensure the remote has a 'main' branch"

    # PR branch must not already exist locally.
    if git show-ref --verify --quiet "refs/heads/$PR_BRANCH" 2>/dev/null; then
        die "branch '$PR_BRANCH' already exists locally; delete it or choose a different name"
    fi

    # PR branch must not already exist remotely.
    if git ls-remote --exit-code origin "refs/heads/$PR_BRANCH" >/dev/null 2>&1; then
        die "branch '$PR_BRANCH' already exists on origin; delete it or choose a different name"
    fi

    echo "preflight ok: remote present, gh authenticated, '$PR_BRANCH' is free"
    ;;

# ---------------------------------------------------------------------------
--gh-squash)
    PR_BRANCH="${3:-}"
    [[ -n "$PR_BRANCH" ]] || die "usage: localland.sh --gh-squash <id> <pr-branch>"

    [[ -f "$STATE_FILE" ]] || die "no state.json found for workflow '$WF_ID'"
    command -v jq >/dev/null 2>&1 || die "jq is required but not found on PATH"

    setup_kind=$(jq -r '.setup_kind // "unknown"' "$STATE_FILE")
    [[ "$setup_kind" == "worktree-branch" ]] \
        || die "workflow '$WF_ID' is not a git workflow (setup_kind=$setup_kind); nothing to squash"

    branch=$(jq -r '.branch // ""' "$STATE_FILE")
    [[ -n "$branch" ]] || die "state.json for '$WF_ID' has no branch field"

    original_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) \
        || die "could not determine current branch"

    # Create PR branch off origin/main.
    if ! git checkout -b "$PR_BRANCH" origin/main 2>&1; then
        die "could not create branch '$PR_BRANCH' off origin/main; ensure 'origin/main' exists and the working tree is clean"
    fi

    # Squash-merge the workflow branch onto the PR branch.
    echo "Squash-merging $branch onto $PR_BRANCH..."
    if ! git merge --squash "$branch" 2>&1; then
        git checkout "$original_branch" 2>/dev/null || true
        git branch -D "$PR_BRANCH" 2>/dev/null || true
        die "git merge --squash failed (conflicts); restored to '$original_branch' and deleted '$PR_BRANCH'"
    fi

    echo "--- staged diff summary ---"
    git diff --cached --stat
    ;;

# ---------------------------------------------------------------------------
*)
    die "unknown subcommand '$MODE'; expected --check, --squash, --cleanup, --gh-preflight, or --gh-squash"
    ;;
esac
