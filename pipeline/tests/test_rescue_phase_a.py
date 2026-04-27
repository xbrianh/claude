"""Shell integration tests for `/gremlins rescue` Phase A (the diagnosis step).

The diagnosis step spawns ``claude -p`` with the rescue prompt and reads a
verdict marker from a known path inside the gremlin's artifacts dir. The
contract this layer protects:

- The diagnosis agent runs in a *scratch* directory, not the gremlin's
  worktree. The worktree path is named in the prompt for read access; it
  must not become the agent's cwd.
- The agent's verdicts (``fixed`` / ``transient`` / ``structural`` /
  ``unsalvageable``) drive whether the wrapper writes a bail reason or
  proceeds to relaunch.
- A missing / malformed marker results in a wrapper-level bail
  (``diagnosis_no_marker`` / ``diagnosis_bad_marker``), never silent
  success.
"""

from __future__ import annotations

import datetime
import json
import pathlib

from fixtures.shell_env import (
    REPO_ROOT,
    install_fake_bin,
    read_fake_claude_log,
    setup_shell_env,
)


def _load_gremlins_module():
    """Return the fleet module — the canonical home for do_rescue."""
    from pipeline import fleet
    return fleet


def _make_failed_gremlin(state_root: pathlib.Path, workdir: pathlib.Path,
                         gr_id: str = "victim-abcdef") -> pathlib.Path:
    """Create the on-disk shape of a gremlin that crashed and is awaiting rescue.

    Returns the state dir path.
    """
    state_dir = state_root / "claude-gremlins" / gr_id
    state_dir.mkdir(parents=True)
    state = {
        "id": gr_id,
        "kind": "localgremlin",
        "stage": "implement",
        "status": "stopped",
        "exit_code": 1,
        "workdir": str(workdir),
        "project_root": str(workdir.parent),
        "description": "test gremlin",
        "started_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rescue_count": 0,
    }
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (state_dir / "log").write_text("fake log tail\n", encoding="utf-8")
    (state_dir / "finished").touch()
    return state_dir


def _patch_state_root(gremlins_mod, state_root: pathlib.Path, monkeypatch):
    """Point the loaded gremlins module at our test STATE_ROOT."""
    monkeypatch.setattr(
        gremlins_mod,
        "STATE_ROOT",
        str(state_root / "claude-gremlins"),
    )


def _install_stub_launcher(home: pathlib.Path) -> pathlib.Path:
    """Replace fake_home/.claude/skills/_bg/launch.sh with a stub that records
    its argv and exits 0. Returns the path to the recording file.

    The default fake home symlinks ``~/.claude/skills`` straight at the repo's
    ``skills/`` dir, so writing through that path would clobber the real
    ``launch.sh`` on disk. Peel the symlink back into a real directory of
    per-child symlinks so we can replace just ``_bg`` with a stub copy.
    """
    skills_dir = home / ".claude" / "skills"
    if skills_dir.is_symlink():
        # Peel: replace the symlink with a real directory of per-child links,
        # then materialize a real `_bg` dir we can safely write into.
        target = skills_dir.resolve()
        skills_dir.unlink()
        skills_dir.mkdir()
        for child in target.iterdir():
            (skills_dir / child.name).symlink_to(child)
    bg = skills_dir / "_bg"
    if bg.is_symlink():
        bg.unlink()
    bg.mkdir(parents=True, exist_ok=True)
    record_file = home / "stub_launcher_calls.log"
    record_file.write_text("", encoding="utf-8")
    launcher = bg / "launch.sh"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$@\" >> '{record_file}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return record_file


def test_rescue_diagnosis_runs_in_scratch_dir_not_worktree(tmp_path, monkeypatch):
    """The diagnosis agent's cwd is a /tmp scratch dir, not the gremlin's worktree."""
    sh = setup_shell_env(tmp_path)
    state_dir = _make_failed_gremlin(sh.state_root, sh.repo)

    # HOME + PATH already steered by setup_shell_env. Tell our fake claude
    # to declare unsalvageable so the wrapper bails (no relaunch needed)
    # — this test only cares about cwd, not the relaunch path.
    sh.env["FAKE_CLAUDE_RESCUE_VERDICT"] = "unsalvageable"
    for k, v in sh.env.items():
        monkeypatch.setenv(k, v)

    gremlins_mod = _load_gremlins_module()
    _patch_state_root(gremlins_mod, sh.state_root, monkeypatch)

    ok = gremlins_mod.do_rescue("victim-abcdef", headless=False)
    # unsalvageable verdict returns False (no relaunch). That's expected.
    assert ok is False

    log = read_fake_claude_log(sh.fake_claude_log)
    rescue_calls = [e for e in log if e["stage"] == "rescue-diagnosis"]
    assert len(rescue_calls) == 1, log
    cwd = rescue_calls[0]["cwd"]
    # cwd must be a scratch dir, not the worktree.
    assert pathlib.Path(cwd).resolve() != sh.repo.resolve(), \
        f"diagnosis must run in scratch, not worktree ({cwd})"
    assert "gremlin-rescue-" in cwd, \
        f"expected scratch dir prefix gremlin-rescue-, got {cwd}"


