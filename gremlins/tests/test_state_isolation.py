"""Regression tests for the GR_ID-leakage bug captured on PR #140.

When `pytest` runs as a subprocess of an implement-stage gremlin, GR_ID is
inherited. Without isolation, gremlins.state.set_stage shells out to
set-stage.sh against the parent gremlin's state.json, corrupting its
`stage` and `sub_stage` fields — observable in `/gremlins --watch` and
dangerous for any rescue-flow logic that branches on `stage`.

The fix is the autouse `_isolate_gr_id` fixture in conftest.py, which
delenv's GR_ID before every test. These tests verify both layers:

- test_autouse_isolate_gr_id_unsets_gr_id_under_inherited_env: spawns a
  pytest subprocess with GR_ID set in its environment and asserts the
  autouse fixture removes it inside the test body. Without the subprocess
  hop, this test would pass trivially in any clean CI environment (no
  GR_ID inherited) regardless of whether the autouse fixture was present.
- test_*_does_not_clobber_external_state: per-orchestrator end-to-end
  checks that running each entry point does not invoke set-stage.sh
  against a pre-staged parent gremlin's state.json. Each orchestrator is
  exercised in its own test so a regression message names the offender.

Coverage envelope: with GR_ID unset (the post-fix invariant), set_stage
early-returns before subprocess.run, so these tests verify that guard
plus the autouse fixture's delenv. They do NOT catch hypothetical future
code paths that resolve GR_ID from somewhere other than os.environ (a
config file, a passed-in arg) — those bypass the autouse delenv. To
make any leak that *does* hit subprocess.run deterministic regardless of
whether ~/.claude/skills/_bg/set-stage.sh exists on the test machine,
the orchestrator tests monkeypatch gremlins.state.SET_STAGE_SH to a
real executable in tmp_path.
"""

import json
import os
import pathlib
import subprocess
import sys
import textwrap

from conftest import (
    MINIMAL_EVENTS,
    REVIEW_LABELS as _REVIEW_LABELS,
    ReviewCreatingClient as _ReviewCreatingClient,
    common_local_patches as _common_patches,
)
from gremlins.clients.fake import FakeClaudeClient
from gremlins.orchestrators.local import address_main, local_main, review_main


def test_autouse_isolate_gr_id_unsets_gr_id_under_inherited_env(tmp_path):
    # Spawn a pytest subprocess with GR_ID set in env. The autouse fixture
    # must remove it inside the inner test body. Without the subprocess hop
    # this would pass trivially in any environment that doesn't already
    # have GR_ID set, so removing the autouse fixture wouldn't trip the
    # regression.
    #
    # Place a conftest.py next to the inner test that imports the real
    # autouse fixture from gremlins.tests.conftest so we are exercising
    # the actual fixture under test, not a re-implementation.
    inner_conftest = tmp_path / "conftest.py"
    inner_conftest.write_text(textwrap.dedent("""
        # Re-export the autouse _isolate_gr_id fixture from the real
        # gremlins.tests.conftest so the inner pytest run picks it up.
        # importlib avoids the name collision pytest sees when this
        # conftest.py tries to `from conftest import ...`.
        import importlib.util as _u, os, pathlib as _p
        _src = _p.Path(os.environ["GREMLINS_TESTS_DIR"]) / "conftest.py"
        _spec = _u.spec_from_file_location("gremlins_tests_conftest", _src)
        _mod = _u.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _isolate_gr_id = _mod._isolate_gr_id
    """))
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(textwrap.dedent("""
        import os

        def test_gr_id_unset_inside_pytest():
            assert os.environ.get("GR_ID") is None, (
                "autouse _isolate_gr_id fixture failed to remove inherited "
                f"GR_ID={os.environ.get('GR_ID')!r}"
            )
    """))
    tests_dir = pathlib.Path(__file__).resolve().parent
    repo_root = tests_dir.parent.parent
    env = dict(os.environ)
    env["GR_ID"] = "fake-parent-gremlin-deadbeef"
    env["GREMLINS_TESTS_DIR"] = str(tests_dir)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + str(tests_dir)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        f"inner pytest failed (autouse fixture not isolating GR_ID?):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _recording_run(recorded_calls):
    """Build a subprocess.run replacement that records each call's argv and
    returns a successful CompletedProcess. Module-scope helper so we don't
    rebuild the closure target type on every recorded call."""

    def recording_run(*args, **kwargs):
        recorded_calls.append(args[0] if args else kwargs.get("args"))
        return subprocess.CompletedProcess(
            args=args[0] if args else (),
            returncode=0,
            stdout="",
            stderr="",
        )

    return recording_run


