"""GitHub CLI helpers used by the gh orchestrator and gh stages.

All functions that call ``gh`` or parse stream-json events for GitHub URLs
live here so the stage modules stay focused on orchestration.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Callable, List, Optional


def get_repo() -> str:
    """Return the current repo's ``owner/name`` via ``gh repo view``."""
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"not in a gh-recognized repo: {r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout.strip()


def extract_gh_url(
    events: List[dict],
    url_pattern: str,
    cmd_pattern: str,
    label: str,
) -> str:
    """Extract a GitHub URL from a claude stream-json event list.

    Searches ``Bash`` tool_use events whose ``command`` matches ``cmd_pattern``
    (regex), finds their paired ``tool_result`` events, and returns the last
    URL matching ``url_pattern`` found in those results. Falls back to scanning
    the final ``result`` event's text if no tool_result match is found.

    Raises ``RuntimeError`` when no URL is found.
    """
    # Collect tool_use IDs for Bash commands matching cmd_pattern.
    matching_ids: set = set()
    for evt in events:
        if evt.get("type") != "assistant":
            continue
        for c in (evt.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if (
                c.get("type") == "tool_use"
                and c.get("name") == "Bash"
                and re.search(cmd_pattern, (c.get("input") or {}).get("command") or "")
            ):
                matching_ids.add(c.get("id"))

    # Scan tool_result events for those IDs.
    last_tool_url: Optional[str] = None
    for evt in events:
        if evt.get("type") != "user":
            continue
        for c in (evt.get("message") or {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            if c.get("tool_use_id") not in matching_ids:
                continue
            body = c.get("content")
            if isinstance(body, list):
                text = "\n".join((p.get("text") or "") for p in body if isinstance(p, dict))
            elif isinstance(body, str):
                text = body
            else:
                text = str(body) if body is not None else ""
            matches = re.findall(url_pattern, text)
            if matches:
                last_tool_url = matches[-1]

    if last_tool_url:
        return last_tool_url

    # Fallback: scan the last result event's text.
    for evt in reversed(events):
        if evt.get("type") == "result":
            result_text = evt.get("result") or ""
            matches = re.findall(url_pattern, result_text)
            if matches:
                return matches[-1]

    raise RuntimeError(f"failed to extract {label} URL from claude output events")


def check_copilot_review(repo: str, pr_num: str) -> Optional[str]:
    """Return the first non-PENDING Copilot review state, or None if not ready."""
    r = subprocess.run(
        [
            "gh", "api", f"repos/{repo}/pulls/{pr_num}/reviews",
            "--jq", '.[] | select(.user.login | test("[Cc]opilot")) | .state',
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    lines = [ln for ln in r.stdout.splitlines() if ln and ln != "PENDING"]
    return lines[0] if lines else None
