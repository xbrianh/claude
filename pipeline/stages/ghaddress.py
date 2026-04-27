"""ghaddress stage for the gh pipeline."""

from __future__ import annotations

import pathlib
from typing import Optional

from ..clients.claude import ClaudeClient
from ..state import check_bail


def run_ghaddress_stage(
    *,
    client: ClaudeClient,
    model: Optional[str],
    pr_url: str,
    artifacts_dir: pathlib.Path,
) -> None:
    """Run /ghaddress on the PR. Calls check_bail after completion."""
    client.run(
        f"/ghaddress {pr_url}",
        label="ghaddress",
        model=model,
        raw_path=artifacts_dir / "stream-ghaddress.jsonl",
    )
    check_bail("/ghaddress")
