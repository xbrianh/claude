"""ghreview stage for the gh pipeline."""

from __future__ import annotations

import pathlib
from typing import Optional

from ..clients.claude import ClaudeClient
from ..state import check_bail


def run_ghreview_stage(
    *,
    client: ClaudeClient,
    model: Optional[str],
    pr_url: str,
    artifacts_dir: pathlib.Path,
) -> None:
    """Run /ghreview. Calls check_bail after completion."""
    client.run(
        f"/ghreview {pr_url}",
        label="ghreview",
        model=model,
        raw_path=artifacts_dir / "stream-ghreview.jsonl",
    )
    check_bail("/ghreview")
