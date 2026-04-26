"""Address-code stage.

Globs for the three review files in ``session_dir``, builds the address
prompt from ``pipeline/prompts/address_code.md``, and invokes ``claude -p``.
On any failure (missing/ambiguous review files, malformed model name in a
filename, claude crash) records ``bail_class=other`` so headless rescue
sees a usable bail marker rather than having to grep the log.
"""

from __future__ import annotations

import glob
import os
import pathlib
import re
from typing import Dict

from ..clients.claude import ClaudeClient
from ..state import emit_bail

MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PROMPT_TEMPLATE_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "address_code.md"


def _model_from(path: pathlib.Path, lens: str) -> str:
    """Extract the reviewer model name from ``review-code-<lens>-<model>.md``.

    Validate against MODEL_RE so a malformed filename (e.g.
    ``review-code-holistic-.md``, or one with unexpected characters)
    fails loudly instead of producing an empty/garbled prompt label.
    """
    stem = path.stem  # review-code-<lens>-<model>
    prefix = f"review-code-{lens}-"
    model = stem[len(prefix):] if stem.startswith(prefix) else ""
    if not model or not MODEL_RE.match(model):
        raise ValueError(
            f"cannot extract a valid model name from review file: {path.name}"
        )
    return model


def run_address_code_stage(
    *,
    client: ClaudeClient,
    session_dir: pathlib.Path,
    address_model: str,
    is_git: bool,
) -> None:
    """Execute the address-code stage. Emits bail_class=other on failure
    when running under a gremlin (no-op otherwise) — including failures
    during glob/validation before claude is spawned. Shared by the
    orchestrator and /localaddress."""
    # Outer try/except so *any* stage failure (missing or ambiguous review
    # files, invalid model in a filename, read errors, or the claude -p
    # subprocess itself) records a bail marker before the exception
    # propagates. Without this wrapping the pre-claude failure paths would
    # exit without ever calling emit_bail, and headless rescue would have
    # no bail_class to act on.
    try:
        review_files: Dict[str, pathlib.Path] = {}
        for lens in ("holistic", "detail", "scope"):
            matches = sorted(glob.glob(str(session_dir / f"review-code-{lens}-*.md")))
            if not matches:
                raise FileNotFoundError(
                    f"no review-code-{lens}-*.md file found in {session_dir}"
                )
            if len(matches) > 1:
                raise RuntimeError(
                    f"multiple review-code-{lens}-*.md files in {session_dir}: "
                    f"{', '.join(matches)}"
                )
            review_files[lens] = pathlib.Path(matches[0])

        model_a = _model_from(review_files["holistic"], "holistic")
        model_b = _model_from(review_files["detail"], "detail")
        model_c = _model_from(review_files["scope"], "scope")

        text_a = review_files["holistic"].read_text(encoding="utf-8")
        text_b = review_files["detail"].read_text(encoding="utf-8")
        text_c = review_files["scope"].read_text(encoding="utf-8")

        address_commit_instr = ""
        if is_git:
            address_commit_instr = (
                "After making all fixes, stage the changed files by name and "
                "create a single git commit titled 'Address review feedback' whose "
                "body references all three review files. Do not push."
            )

        # Only attached when running under a gremlin so direct invocations
        # (no GR_ID) don't see prompt instructions for a helper they can't
        # usefully invoke.
        bail_section = ""
        if os.environ.get("GR_ID"):
            bail_section = """

If a finding asks you to change something that touches secrets/credentials, or you decline to address one or more findings for any other reason that should halt automated recovery, run the bail helper before finishing:
  - `~/.claude/skills/_bg/set-bail.sh "$GR_ID" secrets "<one-line reason>"` if the blocked finding touches secrets.
  - `~/.claude/skills/_bg/set-bail.sh "$GR_ID" other "<one-line reason>"` for any other reason you cannot proceed.
Do not call this helper if you successfully addressed every actionable finding.
"""

        template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
        address_prompt = template.format(
            model_a=model_a,
            model_b=model_b,
            model_c=model_c,
            text_a=text_a,
            text_b=text_b,
            text_c=text_c,
            address_commit_instr=address_commit_instr,
            bail_section=bail_section,
        )
        client.run(
            address_prompt,
            label="address-code",
            model=address_model,
            raw_path=session_dir / "stream-address.jsonl",
        )
    except (SystemExit, Exception) as exc:
        emit_bail("other", f"address-code stage failed: {exc}"[:200])
        raise
