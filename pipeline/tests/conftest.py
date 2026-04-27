import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Shared minimal event stream used across test modules.
MINIMAL_EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "test-session-1"},
    {"type": "result", "subtype": "success"},
]


@pytest.fixture(autouse=True)
def _isolate_gr_id(monkeypatch):
    # If the test process inherits GR_ID from a parent gremlin (e.g. an
    # implement stage running `python -m pytest`), pipeline.state.set_stage
    # would shell out to set-stage.sh against the parent's state.json and
    # corrupt its `stage` / `sub_stage` fields. Default-deny here; tests that
    # genuinely need GR_ID set it explicitly via monkeypatch.setenv, which
    # overrides this delenv.
    monkeypatch.delenv("GR_ID", raising=False)
