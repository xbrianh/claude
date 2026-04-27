import pathlib
import re
import shutil
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.clients.fake import FakeClaudeClient

# Shared minimal event stream used across test modules.
MINIMAL_EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "test-session-1"},
    {"type": "result", "subtype": "success"},
]

# Labels the triple review emits (default sonnet models). Shared so the
# orchestrator smoke tests and the GR_ID-isolation regression tests stay
# in sync if the label scheme changes.
REVIEW_LABELS = {
    "review-code:holistic:sonnet",
    "review-code:detail:sonnet",
    "review-code:scope:sonnet",
}


class ReviewCreatingClient(FakeClaudeClient):
    """FakeClaudeClient that writes the review output file when a review-code
    label is called. Extracts the output path from the prompt so it lands at
    exactly the path run_triple_review expects to exist after the workers
    finish. Shared between test_orchestrator_local and test_state_isolation."""

    def run(self, prompt, *, label, **kwargs):
        if label.startswith("review-code:"):
            m = re.search(r"`([^`]+\.md)`\s+is the canonical", prompt)
            assert m, f"regex did not match review-code prompt for label {label!r}"
            out = pathlib.Path(m.group(1))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("# Review\n\n## Findings\nNone.\n")
        return super().run(prompt, label=label, **kwargs)


def common_local_patches(monkeypatch):
    """Apply monkeypatches shared across local-orchestrator smoke tests."""
    monkeypatch.setattr(shutil, "which", lambda n: "/fake/claude" if n == "claude" else None)
    monkeypatch.setattr(
        "pipeline.orchestrators.local.install_signal_handlers", lambda c: None
    )
    monkeypatch.setattr("pipeline.stages.review_code.time.sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _isolate_gr_id(monkeypatch):
    # If the test process inherits GR_ID from a parent gremlin (e.g. an
    # implement stage running `python -m pytest`), pipeline.state.set_stage
    # would shell out to set-stage.sh against the parent's state.json and
    # corrupt its `stage` / `sub_stage` fields. Default-deny here; tests that
    # genuinely need GR_ID set it explicitly via monkeypatch.setenv, which
    # overrides this delenv.
    monkeypatch.delenv("GR_ID", raising=False)
