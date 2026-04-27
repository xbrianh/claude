"""Tests for gremlins/fleet.py (formerly skills/gremlins/gremlins.py)."""

import json
import os
import pathlib
import subprocess

import pytest

from gremlins import fleet as gremlins


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_state(state_dir: pathlib.Path, payload: dict, *,
                 finished: bool = False, log_text: str | None = None) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    sf = state_dir / "state.json"
    sf.write_text(json.dumps(payload))
    if finished:
        (state_dir / "finished").touch()
    if log_text is not None:
        (state_dir / "log").write_text(log_text)
    return str(sf)


def _setup_dead_gremlin(tmp_path, monkeypatch, gr_id="test-id-aabb12",
                        **state_overrides):
    """Build a state-root with a single dead gremlin, monkeypatch STATE_ROOT."""
    state_root = tmp_path / "state-root"
    state_root.mkdir()
    gr_dir = state_root / gr_id
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    state = {
        "id": gr_id,
        "kind": "localgremlin",
        "stage": "review-code",
        "status": "dead",
        "exit_code": 2,
        "workdir": str(workdir),
        "rescue_count": 0,
    }
    state.update(state_overrides)
    _write_state(gr_dir, state, finished=True)
    monkeypatch.setattr(gremlins, "STATE_ROOT", str(state_root))
    return gr_dir, workdir


def _init_git_repo(path: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path,
                   check=True, capture_output=True)


# ---------------------------------------------------------------------------
# liveness_of_state_file — state transitions
# ---------------------------------------------------------------------------

def test_liveness_running_with_live_pid_and_fresh_log(tmp_path):
    sf = _write_state(
        tmp_path / "g",
        {"status": "running", "pid": os.getpid()},
        log_text="recent",
    )
    assert gremlins.liveness_of_state_file(sf) == "running"


def test_liveness_dead_finished_zero_exit(tmp_path):
    sf = _write_state(
        tmp_path / "g",
        {"status": "running", "pid": 99999, "exit_code": 0},
        finished=True,
    )
    assert gremlins.liveness_of_state_file(sf) == "dead:finished"


def test_liveness_dead_with_nonzero_exit(tmp_path):
    sf = _write_state(
        tmp_path / "g",
        {"status": "running", "pid": 99999, "exit_code": 2},
        finished=True,
    )
    assert gremlins.liveness_of_state_file(sf) == "dead:exit 2"


def test_liveness_dead_bailed_includes_reason(tmp_path):
    sf = _write_state(
        tmp_path / "g",
        {"status": "bailed", "exit_code": 2, "bail_reason": "structural"},
        finished=True,
    )
    assert gremlins.liveness_of_state_file(sf) == "dead:bailed:structural"


def test_liveness_dead_crashed_when_pid_gone(tmp_path):
    # PID extremely unlikely to exist
    sf = _write_state(
        tmp_path / "g",
        {"status": "running", "pid": 999999},
    )
    assert gremlins.liveness_of_state_file(sf).startswith("dead:crashed")


def test_liveness_stalled_when_log_is_old(tmp_path, monkeypatch):
    sf = _write_state(
        tmp_path / "g",
        {"status": "running", "pid": os.getpid()},
        log_text="old",
    )
    log_path = tmp_path / "g" / "log"
    old = os.path.getmtime(log_path) - 10000
    os.utime(log_path, (old, old))
    monkeypatch.setattr(gremlins, "BG_STALL_SECS", 100)
    assert gremlins.liveness_of_state_file(sf).startswith("stalled:")


# ---------------------------------------------------------------------------
# build_row — rescue marker (state transition: dead → rescued → running)
# ---------------------------------------------------------------------------

def test_build_row_rescue_suffix_singular():
    state = {"kind": "localgremlin", "stage": "implement",
             "rescue_count": 1, "started_at": ""}
    row = gremlins.build_row("g1", "/sf", "/wdir", state, "running")
    assert "(rescue)" in row["live"]


def test_build_row_rescue_suffix_multiple():
    state = {"kind": "localgremlin", "stage": "implement",
             "rescue_count": 3, "started_at": ""}
    row = gremlins.build_row("g1", "/sf", "/wdir", state, "running")
    assert "(rescue x3)" in row["live"]