def test_rescue_unsalvageable_records_bail(tmp_path, monkeypatch):
    """An ``unsalvageable`` marker writes ``bail_reason=unsalvageable``."""
    sh = setup_shell_env(tmp_path)
    state_dir = _make_failed_gremlin(sh.state_root, sh.repo)

    sh.env["FAKE_CLAUDE_RESCUE_VERDICT"] = "unsalvageable"
    sh.env["FAKE_CLAUDE_RESCUE_SUMMARY"] = "worktree gone"
    for k, v in sh.env.items():
        monkeypatch.setenv(k, v)

    gremlins_mod = _load_gremlins_module()
    _patch_state_root(gremlins_mod, sh.state_root, monkeypatch)

    gremlins_mod.do_rescue("victim-abcdef", headless=False)

    state = json.loads((state_dir / "state.json").read_text())
    assert state["bail_reason"] == "unsalvageable"
    assert "worktree gone" in state.get("bail_detail", "")
    assert state["status"] == "bailed"


def test_rescue_structural_records_bail(tmp_path, monkeypatch):
    """A ``structural`` marker writes ``bail_reason=structural`` with the agent's summary."""
    sh = setup_shell_env(tmp_path)
    state_dir = _make_failed_gremlin(sh.state_root, sh.repo)

    sh.env["FAKE_CLAUDE_RESCUE_VERDICT"] = "structural"
    sh.env["FAKE_CLAUDE_RESCUE_SUMMARY"] = "pipeline bug in foo.py"
    for k, v in sh.env.items():
        monkeypatch.setenv(k, v)

    gremlins_mod = _load_gremlins_module()
    _patch_state_root(gremlins_mod, sh.state_root, monkeypatch)

    gremlins_mod.do_rescue("victim-abcdef", headless=False)

    state = json.loads((state_dir / "state.json").read_text())
    assert state["bail_reason"] == "structural"
    assert "pipeline bug in foo.py" in state.get("bail_detail", "")


def test_rescue_no_marker_records_diagnosis_no_marker(tmp_path, monkeypatch):
    """Agent that returns 0 without writing the marker → diagnosis_no_marker bail."""
    sh = setup_shell_env(tmp_path)
    # Override the fake claude with a stub that emits no marker but exits 0.
    no_marker_claude = tmp_path / "no_marker_claude.py"
    no_marker_claude.write_text(
        "#!/usr/bin/env python\nimport sys\nsys.exit(0)\n",
        encoding="utf-8",
    )
    install_fake_bin(sh.bin_dir, "claude", no_marker_claude)

    state_dir = _make_failed_gremlin(sh.state_root, sh.repo)

    for k, v in sh.env.items():
        monkeypatch.setenv(k, v)

    gremlins_mod = _load_gremlins_module()
    _patch_state_root(gremlins_mod, sh.state_root, monkeypatch)

    gremlins_mod.do_rescue("victim-abcdef", headless=False)

    state = json.loads((state_dir / "state.json").read_text())
    assert state["bail_reason"] == "diagnosis_no_marker"


def test_rescue_fixed_verdict_invokes_launcher_resume(tmp_path, monkeypatch):
    """A ``fixed`` marker triggers ``launch.sh --resume <id>``.

    Replaces the launcher with a recording stub so we don't actually fork a
    background pipeline; the test verifies the wrapper called it with the
    right argv and reported relaunch_outcome=success.
    """
    sh = setup_shell_env(tmp_path)
    state_dir = _make_failed_gremlin(sh.state_root, sh.repo)

    record_file = _install_stub_launcher(sh.home)

    sh.env["FAKE_CLAUDE_RESCUE_VERDICT"] = "fixed"
    sh.env["FAKE_CLAUDE_RESCUE_SUMMARY"] = "edited state.json"
    for k, v in sh.env.items():
        monkeypatch.setenv(k, v)

    gremlins_mod = _load_gremlins_module()
    _patch_state_root(gremlins_mod, sh.state_root, monkeypatch)

    ok = gremlins_mod.do_rescue("victim-abcdef", headless=False)
    assert ok is True

    recorded = record_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(recorded) == 1, recorded
    assert "--resume" in recorded[0]
    assert "victim-abcdef" in recorded[0]


def test_rescue_headless_excluded_class_refused(tmp_path, monkeypatch):
    """Headless rescue refuses gremlins whose bail_class is in the exclusion list."""
    sh = setup_shell_env(tmp_path)
    state_dir = _make_failed_gremlin(sh.state_root, sh.repo)
    # Add an excluded bail_class to the existing victim state.
    state = json.loads((state_dir / "state.json").read_text())
    state["bail_class"] = "secrets"
    state["bail_detail"] = "diff touches secrets"
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    for k, v in sh.env.items():
        monkeypatch.setenv(k, v)

    gremlins_mod = _load_gremlins_module()
    _patch_state_root(gremlins_mod, sh.state_root, monkeypatch)

    ok = gremlins_mod.do_rescue("victim-abcdef", headless=True)
    assert ok is False

    final = json.loads((state_dir / "state.json").read_text())
    assert final["bail_reason"] == "excluded_class:secrets"
    # Fake claude must not have been spawned at all.
    log = read_fake_claude_log(sh.fake_claude_log)
    assert all(e["stage"] != "rescue-diagnosis" for e in log), log
