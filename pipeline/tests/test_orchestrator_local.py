import pathlib
import re

import pytest

from conftest import MINIMAL_EVENTS
from pipeline.clients.fake import FakeClaudeClient
from pipeline.orchestrators.local import address_main, local_main, review_main

# Labels the triple review emits (default sonnet models).
_REVIEW_LABELS = {
    "review-code:holistic:sonnet",
    "review-code:detail:sonnet",
    "review-code:scope:sonnet",
}


class _ReviewCreatingClient(FakeClaudeClient):
    """FakeClaudeClient that writes the review output file when a review-code
    label is called. Extracts the output path from the prompt so it lands at
    exactly the path run_triple_review expects to exist after the workers finish."""

    def run(self, prompt, *, label, **kwargs):
        if label.startswith("review-code:"):
            m = re.search(r"`([^`]+\.md)`\s+is the canonical", prompt)
            assert m, f"regex did not match review-code prompt for label {label!r}"
            out = pathlib.Path(m.group(1))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("# Review\n\n## Findings\nNone.\n")
        return super().run(prompt, label=label, **kwargs)


def _common_patches(monkeypatch):
    """Apply monkeypatches shared across orchestrator smoke tests."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: "/fake/claude" if n == "claude" else None)
    monkeypatch.setattr(
        "pipeline.orchestrators.local.install_signal_handlers", lambda c: None
    )
    monkeypatch.setattr(
        "pipeline.stages.review_code.time.sleep", lambda s: None
    )


# ---------------------------------------------------------------------------
# local_main smoke test (--plan mode: skips plan, runs implement→review→address)
# ---------------------------------------------------------------------------

def test_local_main_plan_mode(tmp_path, monkeypatch):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\nDo stuff.\n")

    monkeypatch.chdir(tmp_path)
    _common_patches(monkeypatch)
    monkeypatch.setattr(
        "pipeline.orchestrators.local.resolve_session_dir", lambda: session_dir
    )
    # tmp_path is not a git repo → is_git=False; monkeypatch for clarity.
    monkeypatch.setattr("pipeline.orchestrators.local.in_git_repo", lambda: False)
    monkeypatch.setattr(
        "pipeline.orchestrators.local._load_core_principles", lambda: "Be good."
    )
    # Fake that implement produced changes (FakeClaudeClient won't create files).
    monkeypatch.setattr(
        "pipeline.stages.implement.changes_outside_git", lambda s, d: True
    )

    client = _ReviewCreatingClient(
        fixtures={
            "implement": MINIMAL_EVENTS,
            **{lbl: MINIMAL_EVENTS for lbl in _REVIEW_LABELS},
            "address-code": MINIMAL_EVENTS,
        }
    )

    result = local_main(["--plan", str(plan_file)], client=client)
    assert result == 0

    labels = [c.label for c in client.calls]
    assert labels[0] == "implement"
    assert set(labels[1:4]) == _REVIEW_LABELS
    assert labels[4] == "address-code"


# ---------------------------------------------------------------------------
# review_main smoke test
# ---------------------------------------------------------------------------

def test_review_main_calls_client(tmp_path, monkeypatch):
    _common_patches(monkeypatch)
    monkeypatch.setattr("pipeline.orchestrators.local.in_git_repo", lambda: False)

    client = _ReviewCreatingClient(
        fixtures={lbl: MINIMAL_EVENTS for lbl in _REVIEW_LABELS}
    )

    result = review_main(["--dir", str(tmp_path)], client=client)
    assert result == 0
    assert {c.label for c in client.calls} == _REVIEW_LABELS


# ---------------------------------------------------------------------------
# address_main smoke test
# ---------------------------------------------------------------------------

def test_address_main_calls_client(tmp_path, monkeypatch):
    for lens in ("holistic", "detail", "scope"):
        (tmp_path / f"review-code-{lens}-sonnet.md").write_text(
            f"# {lens.title()} Review\n\n## Findings\nNone.\n"
        )

    _common_patches(monkeypatch)
    monkeypatch.setattr("pipeline.orchestrators.local.in_git_repo", lambda: False)

    client = FakeClaudeClient(fixtures={"address-code": MINIMAL_EVENTS})

    result = address_main(["--dir", str(tmp_path)], client=client)
    assert result == 0
    assert len(client.calls) == 1
    assert client.calls[0].label == "address-code"
    assert client.calls[0].model == "sonnet"
