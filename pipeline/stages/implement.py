"""Local implement stage.

Captures pre-impl HEAD/sentinel state, renders
``pipeline/prompts/implement_local.md`` with the core-principles excerpt,
the plan text, and the commit-instruction blob, runs ``claude -p``, then
enforces the spec invariant that an empty implementation must never flow
into code review.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Optional

from ..clients.claude import ClaudeClient
from ..git import git_head

PROMPT_TEMPLATE_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "implement_local.md"


def changes_outside_git(sentinel: pathlib.Path, session_dir: pathlib.Path) -> bool:
    try:
        threshold = sentinel.stat().st_mtime
    except Exception:
        return False
    cwd = pathlib.Path(".").resolve()
    try:
        session_resolved = session_dir.resolve()
    except Exception:
        session_resolved = session_dir
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        dp = pathlib.Path(dirpath)
        try:
            dp_resolved = dp.resolve()
            if dp_resolved == session_resolved or session_resolved in dp_resolved.parents:
                dirnames[:] = []
                continue
        except Exception:
            pass
        for f in filenames:
            fp = dp / f
            try:
                if fp.stat().st_mtime > threshold:
                    return True
            except Exception:
                continue
    return False


def run_implement_stage(
    *,
    client: ClaudeClient,
    impl_model: str,
    plan_file: pathlib.Path,
    plan_text: str,
    core_principles: str,
    session_dir: pathlib.Path,
    is_git: bool,
) -> None:
    pre_head = ""
    pre_sentinel: Optional[pathlib.Path] = None
    if is_git:
        pre_head = git_head()
    else:
        pre_sentinel = session_dir / ".pre-impl"
        pre_sentinel.touch()

    # The commit message references `plan.md` (basename) rather than the
    # absolute session-dir path, which is user-specific and would end up in
    # git history otherwise.
    impl_commit_instr = "."
    if is_git:
        impl_commit_instr = (
            ", stage the changed files by name and create a single git commit "
            "with a clear message that references the implementation plan "
            "(refer to it as `plan.md` in the commit message, not by absolute "
            "path). Do NOT create any meta/scaffolding files in the repo — no "
            "`.claude-workflow/` directory, no `plan.md`, no review docs, no "
            "notes-to-self. Do not push."
        )

    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        core_principles=core_principles,
        plan_text=plan_text,
        impl_commit_instr=impl_commit_instr,
    )
    client.run(
        prompt,
        label="implement",
        model=impl_model,
        raw_path=session_dir / "stream-implement.jsonl",
    )

    # Spec invariant: an empty implementation must never flow into code review.
    if is_git:
        post_head = git_head()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        if post_head == pre_head and not porcelain.stdout.strip():
            raise RuntimeError("implementation stage produced no changes; aborting")
    else:
        assert pre_sentinel is not None
        if not changes_outside_git(pre_sentinel, session_dir):
            raise RuntimeError("implementation stage produced no changes; aborting")