def test_build_row_no_rescue_suffix_when_zero():
    state = {"kind": "localgremlin", "stage": "implement",
             "rescue_count": 0, "started_at": ""}
    row = gremlins.build_row("g1", "/sf", "/wdir", state, "running")
    assert "(rescue" not in row["live"]


# ---------------------------------------------------------------------------
# Phase A marker contract — _read_rescue_marker
# ---------------------------------------------------------------------------

def _write_marker(path: pathlib.Path, payload) -> str:
    if isinstance(payload, str):
        path.write_text(payload)
    else:
        path.write_text(json.dumps(payload))
    return str(path)


def test_marker_missing_file(tmp_path):
    status, msg = gremlins._read_rescue_marker(str(tmp_path / "missing.json"))
    assert status == "no_marker"
    assert "did not write" in msg


def test_marker_unparseable(tmp_path):
    p = _write_marker(tmp_path / "m.json", "not json")
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "bad_marker"
    assert "unreadable" in msg


def test_marker_not_a_json_object(tmp_path):
    p = _write_marker(tmp_path / "m.json", [1, 2, 3])
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "bad_marker"
    assert "not a JSON object" in msg


def test_marker_invalid_status(tmp_path):
    p = _write_marker(tmp_path / "m.json", {"status": "bogus"})
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "bad_marker"
    assert "invalid status" in msg


def test_marker_summary_must_be_string(tmp_path):
    p = _write_marker(tmp_path / "m.json", {"status": "fixed", "summary": [1, 2]})
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "bad_marker"


def test_marker_fixed(tmp_path):
    p = _write_marker(tmp_path / "m.json",
                       {"status": "fixed", "summary": "patched state.json"})
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "fixed"
    assert msg == "patched state.json"


def test_marker_transient(tmp_path):
    p = _write_marker(tmp_path / "m.json",
                       {"status": "transient", "summary": "network flake"})
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "transient"
    assert msg == "network flake"


def test_marker_structural_with_summary(tmp_path):
    p = _write_marker(tmp_path / "m.json",
                       {"status": "structural", "summary": "bug in foo.sh"})
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "structural"
    assert msg == "bug in foo.sh"


def test_marker_structural_without_summary_uses_fallback(tmp_path):
    p = _write_marker(tmp_path / "m.json", {"status": "structural"})
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "structural"
    assert msg  # non-empty fallback
    assert "structural" in msg.lower()


def test_marker_unsalvageable_without_summary_uses_fallback(tmp_path):
    p = _write_marker(tmp_path / "m.json", {"status": "unsalvageable"})
    status, msg = gremlins._read_rescue_marker(p)
    assert status == "unsalvageable"
    assert "unsalvageable" in msg.lower()


def test_marker_summary_collapses_whitespace(tmp_path):
    p = _write_marker(tmp_path / "m.json",
                       {"status": "fixed", "summary": "line one\nline two\t  end"})
    status, msg = gremlins._read_rescue_marker(p)
    assert "\n" not in msg
    assert "line one" in msg and "line two" in msg


def test_marker_summary_capped_to_500_chars(tmp_path):
    p = _write_marker(tmp_path / "m.json",
                       {"status": "fixed", "summary": "x" * 1000})
    status, msg = gremlins._read_rescue_marker(p)
    assert len(msg) <= 500
    assert msg.endswith("...")


# ---------------------------------------------------------------------------
# rescue --headless: bail-class exclusion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bail_class", [
    "reviewer_requested_changes", "security", "secrets",
])
def test_rescue_headless_excludes_class(tmp_path, monkeypatch, capsys, bail_class):
    gr_dir, _ = _setup_dead_gremlin(
        tmp_path, monkeypatch,
        bail_class=bail_class,
        bail_detail="upstream-set detail",
    )
    ok = gremlins.do_rescue("test-id-aabb12", headless=True)
    assert ok is False
    new = json.loads((gr_dir / "state.json").read_text())
    assert new["bail_reason"] == f"excluded_class:{bail_class}"
    assert new["bail_detail"] == "upstream-set detail"
    assert new["status"] == "bailed"
    assert (gr_dir / "finished").exists()


