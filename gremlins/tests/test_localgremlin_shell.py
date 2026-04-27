"""Shell integration tests for the localgremlin pipeline.

Drives the full plan → implement → review (parallel) → address chain
via ``launch.sh localgremlin`` with a fake `claude` on PATH and a real
git repo. Verifies stage ordering and per-stage artifacts on disk.
"""

from __future__ import annotations

import subprocess

from fixtures.shell_env import (
    REPO_ROOT,
    read_fake_claude_log,
    read_state,
    setup_shell_env,
    wait_for_finished,
)

LAUNCH_SH = REPO_ROOT / "skills" / "_bg" / "launch.sh"


def _launch_local(sh, *args, timeout=15):
    return subprocess.run(
        [str(LAUNCH_SH), "--print-id", "localgremlin", *args],
        cwd=str(sh.repo), env=sh.env,
        capture_output=True, text=True, timeout=timeout,
    )


def test_localgremlin_full_pipeline_via_launch(tmp_path):
    """plan → implement → review → address all run, in order, exactly once."""
    sh = setup_shell_env(tmp_path)
    r = _launch_local(sh, "test full pipeline")
    assert r.returncode == 0, r.stderr
    gr_id = r.stdout.strip()

    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120), \
        f"pipeline did not finish; log:\n{(state_dir / 'log').read_text(errors='replace')[-2000:]}"

    state = read_state(state_dir / "state.json")
    assert state["status"] == "done", \
        f"expected done, got {state.get('status')}; log tail:\n" \
        f"{(state_dir / 'log').read_text(errors='replace')[-2000:]}"
    assert state["exit_code"] == 0

    # Verify stage ordering from the fake claude log.
    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    assert stages[0] == "plan", stages
    assert stages[1] == "implement-local", stages
    assert stages[2] == "review", stages
    assert stages[3] == "address", stages

    # Artifacts exist in the session dir.
    artifacts = state_dir / "artifacts"
    assert (artifacts / "plan.md").exists()
    review_files = list(artifacts.glob("review-code-*.md"))
    assert len(review_files) == 1, [p.name for p in review_files]


def test_localgremlin_model_flags_forwarded(tmp_path):
    """`-i <model>` reaches the implement stage's claude invocation."""
    sh = setup_shell_env(tmp_path)
    r = _launch_local(sh, "-i", "haiku", "test model forwarding")
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    log = read_fake_claude_log(sh.fake_claude_log)
    impl_calls = [e for e in log if e["stage"] == "implement-local"]
    assert len(impl_calls) == 1
    assert impl_calls[0]["model"] == "haiku", impl_calls[0]


def test_localgremlin_plan_mode_skips_plan_stage(tmp_path):
    """`--plan <path>` copies the plan file and skips the plan claude call."""
    sh = setup_shell_env(tmp_path)
    plan = sh.repo / "given-plan.md"
    plan.write_text("# Provided Plan\n\n## Tasks\n- [ ] Stuff\n", encoding="utf-8")

    r = _launch_local(sh, "--plan", str(plan))
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    assert "plan" not in stages, f"plan stage should have been skipped: {stages}"
    assert "implement-local" in stages
    # The supplied plan was snapshot into the artifacts dir.
    snapshot = state_dir / "artifacts" / "plan.md"
    assert snapshot.exists()
    assert snapshot.read_text(encoding="utf-8") == plan.read_text(encoding="utf-8")


def test_pipeline_survives_worktree_pipeline_rename(tmp_path):
    """Regression: pipeline completes even when implement renames worktree's gremlins/.

    Without PYTHONSAFEPATH=1, python -m gremlins.cli imports from the worktree
    (cwd). Renaming gremlins/ during implement then causes FileNotFoundError in
    later stages because PROMPTS_DIR is __file__-relative and the directory is
    gone. With the fix, python loads gremlins from HOME/.claude/gremlins/ and the
    worktree rename is harmless.
    """
    sh = setup_shell_env(tmp_path)

    # Add a gremlins/ stub to the repo so the worktree shadows $HOME/.claude/gremlins/.
    pipeline_stub = sh.repo / "gremlins"
    pipeline_stub.mkdir()
    (pipeline_stub / "__init__.py").write_text("# stub\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(sh.repo), "add", "gremlins"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(sh.repo), "commit", "-m", "add gremlins stub"],
        check=True, capture_output=True,
    )

    sh.env["FAKE_CLAUDE_RENAME_PIPELINE"] = "1"
    r = _launch_local(sh, "test gremlins rename regression")
    assert r.returncode == 0, r.stderr
    gr_id = r.stdout.strip()

    state_dir = sh.state_root / "claude-gremlins" / gr_id
    log_path = state_dir / "log"
    assert wait_for_finished(state_dir, timeout=120), (
        f"pipeline did not finish; log:\n"
        f"{log_path.read_text(errors='replace')[-2000:] if log_path.exists() else '<log file missing>'}"
    )

    state = read_state(state_dir / "state.json")
    assert state["exit_code"] == 0, (
        f"expected exit 0; log tail:\n"
        f"{log_path.read_text(errors='replace')[-2000:] if log_path.exists() else '<log file missing>'}"
    )
