"""ghreview + parallel scope-reviewer stage for the gh pipeline.

Runs ``/ghreview <pr_url>`` and a scope reviewer in two threads concurrently,
mirroring the bash ``&`` / ``wait`` pattern in ``ghgremlin.sh:609-637``.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import threading
from typing import Optional

from ..clients.claude import ClaudeClient
from ..state import check_bail

PROMPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "prompts"
LENSES_DIR = PROMPTS_DIR / "lenses"


def _get_pr_diff(pr_url: str) -> str:
    r = subprocess.run(
        ["gh", "pr", "diff", pr_url],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"gh pr diff failed: {r.stderr.strip()}")
    return r.stdout


def run_ghreview_stage(
    *,
    client: ClaudeClient,
    model: Optional[str],
    pr_url: str,
    issue_body: str,
    artifacts_dir: pathlib.Path,
) -> None:
    """Run /ghreview and scope reviewer in parallel. Raises if either fails.
    Calls check_bail after both complete so a reviewer_requested_changes bail
    from /ghreview halts the pipeline."""
    scope_lens_path = LENSES_DIR / "scope.md"
    if not scope_lens_path.exists() or scope_lens_path.stat().st_size == 0:
        raise FileNotFoundError(f"missing or empty lens file: {scope_lens_path}")
    scope_lens = scope_lens_path.read_text(encoding="utf-8")

    pr_diff = _get_pr_diff(pr_url)
    scope_tmp = artifacts_dir / "scope-review-pr.tmp"

    scope_template = (PROMPTS_DIR / "scope_review_pr.md").read_text(encoding="utf-8")
    scope_prompt = scope_template.format(
        scope_lens=scope_lens,
        issue_body=issue_body,
        pr_diff=pr_diff,
        scope_review_tmp=str(scope_tmp),
        pr_url=pr_url,
    )

    errors: dict = {}

    def run_ghreview() -> None:
        try:
            client.run(
                f"/ghreview {pr_url}",
                label="ghreview",
                model=model,
                raw_path=artifacts_dir / "stream-ghreview.jsonl",
            )
        except Exception as exc:
            errors["ghreview"] = exc
            sys.stderr.write(f"/ghreview failed: {exc}\n")
            sys.stderr.flush()

    def run_scope() -> None:
        try:
            client.run(
                scope_prompt,
                label="scope-review-pr",
                model=model,
                raw_path=artifacts_dir / "stream-scope-review-pr.jsonl",
            )
        except Exception as exc:
            errors["scope"] = exc
            sys.stderr.write(f"scope reviewer failed: {exc}\n")
            sys.stderr.flush()

    t_ghreview = threading.Thread(target=run_ghreview, daemon=True)
    t_scope = threading.Thread(target=run_scope, daemon=True)

    t_ghreview.start()
    t_scope.start()

    t_ghreview.join()
    if "ghreview" in errors:
        t_scope.join(timeout=5)
        raise RuntimeError(f"/ghreview failed: {errors['ghreview']}")

    t_scope.join()
    if "scope" in errors:
        raise RuntimeError(f"scope reviewer failed: {errors['scope']}")

    check_bail("/ghreview")