def test_rescue_headless_does_not_exclude_other_class(tmp_path, monkeypatch):
    """`other` is the only attempted class — verify it gets past the exclusion check."""
    gr_dir, _ = _setup_dead_gremlin(tmp_path, monkeypatch, bail_class="other")
    # Stub the diagnosis step so the rescue terminates without claude.
    monkeypatch.setattr(gremlins, "_run_headless_diagnosis",
                        lambda *a, **kw: ("structural", "fake"))
    ok = gremlins.do_rescue("test-id-aabb12", headless=True)
    assert ok is False
    new = json.loads((gr_dir / "state.json").read_text())
    # Should bail with "structural" (from the stubbed diagnosis), NOT excluded_class
    assert new["bail_reason"] == "structural"


# ---------------------------------------------------------------------------
# rescue --headless: attempt cap enforcement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rescue_count", [3, 4, 10])
def test_rescue_headless_at_or_above_cap_refuses(tmp_path, monkeypatch, rescue_count):
    gr_dir, _ = _setup_dead_gremlin(tmp_path, monkeypatch,
                                      rescue_count=rescue_count)
    ok = gremlins.do_rescue("test-id-aabb12", headless=True)
    assert ok is False
    new = json.loads((gr_dir / "state.json").read_text())
    assert new["bail_reason"] == "attempts_exhausted"
    assert f"reached cap of {gremlins.RESCUE_CAP}" in new["bail_detail"]


def test_rescue_headless_below_cap_proceeds_past_check(tmp_path, monkeypatch):
    gr_dir, _ = _setup_dead_gremlin(tmp_path, monkeypatch,
                                      rescue_count=gremlins.RESCUE_CAP - 1)
    # Stub diagnosis so we don't actually run claude
    monkeypatch.setattr(gremlins, "_run_headless_diagnosis",
                        lambda *a, **kw: ("structural", "agent flagged"))
    ok = gremlins.do_rescue("test-id-aabb12", headless=True)
    assert ok is False
    new = json.loads((gr_dir / "state.json").read_text())
    # Bails with "structural" from diagnosis, not "attempts_exhausted"
    assert new["bail_reason"] == "structural"


def test_rescue_headless_running_refused(tmp_path, monkeypatch, capsys):
    gr_dir, _ = _setup_dead_gremlin(tmp_path, monkeypatch)
    # Mark as running
    state = json.loads((gr_dir / "state.json").read_text())
    state["status"] = "running"
    state["pid"] = os.getpid()
    state["exit_code"] = None
    (gr_dir / "state.json").write_text(json.dumps(state))
    (gr_dir / "finished").unlink()
    (gr_dir / "log").write_text("recent")

    ok = gremlins.do_rescue("test-id-aabb12", headless=True)
    assert ok is False
    out = capsys.readouterr().out
    assert "still running" in out


# ---------------------------------------------------------------------------
# do_close — close flow
# ---------------------------------------------------------------------------

def test_close_dead_gremlin_marks_closed(tmp_path, monkeypatch, capsys):
    gr_dir, _ = _setup_dead_gremlin(tmp_path, monkeypatch)
    ok = gremlins.do_close("test-id-aabb12")
    assert ok is True
    assert (gr_dir / "closed").exists()


def test_close_already_closed_is_idempotent(tmp_path, monkeypatch, capsys):
    gr_dir, _ = _setup_dead_gremlin(tmp_path, monkeypatch)
    (gr_dir / "closed").touch()
    ok = gremlins.do_close("test-id-aabb12")
    assert ok is True
    assert "already closed" in capsys.readouterr().out


def test_close_running_refused(tmp_path, monkeypatch, capsys):
    gr_dir, _ = _setup_dead_gremlin(tmp_path, monkeypatch)
    state = json.loads((gr_dir / "state.json").read_text())
    state["status"] = "running"
    state["pid"] = os.getpid()
    state["exit_code"] = None
    (gr_dir / "state.json").write_text(json.dumps(state))
    (gr_dir / "finished").unlink()
    (gr_dir / "log").write_text("recent")

    ok = gremlins.do_close("test-id-aabb12")
    assert ok is False
    assert not (gr_dir / "closed").exists()
    assert "still live" in capsys.readouterr().out


