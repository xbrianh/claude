"""Regression tests for the GR_ID-leakage bug captured on PR #140.

When `pytest` runs as a subprocess of an implement-stage gremlin, GR_ID is
inherited. Without isolation, pipeline.state.set_stage shells out to
set-stage.sh against the parent gremlin's state.json, corrupting its
`stage` and `sub_stage` fields — observable in `/gremlins --watch` and
dangerous for any rescue-flow logic that branches on `stage`.

The fix is the autouse `_isolate_gr_id` fixture in conftest.py, which
delenv's GR_ID before every test. These tests verify both layers:

- test_autouse_isolate_gr_id_unsets_gr_id: direct check that the autouse
  fixture is in effect and GR_ID is unset inside the test body.
- test_orchestrators_do_not_clobber_external_state: end-to-end check that
  running the local orchestrator entry points does not write to a
  pre-existing parent gremlin state.json under XDG_STATE_HOME.
"""

import json
import os
import pathlib
import re
import shutil

from conftest import MINIMAL_EVENTS
from pipeline.clients.fake import FakeClaudeClient
from pipeline.orchestrators.local import address_main, local_main, review_main


_REVIEW_LABELS = {
    "review-code:holistic:sonnet",
    "review-code:detail:sonnet",
    "review-code:scope:sonnet",
}


def test_autouse_isolate_gr_id_unsets_gr_id():
    # If pytest is invoked from inside a gremlin's implement stage, GR_ID is
    # inherited from the parent. The autouse fixture in conftest.py must
    # remove it; otherwise pipeline.state.set_stage will shell out to
    # set-stage.sh against the parent's state.json.
    assert os.environ.get("GR_ID") is None


class _ReviewCreatingClient(FakeClaudeClient):
    """Mirrors test_orchestrator_local._ReviewCreatingClient: writes a stub
    review file when a review-code label is invoked, so the triple-review
    wait-for-files loop completes."""

    def run(self, prompt, *, label, **kwargs):
        if label.startswith("review-code:"):
            m = re.search(r"`([^`]+\.md)`\s+is the canonical", prompt)
            assert m, f"regex did not match review-code prompt for label {label!r}"
            out = pathlib.Path(m.group(1))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("# Review\n\n## Findings\nNone.\n")
        return super().run(prompt, label=label, **kwargs)


def _common_patches(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda n: "/fake/claude" if n == "claude" else None)
    monkeypatch.setattr(
        "pipeline.orchestrators.local.install_signal_handlers", lambda c: None
    )
    monkeypatch.setattr("pipeline.stages.review_code.time.sleep", lambda s: None)


def test_orchestrators_do_not_clobber_external_state(tmp_path, monkeypatch):
    # Stub pipeline.state.subprocess.run with a recorder so we can detect any
    # set-stage.sh invocation regardless of whether the bash helper actually
    # exists on the test machine, and without depending on the leaked GR_ID
    # value matching a pre-created state file.
    recorded_calls = []
    real_run = __import__("subprocess").run

    def recording_run(*args, **kwargs):
        recorded_calls.append(args[0] if args else kwargs.get("args"))
        # Return a successful CompletedProcess shape so callers don't choke.
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr("pipeline.state.subprocess.run", recording_run)

    # Simulate the parent gremlin's state directory under XDG_STATE_HOME so
    # any direct (non-set-stage.sh) write path that resolves through
    # XDG_STATE_HOME would still land in tmp_path and corrupt the file.
    xdg = tmp_path / "xdg"
    parent_id = "parent-gremlin-deadbeef"
    parent_state_dir = xdg / "claude-gremlins" / parent_id
    parent_state_dir.mkdir(parents=True)
    parent_state_file = parent_state_dir / "state.json"
    original_content = json.dumps({"id": parent_id, "stage": "implement"})
    parent_state_file.write_text(original_content)
    parent_mtime = parent_state_file.stat().st_mtime_ns

    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    # GR_ID intentionally NOT set here — the autouse _isolate_gr_id fixture
    # in conftest.py has removed it. With Layer 1 in place this test asserts
    # the orchestrators are quiet: set_stage no-ops because GR_ID is empty,
    # so subprocess.run is never invoked against set-stage.sh. If a future
    # change reintroduces the leak (e.g. a stage that re-reads GR_ID from
    # somewhere other than os.environ, or autouse delenv being removed when
    # pytest runs from a parent gremlin), the recorded_calls assertion below
    # fires.

    # ---- local_main (--plan mode) ----
    session_dir = tmp_path / "session-local"
    session_dir.mkdir()
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\nDo stuff.\n")

    monkeypatch.chdir(tmp_path)
    _common_patches(monkeypatch)
    monkeypatch.setattr(
        "pipeline.orchestrators.local.resolve_session_dir", lambda: session_dir
    )
    monkeypatch.setattr("pipeline.orchestrators.local.in_git_repo", lambda: False)
    monkeypatch.setattr(
        "pipeline.orchestrators.local._load_core_principles", lambda: "Be good."
    )
    monkeypatch.setattr(
        "pipeline.stages.implement.changes_outside_git", lambda s, d: True
    )
    client_local = _ReviewCreatingClient(
        fixtures={
            "implement": MINIMAL_EVENTS,
            **{lbl: MINIMAL_EVENTS for lbl in _REVIEW_LABELS},
            "address-code": MINIMAL_EVENTS,
        }
    )
    assert local_main(["--plan", str(plan_file)], client=client_local) == 0

    # ---- review_main ----
    review_dir = tmp_path / "review-dir"
    review_dir.mkdir()
    monkeypatch.chdir(review_dir)
    client_review = _ReviewCreatingClient(
        fixtures={lbl: MINIMAL_EVENTS for lbl in _REVIEW_LABELS}
    )
    assert review_main(["--dir", str(review_dir)], client=client_review) == 0

    # ---- address_main ----
    address_dir = tmp_path / "address-dir"
    address_dir.mkdir()
    for lens in ("holistic", "detail", "scope"):
        (address_dir / f"review-code-{lens}-sonnet.md").write_text(
            f"# {lens.title()} Review\n\n## Findings\nNone.\n"
        )
    client_addr = FakeClaudeClient(fixtures={"address-code": MINIMAL_EVENTS})
    assert address_main(["--dir", str(address_dir)], client=client_addr) == 0

    # The parent gremlin's state.json must not have been touched by any of
    # the orchestrators above (covers any direct write path that uses
    # XDG_STATE_HOME).
    assert parent_state_file.stat().st_mtime_ns == parent_mtime
    assert parent_state_file.read_text() == original_content
    state = json.loads(parent_state_file.read_text())
    assert state["stage"] == "implement"
    assert "sub_stage" not in state

    # No orchestrator stage may invoke set-stage.sh — with GR_ID unset (the
    # post-fix invariant), pipeline.state.set_stage early-returns and never
    # shells out. Recording subprocess.run catches the leak even when the
    # bash helper isn't on disk in the test environment, and even if a
    # future write path computes a state file under a different gr_id than
    # the one staged above.
    leaked = [
        c for c in recorded_calls
        if c and len(c) >= 1 and "set-stage.sh" in str(c[0])
    ]
    assert not leaked, (
        f"orchestrators leaked set-stage.sh calls under unset GR_ID: {leaked}"
    )
