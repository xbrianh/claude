"""Git helpers used across pipeline stages.

Phase 1 only needs the cwd-based ``in_git_repo`` and ``git_head`` checks
the local pipeline uses for its empty-implementation invariant. The
ghgremlin impl-handoff branch lifecycle (``record_pre_impl_state``,
``classify_impl_outcome``, etc.) lands in Phase 3.
"""

from __future__ import annotations

import subprocess


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