def test_close_not_found(tmp_path, monkeypatch, capsys):
    state_root = tmp_path / "state-root"
    state_root.mkdir()
    monkeypatch.setattr(gremlins, "STATE_ROOT", str(state_root))
    ok = gremlins.do_close("nonexistent-id")
    assert ok is False
    assert "no gremlin matched" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# do_land / _land_local — squash path
# ---------------------------------------------------------------------------

def test_land_local_squash_lands_branch_and_deletes_it(tmp_path, monkeypatch, capsys):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _init_git_repo(project_root)

    branch = "bg/localgremlin/test-id-aabb12"
    subprocess.run(["git", "checkout", "-b", branch], cwd=project_root,
                   check=True, capture_output=True)
    (project_root / "feature.txt").write_text("feature work\n")
    subprocess.run(["git", "add", "."], cwd=project_root,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: add feature.txt"],
                   cwd=project_root, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=project_root,
                   check=True, capture_output=True)

    state_root = tmp_path / "state-root"
    state_root.mkdir()
    gr_id = "test-id-aabb12"
    gr_dir = state_root / gr_id
    artifacts_dir = gr_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "plan.md").write_text(
        "# Add feature\n\n## Context\nAdd feature.txt to the repo.\n"
    )
    workdir = tmp_path / "workdir"  # not actually a worktree; a stand-in
    workdir.mkdir()
    state = {
        "id": gr_id,
        "kind": "localgremlin",
        "status": "dead",
        "exit_code": 0,
        "setup_kind": "worktree-branch",
        "branch": branch,
        "workdir": str(workdir),
        "project_root": str(project_root),
    }
    _write_state(gr_dir, state, finished=True)

    monkeypatch.setattr(gremlins, "STATE_ROOT", str(state_root))
    monkeypatch.setattr(gremlins, "_synthesize_commit_message_ai",
                        lambda inputs: ("Add feature.txt to repo",
                                         "Adds feature.txt with placeholder content."))
    monkeypatch.chdir(project_root)

    ok = gremlins._land_local(gr_id, str(gr_dir / "state.json"),
                                str(gr_dir), state, mode="squash")
    assert ok is True

    log_out = subprocess.run(
        ["git", "log", "--oneline", "main"], cwd=project_root,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Add feature.txt to repo" in log_out

    branches = subprocess.run(
        ["git", "branch", "--list", branch], cwd=project_root,
        capture_output=True, text=True, check=True,
    ).stdout
    assert branches.strip() == ""


def test_land_local_refuses_non_worktree_branch_setup(tmp_path, monkeypatch, capsys):
    state = {
        "id": "x",
        "kind": "localgremlin",
        "setup_kind": "cp-snapshot",  # not worktree-branch
        "branch": "bg/localgremlin/x",
    }
    ok = gremlins._land_local("x", "/sf", "/wdir", state, mode="squash")
    assert ok is False
    assert "only worktree-branch gremlins" in capsys.readouterr().out


def test_land_local_refuses_when_branch_missing_from_state(tmp_path, monkeypatch, capsys):
    state = {
        "id": "x",
        "kind": "localgremlin",
        "setup_kind": "worktree-branch",
        "branch": "",
    }
    ok = gremlins._land_local("x", "/sf", "/wdir", state, mode="squash")
    assert ok is False
    assert "no branch field" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Misc small-surface helpers
# ---------------------------------------------------------------------------

def test_atomic_patch_state_round_trip(tmp_path):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"a": 1, "b": 2}))
    ok = gremlins._atomic_patch_state(str(sf), {"b": 99, "c": 3})
    assert ok is True
    new = json.loads(sf.read_text())
    assert new == {"a": 1, "b": 99, "c": 3}


def test_atomic_patch_state_unreadable_file(tmp_path):
    ok = gremlins._atomic_patch_state(str(tmp_path / "missing.json"), {"a": 1})
    assert ok is False


