"""Commit-and-open-PR stage for the gh pipeline.

Selects the correct action clause from ``pipeline/prompts/`` based on the
implement stage's classified outcome, assembles the full commit-pr prompt, and
runs claude with ``--resume <impl_session_id>`` so the same claude session that
did the implementation also creates the branch and opens the PR.
"""

from __future__ import annotations

import pathlib
from typing import Optional

from ..clients.claude import ClaudeClient, CompletedRun
from ..git import DirtyOnly, HeadAdvanced, ImplOutcome, PreImplState
from ..gh_utils import extract_gh_url

PROMPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "prompts"


def _load(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def run_commit_pr_stage(
    *,
    client: ClaudeClient,
    model: Optional[str],
    impl_session_id: Optional[str],
    pre_state: PreImplState,
    outcome: ImplOutcome,
    handoff_branch: str,
    issue_num: str,
    session_dir: pathlib.Path,
) -> str:
    """Build the commit-pr prompt, run claude (resuming the implement session),
    extract and return the PR URL from the event stream."""

    # Action clause: which of the three shapes the agent should take.
    if isinstance(outcome, HeadAdvanced):
        # Check whether the worktree is also dirty.
        import subprocess
        status_r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        worktree_dirty = bool(status_r.stdout.strip())
        if worktree_dirty:
            action_clause = _load("commit_pr_handoff_dirty.md").format(
                handoff_branch=handoff_branch,
                commit_count=outcome.commit_count,
                pre_head=pre_state.head,
            )
        else:
            action_clause = _load("commit_pr_handoff_clean.md").format(
                handoff_branch=handoff_branch,
                commit_count=outcome.commit_count,
                pre_head=pre_state.head,
            )
    else:
        # DirtyOnly: create branch + commit + push from scratch
        action_clause = _load("commit_pr_fresh.md")

    # Branch-name and closes-link clauses depend on whether we have an issue.
    if issue_num:
        branch_clause = f"Name the branch 'issue-{issue_num}-<short-slug>'."
        closes_clause = (
            f"End the commit message with 'Closes #{issue_num}' and include "
            f"'Closes #{issue_num}' in the PR body."
        )
    else:
        branch_clause = "Name the branch with a short descriptive slug derived from the plan title."
        closes_clause = (
            "Do NOT include any 'Closes #N' or 'Fixes #N' link in the commit "
            "message or PR body."
        )

    prompt = (
        f"{action_clause} {branch_clause} {closes_clause} "
        "Print ONLY the PR URL on the final line of your response."
    )

    completed: CompletedRun = client.run(
        prompt,
        label="commit-pr",
        model=model,
        resume_session=impl_session_id,
        raw_path=session_dir / "stream-commit-pr.jsonl",
        capture_events=True,
    )

    events = completed.events or []
    pr_url = extract_gh_url(
        events,
        url_pattern=r"https://github\.com/[^ )]+/pull/[0-9]+",
        cmd_pattern=r"gh pr create",
        label="PR",
    )
    return pr_url
