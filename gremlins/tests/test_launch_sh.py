"""Shell integration tests for skills/_bg/launch.sh.

Drives launch.sh as a real subprocess with a fake `claude` (and `gh` where
needed) on PATH and a real throwaway git repo. Verifies the on-disk
contracts the bash script owns: state.json layout, pipeline_args
persistence, instructions sidecar, --resume rehydration, and unknown-flag
rejection.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

from fixtures.shell_env import (
    REPO_ROOT,
    read_state,
    setup_shell_env,
    wait_for_finished,
)

LAUNCH_SH = REPO_ROOT / "skills" / "_bg" / "launch.sh"


def _run_launch(env, *args, cwd=None, timeout=30):
    return subprocess.run(
        [str(LAUNCH_SH), *args],
        cwd=str(cwd) if cwd else None,
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_launch_sh_local_creates_expected_state_layout(tmp_path):
    """launch.sh localgremlin: state dir layout + state.json shape."""
    sh = setup_shell_env(tmp_path)
    r = _run_launch(
        sh.env, "--print-id", "localgremlin", "-i", "sonnet", "test instructions",
        cwd=sh.repo,
    )
    assert r.returncode == 0, f"launch.sh failed: {r.stderr}"
    gr_id = r.stdout.strip()
    assert gr_id, "expected --print-id to print a gremlin id"

    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert state_dir.is_dir(), f"state dir missing: {state_dir}"

    state_file = state_dir / "state.json"
    assert state_file.exists()

    instructions_file = state_dir / "instructions.txt"
    assert instructions_file.exists()
    assert instructions_file.read_text(encoding="utf-8") == "-i sonnet test instructions"

    state = read_state(state_file)
    assert state["id"] == gr_id
    assert state["kind"] == "localgremlin"
    assert state["setup_kind"] == "worktree-branch"
    assert state["branch"] == f"bg/localgremlin/{gr_id}"
    assert state["pipeline_args"] == ["-i", "sonnet"]
    assert state["instructions"].endswith("test instructions")
    assert "workdir" in state and state["workdir"]

    # Wait for the pipeline to terminate so the test doesn't leave
    # stray subprocesses behind. Don't assert outcome here — that's a
    # separate test's concern.
    wait_for_finished(state_dir, timeout=60)


def test_launch_sh_writes_worktree(tmp_path):
    """localgremlin sets up a `git worktree add -b` worktree at the named branch."""
    sh = setup_shell_env(tmp_path)
    r = _run_launch(sh.env, "--print-id", "localgremlin", "test", cwd=sh.repo)
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state = read_state(sh.state_root / "claude-gremlins" / gr_id / "state.json")
    workdir = pathlib.Path(state["workdir"])
    # Worktree dir exists and is a real checkout while the gremlin is
    # still running. After finish.sh removes it on success the
    # assertion would race, so check immediately.
    assert workdir.is_dir(), f"worktree should exist immediately after launch: {workdir}"
    # `git worktree list` from the source repo must mention this workdir.
    r = subprocess.run(
        ["git", "-C", str(sh.repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    )
    assert str(workdir) in r.stdout
    wait_for_finished(sh.state_root / "claude-gremlins" / gr_id, timeout=60)


def test_launch_sh_persists_pipeline_args_with_models(tmp_path):
    """Pipeline-level model flags before the positional are persisted verbatim."""
    sh = setup_shell_env(tmp_path)
    r = _run_launch(
        sh.env, "--print-id", "localgremlin",
        "-p", "opus", "-i", "sonnet", "-x", "haiku",
        "test", cwd=sh.repo,
    )
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state = read_state(sh.state_root / "claude-gremlins" / gr_id / "state.json")
    assert state["pipeline_args"] == ["-p", "opus", "-i", "sonnet", "-x", "haiku"]
    wait_for_finished(sh.state_root / "claude-gremlins" / gr_id, timeout=60)


def test_launch_sh_unknown_flag_rejected(tmp_path):
    """Unknown leading flags must abort launch.sh non-zero before any state."""
    sh = setup_shell_env(tmp_path)
    r = _run_launch(sh.env, "--bogus-flag", "localgremlin", "test", cwd=sh.repo)
    assert r.returncode != 0
    assert "unknown flag" in (r.stderr.lower() + r.stdout.lower())
    # No state dir should have been created.
    listing = list((sh.state_root / "claude-gremlins").glob("*")) \
        if (sh.state_root / "claude-gremlins").exists() else []
    assert listing == [], f"unknown-flag failure must not create state: {listing}"


def test_launch_sh_invalid_kind_rejected(tmp_path):
    """An unrecognized `kind` positional argument must abort with non-zero."""
    sh = setup_shell_env(tmp_path)
    r = _run_launch(sh.env, "notakind", "test", cwd=sh.repo)
    assert r.returncode != 0
    assert "invalid kind" in (r.stderr.lower() + r.stdout.lower())


def test_launch_sh_resume_rehydrates_pipeline_args(tmp_path):
    """`launch.sh --resume <id>` reloads pipeline_args + bumps rescue_count."""
    sh = setup_shell_env(tmp_path)
    # Force the original gremlin to fail at the plan stage so it leaves a
    # state dir + finished marker for resume to act on.
    sh.env["FAKE_CLAUDE_FAIL_AT"] = "plan"
    r = _run_launch(
        sh.env, "--print-id", "localgremlin", "-i", "sonnet", "test resume",
        cwd=sh.repo,
    )
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=30), \
        "fake-fail gremlin should have terminated quickly"

    pre_state = read_state(state_dir / "state.json")
    assert pre_state.get("status") in ("stopped", "done", "running"), pre_state
    pre_rescue_count = pre_state.get("rescue_count", 0)

    # Now flip the failure off and resume; verify pipeline_args replay and
    # rescue_count increments.
    sh.env["FAKE_CLAUDE_FAIL_AT"] = ""
    r = _run_launch(sh.env, "--resume", gr_id, cwd=sh.repo, timeout=10)
    assert r.returncode == 0, f"resume failed: {r.stderr}"

    # State.json is rewritten by --resume *before* the spawn — read fresh.
    post_state = read_state(state_dir / "state.json")
    assert post_state["rescue_count"] == pre_rescue_count + 1
    assert post_state["status"] == "running"
    assert post_state["resumed_from_stage"] == "plan"
    assert post_state["pipeline_args"] == ["-i", "sonnet"]
    # The `finished` marker must have been cleared so liveness no longer
    # treats the gremlin as terminal.
    assert not (state_dir / "finished").exists()

    # Wait for the relaunched run to settle. Don't assert outcome — the
    # plan/implement/review pipeline may not produce a working tree
    # diff in test conditions; we only care that bookkeeping is right.
    wait_for_finished(state_dir, timeout=60)


def test_launch_sh_resume_refuses_running_gremlin(tmp_path):
    """`--resume` must refuse a gremlin whose recorded pid is still alive."""
    sh = setup_shell_env(tmp_path)
    state_dir = sh.state_root / "claude-gremlins" / "fake-id-deadbe"
    state_dir.mkdir(parents=True)
    # Use our own pid as a known-live process. Status=running + live pid
    # is the precondition launch.sh's resume guard refuses on.
    state = {
        "id": "fake-id-deadbe",
        "kind": "localgremlin",
        "workdir": str(sh.repo),
        "branch": "bg/localgremlin/fake-id-deadbe",
        "stage": "plan",
        "status": "running",
        "pid": os.getpid(),
        "pipeline_args": [],
    }
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (state_dir / "instructions.txt").write_text("foo", encoding="utf-8")

    r = _run_launch(sh.env, "--resume", "fake-id-deadbe", cwd=sh.repo, timeout=10)
    assert r.returncode != 0
    assert "still running" in (r.stderr + r.stdout)


def test_launch_sh_resume_refuses_finished_success(tmp_path):
    """`--resume` must refuse a gremlin that already finished with exit_code=0."""
    sh = setup_shell_env(tmp_path)
    state_dir = sh.state_root / "claude-gremlins" / "fake-id-success"
    state_dir.mkdir(parents=True)
    state = {
        "id": "fake-id-success",
        "kind": "localgremlin",
        "workdir": str(sh.repo),
        "branch": "bg/localgremlin/fake-id-success",
        "stage": "address-code",
        "status": "done",
        "exit_code": 0,
        "pipeline_args": [],
    }
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (state_dir / "finished").touch()
    (state_dir / "instructions.txt").write_text("foo", encoding="utf-8")

    r = _run_launch(sh.env, "--resume", "fake-id-success", cwd=sh.repo, timeout=10)
    assert r.returncode != 0
    assert "finished successfully" in (r.stderr + r.stdout)


def test_launch_sh_plan_file_resolved_to_absolute(tmp_path):
    """A relative --plan file path must be normalized to absolute in state.json."""
    sh = setup_shell_env(tmp_path)
    plan_file = sh.repo / "my-plan.md"
    plan_file.write_text("# My Plan Heading\n\nBody.\n", encoding="utf-8")

    r = _run_launch(
        sh.env, "--print-id", "localgremlin", "--plan", "my-plan.md",
        cwd=sh.repo, timeout=15,
    )
    assert r.returncode == 0, r.stderr
    gr_id = r.stdout.strip()
    state = read_state(sh.state_root / "claude-gremlins" / gr_id / "state.json")
    # pipeline_args contains an absolutized path.
    assert "--plan" in state["pipeline_args"]
    idx = state["pipeline_args"].index("--plan")
    persisted = state["pipeline_args"][idx + 1]
    assert os.path.isabs(persisted), f"path should be absolute: {persisted}"
    assert pathlib.Path(persisted).name == "my-plan.md"
    # H1 from plan file becomes the description.
    assert state["description"].startswith("My Plan Heading")
    wait_for_finished(sh.state_root / "claude-gremlins" / gr_id, timeout=60)


def test_launch_sh_plan_and_positional_mutex(tmp_path):
    """`--plan` and positional instructions are mutually exclusive."""
    sh = setup_shell_env(tmp_path)
    plan_file = sh.repo / "plan.md"
    plan_file.write_text("# X\n", encoding="utf-8")
    r = _run_launch(
        sh.env, "localgremlin", "--plan", str(plan_file), "extra positional",
        cwd=sh.repo, timeout=10,
    )
    assert r.returncode != 0
    assert "mutually exclusive" in (r.stderr + r.stdout)


def test_launch_sh_localgremlin_empty_plan_file_rejected(tmp_path):
    """localgremlin's --plan must reject an empty file before creating state."""
    sh = setup_shell_env(tmp_path)
    empty = sh.repo / "empty-plan.md"
    empty.write_text("", encoding="utf-8")
    r = _run_launch(
        sh.env, "localgremlin", "--plan", str(empty),
        cwd=sh.repo, timeout=10,
    )
    assert r.returncode != 0
    assert "empty" in (r.stderr + r.stdout)


