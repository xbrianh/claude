"""Git helpers used across pipeline stages."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from typing import Optional, Union


def in_git_repo() -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def git_head() -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


# ---------------------------------------------------------------------------
# ghgremlin impl-handoff branch lifecycle (Phase 3)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PreImplState:
    """Git state captured before the implement stage runs."""
    head: str
    branch: str  # empty string when HEAD is detached (the launch.sh worktree default)


@dataclasses.dataclass
class EmptyImpl:
    """HEAD unchanged and worktree clean — no implementation work produced."""


@dataclasses.dataclass
class DirtyOnly:
    """HEAD unchanged but worktree has uncommitted changes."""


@dataclasses.dataclass
class HeadAdvanced:
    """HEAD advanced fast-forward from pre-impl state."""
    commit_count: int


@dataclasses.dataclass
class DivergentHead:
    """HEAD changed but is not a fast-forward of the pre-impl HEAD."""
    pre_head: str
    post_head: str


ImplOutcome = Union[EmptyImpl, DirtyOnly, HeadAdvanced, DivergentHead]


def _git(args: list, *, cwd: Optional[str] = None, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + list(args), cwd=cwd, **kwargs)


def record_pre_impl_state(cwd: Optional[str] = None) -> PreImplState:
    """Capture HEAD commit and symbolic branch ref before running the implement stage."""
    head_r = _git(["rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=False)
    head = head_r.stdout.strip() if head_r.returncode == 0 else ""
    if not head:
        raise RuntimeError("could not resolve HEAD before implement stage")
    branch_r = _git(
        ["symbolic-ref", "--short", "HEAD"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    branch = branch_r.stdout.strip() if branch_r.returncode == 0 else ""
    return PreImplState(head=head, branch=branch)


def classify_impl_outcome(pre: PreImplState, cwd: Optional[str] = None) -> ImplOutcome:
    """Classify post-implement git state into one of the four outcome types."""
    head_r = _git(["rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=False)
    post_head = head_r.stdout.strip() if head_r.returncode == 0 else ""

    if post_head and post_head != pre.head:
        ancestor_r = _git(
            ["merge-base", "--is-ancestor", pre.head, post_head],
            cwd=cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if ancestor_r.returncode == 0:
            count_r = _git(
                ["rev-list", "--count", f"{pre.head}..HEAD"],
                cwd=cwd, capture_output=True, text=True, check=False,
            )
            count = int(count_r.stdout.strip() or "0") if count_r.returncode == 0 else 0
            return HeadAdvanced(commit_count=count)
        return DivergentHead(pre_head=pre.head, post_head=post_head)

    status_r = _git(["status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=False)
    if status_r.stdout.strip():
        return DirtyOnly()
    return EmptyImpl()


def create_handoff_branch(pre: PreImplState, cwd: Optional[str] = None) -> str:
    """Create a ghgremlin-impl-handoff-<pid> branch at current HEAD.

    Returns the branch name. Raises if the branch already exists or git switch fails.
    The PID suffix scopes the name to this process so concurrent gremlins in the
    same repo don't collide.
    """
    handoff = f"ghgremlin-impl-handoff-{os.getpid()}"
    check_r = _git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{handoff}"],
        cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if check_r.returncode == 0:
        raise RuntimeError(f"hand-off branch {handoff} already exists; refusing to clobber")
    switch_r = _git(["switch", "-c", handoff], cwd=cwd, capture_output=True, text=True, check=False)
    if switch_r.returncode != 0:
        raise RuntimeError(
            f"could not create hand-off branch {handoff}: {switch_r.stderr.strip()}"
        )
    return handoff


def reset_pre_branch(pre: PreImplState, cwd: Optional[str] = None) -> None:
    """Reset the pre-impl branch ref back to pre.head.

    No-op when HEAD was detached at impl start (pre.branch is empty), which is
    the normal case under launch.sh's detached worktree. Under direct invocation
    from a named branch this resets that branch to PRE_HEAD so implementation
    commits don't leak onto the chain's start ref (intentional destructive reset,
    documented in the DESIGN.md ghgremlin impl-handoff lifecycle section).
    """
    if not pre.branch:
        return
    r = _git(
        ["branch", "-f", pre.branch, pre.head],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"could not reset {pre.branch} to {pre.head}: {r.stderr.strip()}"
        )


def sweep_stale_handoff_branches(handoff_branch: str, cwd: Optional[str] = None) -> None:
    """Delete ghgremlin-impl-handoff-* branches from prior failed runs that are
    already merged into HEAD. Leaves divergent ones in place with a warning.

    Called after create_handoff_branch so HEAD is on the new handoff branch and
    git branch -d (which refuses to delete the current branch) can safely delete
    stale branches that are ancestors of the current HEAD.
    """
    list_r = _git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/ghgremlin-impl-handoff-*"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if list_r.returncode != 0:
        return
    for stale in list_r.stdout.splitlines():
        stale = stale.strip()
        if not stale or stale == handoff_branch:
            continue
        ancestor_r = _git(
            ["merge-base", "--is-ancestor", stale, "HEAD"],
            cwd=cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if ancestor_r.returncode == 0:
            _git(["branch", "-d", stale], cwd=cwd,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            sys.stderr.write(
                f"warning: leaving divergent hand-off branch {stale} in place "
                "(unique commits would be lost)\n"
            )
            sys.stderr.flush()