def _stage_parent_state(tmp_path, monkeypatch):
    """Pre-create a parent gremlin's state.json under XDG_STATE_HOME and
    install a fake set-stage.sh so any leak deterministically hits the
    recorded subprocess.run. Returns (parent_state_file, original_content,
    parent_mtime, recorded_calls).
    """
    xdg = tmp_path / "xdg"
    parent_id = "parent-gremlin-deadbeef"
    parent_state_dir = xdg / "claude-gremlins" / parent_id
    parent_state_dir.mkdir(parents=True)
    parent_state_file = parent_state_dir / "state.json"
    original_content = json.dumps({"id": parent_id, "stage": "implement"})
    parent_state_file.write_text(original_content)
    parent_mtime = parent_state_file.stat().st_mtime_ns
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))

    # Install a fake set-stage.sh executable so gremlins.state.set_stage's
    # exists()/access(X_OK) preflight succeeds. Without this, a future
    # regression that re-leaked GR_ID could still silently pass on a CI
    # machine that has no ~/.claude/skills/_bg/set-stage.sh, because the
    # preflight would early-return before our recorded subprocess.run hook.
    fake_helper = tmp_path / "set-stage.sh"
    fake_helper.write_text("#!/bin/sh\nexit 0\n")
    fake_helper.chmod(0o755)
    monkeypatch.setattr("gremlins.state.SET_STAGE_SH", fake_helper)

    recorded_calls = []
    monkeypatch.setattr("gremlins.state.subprocess.run", _recording_run(recorded_calls))

    # GR_ID intentionally NOT set here — the autouse _isolate_gr_id fixture
    # in conftest.py has removed it. With Layer 1 in place, set_stage
    # no-ops because GR_ID is empty, so subprocess.run is never invoked
    # against set-stage.sh. If autouse delenv is removed AND GR_ID is
    # inherited from the parent env, the recorded_calls assertion fires.
    return parent_state_file, original_content, parent_mtime, recorded_calls


def _assert_no_state_clobber(parent_state_file, original_content, parent_mtime, recorded_calls):
    # File byte-equality covers any direct write path that resolves through
    # XDG_STATE_HOME (the asserts on stage / sub_stage that used to live
    # here were redundant — they're implied by content equality).
    assert parent_state_file.stat().st_mtime_ns == parent_mtime
    assert parent_state_file.read_text() == original_content

    leaked = [
        c for c in recorded_calls
        if c and len(c) >= 1 and "set-stage.sh" in str(c[0])
    ]
    assert not leaked, (
        f"orchestrator leaked set-stage.sh calls under unset GR_ID: {leaked}"
    )


def test_local_main_does_not_clobber_external_state(tmp_path, monkeypatch):
    parent_state_file, original_content, parent_mtime, recorded_calls = (
        _stage_parent_state(tmp_path, monkeypatch)
    )

    session_dir = tmp_path / "session-local"
    session_dir.mkdir()
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\nDo stuff.\n")

    monkeypatch.chdir(tmp_path)
    _common_patches(monkeypatch)
    monkeypatch.setattr(
        "gremlins.orchestrators.local.resolve_session_dir", lambda: session_dir
    )
    monkeypatch.setattr("gremlins.orchestrators.local.in_git_repo", lambda: False)
    monkeypatch.setattr(
        "gremlins.orchestrators.local._load_core_principles", lambda: "Be good."
    )
    monkeypatch.setattr(
        "gremlins.stages.implement.changes_outside_git", lambda s, d: True
    )

    client = _ReviewCreatingClient(
        fixtures={
            "implement": MINIMAL_EVENTS,
            **{lbl: MINIMAL_EVENTS for lbl in _REVIEW_LABELS},
            "address-code": MINIMAL_EVENTS,
        }
    )
    assert local_main(["--plan", str(plan_file)], client=client) == 0
    _assert_no_state_clobber(parent_state_file, original_content, parent_mtime, recorded_calls)


def test_review_main_does_not_clobber_external_state(tmp_path, monkeypatch):
    parent_state_file, original_content, parent_mtime, recorded_calls = (
        _stage_parent_state(tmp_path, monkeypatch)
    )

    review_dir = tmp_path / "review-dir"
    review_dir.mkdir()
    monkeypatch.chdir(review_dir)
    _common_patches(monkeypatch)
    monkeypatch.setattr("gremlins.orchestrators.local.in_git_repo", lambda: False)
    client = _ReviewCreatingClient(
        fixtures={lbl: MINIMAL_EVENTS for lbl in _REVIEW_LABELS}
    )
    assert review_main(["--dir", str(review_dir)], client=client) == 0
    _assert_no_state_clobber(parent_state_file, original_content, parent_mtime, recorded_calls)


def test_address_main_does_not_clobber_external_state(tmp_path, monkeypatch):
    parent_state_file, original_content, parent_mtime, recorded_calls = (
        _stage_parent_state(tmp_path, monkeypatch)
    )

    address_dir = tmp_path / "address-dir"
    address_dir.mkdir()
    for lens in ("holistic", "detail", "scope"):
        (address_dir / f"review-code-{lens}-sonnet.md").write_text(
            f"# {lens.title()} Review\n\n## Findings\nNone.\n"
        )
    monkeypatch.chdir(address_dir)
    _common_patches(monkeypatch)
    monkeypatch.setattr("gremlins.orchestrators.local.in_git_repo", lambda: False)
    client = FakeClaudeClient(fixtures={"address-code": MINIMAL_EVENTS})
    assert address_main(["--dir", str(address_dir)], client=client) == 0
    _assert_no_state_clobber(parent_state_file, original_content, parent_mtime, recorded_calls)