def test_launch_sh_child_inherits_project_root_from_parent(tmp_path):
    """--parent causes the child's project_root to come from the parent's state.json."""
    sh = setup_shell_env(tmp_path)

    # Create a fake parent state directory with a known project_root.
    parent_id = "fake-parent-aabbcc"
    parent_state_dir = sh.state_root / "claude-gremlins" / parent_id
    parent_state_dir.mkdir(parents=True)
    parent_root = sh.repo  # a real directory so IS_GIT check works
    (parent_state_dir / "state.json").write_text(
        json.dumps({"id": parent_id, "project_root": str(parent_root)}),
        encoding="utf-8",
    )

    r = _run_launch(
        sh.env, "--print-id", "--parent", parent_id,
        "localgremlin", "test child inheritance",
        cwd=sh.repo, timeout=30,
    )
    assert r.returncode == 0, f"launch.sh failed: {r.stderr}"
    gr_id = r.stdout.strip()

    state = read_state(sh.state_root / "claude-gremlins" / gr_id / "state.json")
    assert state["project_root"] == str(parent_root)

    wait_for_finished(sh.state_root / "claude-gremlins" / gr_id, timeout=60)


def test_launch_sh_child_falls_back_when_parent_state_missing(tmp_path):
    """--parent with a non-existent parent id falls back to git rev-parse and succeeds."""
    sh = setup_shell_env(tmp_path)

    r = _run_launch(
        sh.env, "--print-id", "--parent", "nonexistent-parent-id",
        "localgremlin", "test fallback",
        cwd=sh.repo, timeout=30,
    )
    assert r.returncode == 0, f"launch.sh failed: {r.stderr}"
    gr_id = r.stdout.strip()

    state = read_state(sh.state_root / "claude-gremlins" / gr_id / "state.json")
    # Should fall back to the git toplevel of sh.repo (where launch was invoked from).
    assert state["project_root"] == str(sh.repo)

    wait_for_finished(sh.state_root / "claude-gremlins" / gr_id, timeout=60)