def test_write_bail_marks_terminal(tmp_path):
    wdir = tmp_path / "wdir"
    wdir.mkdir()
    sf = wdir / "state.json"
    sf.write_text(json.dumps({"id": "x", "status": "dead", "exit_code": 2}))
    gremlins._write_bail(str(sf), str(wdir), "structural", "the agent said so")
    new = json.loads(sf.read_text())
    assert new["bail_reason"] == "structural"
    assert new["bail_detail"] == "the agent said so"
    assert new["status"] == "bailed"
    assert (wdir / "finished").exists()


def test_parse_duration():
    assert gremlins.parse_duration("30s") == 30
    assert gremlins.parse_duration("5m") == 300
    assert gremlins.parse_duration("2h") == 7200
    assert gremlins.parse_duration("1d") == 86400


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        gremlins.parse_duration("5x")
    with pytest.raises(ValueError):
        gremlins.parse_duration("abc")


# ---------------------------------------------------------------------------
# do_rescue interactive: streaming events via stream_events
# ---------------------------------------------------------------------------

def test_rescue_interactive_streams_events_to_stderr(tmp_path, monkeypatch, capsys):
    """Interactive rescue streams [rescue]-prefixed events to stderr."""
    import os as _os

    gr_dir, workdir = _setup_dead_gremlin(tmp_path, monkeypatch)

    # Fake claude: emits stream-json, writes marker file, exits 0.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_bin = bin_dir / "claude"
    fake_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, re, pathlib\n"
        "prompt = sys.argv[-1]\n"
        "m = re.search(r'(/[^\\s`]+\\.done)', prompt)\n"
        "marker = m.group(1) if m else ''\n"
        "for evt in [\n"
        "    {'type':'system','subtype':'init','session_id':'r1','model':'fake','cwd':'.'},\n"
        "    {'type':'assistant','message':{'content':[{'type':'text','text':'diagnosing'}]}},\n"
        "    {'type':'result','subtype':'success','num_turns':1,'total_cost_usd':0},\n"
        "]:\n"
        "    sys.stdout.write(json.dumps(evt) + '\\n')\n"
        "sys.stdout.flush()\n"
        "if marker:\n"
        "    pathlib.Path(marker).parent.mkdir(parents=True, exist_ok=True)\n"
        "    pathlib.Path(marker).write_text(json.dumps({'status':'fixed','summary':'ok'}))\n"
    )
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{_os.environ.get('PATH', '')}")

    # Prevent relaunch step from executing a real launcher.
    _real_access = _os.access
    def _fake_access(path, mode, *args, **kwargs):
        if isinstance(path, str) and "launch.sh" in path:
            return False
        return _real_access(path, mode, *args, **kwargs)
    monkeypatch.setattr(_os, "access", _fake_access)

    gremlins.do_rescue("test-id-aabb12", headless=False)

    _, err = capsys.readouterr()
    assert "[rescue] init" in err
    assert "[rescue] text:" in err
    assert "[rescue] final:" in err


def test_rescue_interactive_nonzero_exit_writes_bail(tmp_path, monkeypatch, capsys):
    """Interactive rescue with rc != 0 (no marker) returns False and sets diagnosis_claude_error."""
    import os as _os

    gr_dir, workdir = _setup_dead_gremlin(tmp_path, monkeypatch)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_bin = bin_dir / "claude"
    fake_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "for evt in [\n"
        "    {'type':'system','subtype':'init','session_id':'r2','model':'fake','cwd':'.'},\n"
        "    {'type':'result','subtype':'error','num_turns':1,'total_cost_usd':0},\n"
        "]:\n"
        "    sys.stdout.write(json.dumps(evt) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.exit(1)\n"
    )
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{_os.environ.get('PATH', '')}")

    _real_access = _os.access
    def _fake_access(path, mode, *args, **kwargs):
        if isinstance(path, str) and "launch.sh" in path:
            return False
        return _real_access(path, mode, *args, **kwargs)
    monkeypatch.setattr(_os, "access", _fake_access)

    result = gremlins.do_rescue("test-id-aabb12", headless=False)

    assert result is False
    state = json.loads((gr_dir / "state.json").read_text())
    assert state.get("bail_reason") == "diagnosis_claude_error"
