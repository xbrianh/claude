"""Shell integration tests for the ghgremlin shim + pipeline.

Drives the gh pipeline end-to-end with a fake `claude` and `gh` on PATH
and a real throwaway git repo (with a bare `origin` remote so the
worktree's `git fetch origin` and `worktree add origin/main` resolve).

Covers the contracts the GitHub issue calls out:
- ``--plan <file>`` posts the file as a GH issue and runs the chain
- ``--plan <issue-ref>`` resolves an existing issue and runs the chain
- ``--model`` reaches every claude invocation
- ``--resume-from <stage>`` skips earlier stages
- unknown leading flags abort before any state is created
"""

from __future__ import annotations

import os
import subprocess

from fixtures.shell_env import (
    REPO_ROOT,
    read_fake_claude_log,
    read_state,
    setup_shell_env,
    wait_for_finished,
)

LAUNCH_SH = REPO_ROOT / "skills" / "_bg" / "launch.sh"
SHIM_SH = REPO_ROOT / "skills" / "ghgremlin" / "ghgremlin.sh"
PIPELINE_PARENT = str(REPO_ROOT)


def _launch_gh(sh, *args, timeout=30):
    return subprocess.run(
        [str(LAUNCH_SH), "--print-id", "ghgremlin", *args],
        cwd=str(sh.repo), env=sh.env,
        capture_output=True, text=True, timeout=timeout,
    )


def _launch_raw(sh, *args, timeout=30):
    """launch.sh with raw argv (no auto-injected --print-id or kind).
    Used by the unknown-leading-flag rejection test which depends on argv
    order in launch.sh's pre-kind flag-parsing loop."""
    return subprocess.run(
        [str(LAUNCH_SH), *args],
        cwd=str(sh.repo), env=sh.env,
        capture_output=True, text=True, timeout=timeout,
    )


def _run_shim_directly(sh, *args, timeout=30):
    """Invoke the ghgremlin.sh shim directly (no launch.sh, no background).

    The shim execs `python -m pipeline.cli gh "$@"`, so this exercises the
    arg-forwarding contract end-to-end without the worktree+state machinery.
    """
    existing = sh.env.get("PYTHONPATH", "")
    new_pp = f"{PIPELINE_PARENT}{os.pathsep + existing if existing else ''}"
    env = {**sh.env, "PYTHONPATH": new_pp}
    return subprocess.run(
        [str(SHIM_SH), *args],
        cwd=str(sh.repo), env=env,
        capture_output=True, text=True, timeout=timeout,
    )


def test_ghgremlin_unknown_flag_rejected_at_launch_sh(tmp_path):
    """An unknown flag *before* the kind in launch.sh must error before any state."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    r = _launch_raw(sh, "--bogus", "ghgremlin", "test", timeout=10)
    assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "unknown flag" in (r.stderr.lower() + r.stdout.lower())


def test_ghgremlin_unknown_pipeline_flag_rejected(tmp_path):
    """Unknown flag in ghgremlin's own arg space (after the kind) must abort."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    r = _run_shim_directly(sh, "--no-such-flag", "test", timeout=10)
    assert r.returncode != 0
    # argparse emits to stderr; pipeline.cli's die() also goes to stderr.
    assert "unrecognized" in r.stderr.lower() or "error" in r.stderr.lower()


def test_ghgremlin_shim_forwards_to_pipeline_cli(tmp_path):
    """ghgremlin.sh shim is a thin exec into `python -m pipeline.cli gh`.

    Invoke it with no args and confirm the orchestrator emits its own
    usage error (proving control reached pipeline.cli gh's argparse).
    """
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    r = _run_shim_directly(sh, timeout=10)
    # No instructions and no --plan → usage failure with non-zero exit.
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "usage" in combined.lower() or "instructions" in combined.lower()


def test_ghgremlin_plan_file_full_chain(tmp_path):
    """`--plan <file>`: file is posted as an issue, plan stage skipped,
    implement → commit-pr → request-copilot → ghreview → wait-copilot →
    ghaddress all run."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    plan_file = sh.repo / "spec.md"
    plan_file.write_text("# Add login\n\n## Tasks\n- [ ] login\n", encoding="utf-8")

    r = _launch_gh(sh, "--plan", str(plan_file))
    assert r.returncode == 0, r.stderr
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120), \
        f"pipeline did not finish; log:\n{(state_dir / 'log').read_text(errors='replace')[-3000:]}"

    state = read_state(state_dir / "state.json")
    assert state["status"] == "done", \
        f"expected done, got {state.get('status')}; log tail:\n" \
        f"{(state_dir / 'log').read_text(errors='replace')[-3000:]}"
    assert state["exit_code"] == 0
    assert state.get("issue_url", "").startswith("https://github.com/")
    assert state.get("pr_url", "").startswith("https://github.com/")

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    # plan-title runs because --plan <file> needs a title via claude text mode.
    assert "plan-title" in stages
    # plan stage (the /ghplan one) must NOT run because --plan supplied.
    assert "ghplan" not in stages
    assert "implement-gh" in stages
    assert "commit-pr" in stages
    assert "ghreview" in stages
    assert "ghaddress" in stages


def test_ghgremlin_plan_issue_ref(tmp_path):
    """`--plan 42`: resolve issue body via gh, no plan-title call needed."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    sh.env["FAKE_GH_ISSUE_BODY"] = "# Resolve issue 42\n\nDo the thing.\n"

    r = _launch_gh(sh, "--plan", "42")
    assert r.returncode == 0, r.stderr
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120), \
        f"pipeline did not finish; log:\n{(state_dir / 'log').read_text(errors='replace')[-3000:]}"

    state = read_state(state_dir / "state.json")
    assert state["status"] == "done"
    # Plan body got snapshot to artifacts.
    plan_md = state_dir / "artifacts" / "plan.md"
    assert plan_md.exists()
    assert "Resolve issue 42" in plan_md.read_text(encoding="utf-8")

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    # No plan-title (no file to title), no /ghplan (--plan supplied).
    assert "plan-title" not in stages
    assert "ghplan" not in stages
    assert "implement-gh" in stages


def test_ghgremlin_model_forwarded_to_all_stages(tmp_path):
    """`--model X` is forwarded as `--model X` to every claude invocation."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    r = _launch_gh(sh, "--plan", "7", "--model", "claude-opus-4-7")
    assert r.returncode == 0, r.stderr
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    log = read_fake_claude_log(sh.fake_claude_log)
    # Every recorded claude call must carry the model flag.
    pipeline_stages = {"implement-gh", "commit-pr", "ghreview", "ghaddress"}
    observed_stages = {e["stage"] for e in log}
    # Fail fast if any expected stage was silently skipped — otherwise the
    # per-stage loop below would vacuously pass on a missing stage.
    assert pipeline_stages.issubset(observed_stages), \
        f"missing expected stages: {pipeline_stages - observed_stages}; observed: {observed_stages}"
    for entry in log:
        if entry["stage"] in pipeline_stages:
            assert entry["model"] == "claude-opus-4-7", \
                f"stage {entry['stage']} got model={entry['model']!r}"


def test_ghgremlin_resume_from_ghreview(tmp_path):
    """`--resume-from ghreview` skips plan/implement/commit-pr/request-copilot."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    plan_file = sh.repo / "spec.md"
    plan_file.write_text("# Plan\n\n## Tasks\n- [ ] foo\n", encoding="utf-8")

    # First, a normal run that gets through commit-pr, so state.json carries
    # issue_url / pr_url for the resume to pick up.
    r = _launch_gh(sh, "--plan", str(plan_file))
    assert r.returncode == 0, r.stderr
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    # Snapshot what was called the first time, then clear the log.
    initial_calls = read_fake_claude_log(sh.fake_claude_log)
    sh.fake_claude_log.write_text("", encoding="utf-8")

    # Resume from ghreview. Use launch.sh --resume which re-applies pipeline_args.
    # But --resume-from isn't part of the original pipeline_args; we need a
    # second --resume-from passed through. Simplest: re-launch the orchestrator
    # via the shim with the persisted state pointing to the same artifacts.
    sh.env["GR_ID"] = gr_id  # so resolve_session_dir picks the same artifacts/
    r = _run_shim_directly(
        sh, "--plan", str(plan_file), "--resume-from", "ghreview",
        timeout=60,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}"

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    assert "implement-gh" not in stages, f"resume should skip implement: {stages}"
    assert "commit-pr" not in stages, f"resume should skip commit-pr: {stages}"
    # ghreview onward must have run.
    assert "ghreview" in stages
    assert "ghaddress" in stages

    # First run also ran the full chain; sanity-check.
    initial_stages = [e["stage"] for e in initial_calls]
    assert "implement-gh" in initial_stages


def test_ghgremlin_resume_from_implement(tmp_path):
    """`--resume-from implement`: plan stage skipped (no /ghplan), but the
    pipeline can still rehydrate issue body from state.json."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    # Pre-seed a state.json + plan.md to simulate a prior run.
    plan_file = sh.repo / "spec.md"
    plan_file.write_text("# Plan resume\n", encoding="utf-8")

    # First run gets through implement+commit-pr.
    r = _launch_gh(sh, "--plan", str(plan_file))
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    # Clear log; resume from implement.
    sh.fake_claude_log.write_text("", encoding="utf-8")
    sh.env["GR_ID"] = gr_id
    r = _run_shim_directly(
        sh, "--plan", str(plan_file), "--resume-from", "implement",
        timeout=60,
    )
    assert r.returncode == 0, r.stderr

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    # No plan-title (plan.md already snapshot, --plan resolves from snapshot).
    assert "plan-title" not in stages, stages
    # implement onward.
    assert "implement-gh" in stages
    assert "commit-pr" in stages


def test_ghgremlin_resume_from_commit_pr(tmp_path):
    """`--resume-from commit-pr` rewrites to `--resume-from implement`
    (IMPL_SESSION is not persisted), so implement and later stages run."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    plan_file = sh.repo / "spec.md"
    plan_file.write_text("# Plan commit-pr resume\n", encoding="utf-8")

    r = _launch_gh(sh, "--plan", str(plan_file))
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    sh.fake_claude_log.write_text("", encoding="utf-8")
    sh.env["GR_ID"] = gr_id
    r = _run_shim_directly(
        sh, "--plan", str(plan_file), "--resume-from", "commit-pr",
        timeout=60,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}"
    # The rewind note must appear in stderr.
    assert "rewinding to implement" in r.stderr

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    assert "plan-title" not in stages, stages
    # implement onward (commit-pr rewound → implement).
    assert "implement-gh" in stages
    assert "commit-pr" in stages
    assert "ghreview" in stages
    assert "ghaddress" in stages


def test_ghgremlin_resume_from_request_copilot(tmp_path):
    """`--resume-from request-copilot` skips plan/implement/commit-pr."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    plan_file = sh.repo / "spec.md"
    plan_file.write_text("# Plan request-copilot resume\n", encoding="utf-8")

    r = _launch_gh(sh, "--plan", str(plan_file))
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    sh.fake_claude_log.write_text("", encoding="utf-8")
    sh.env["GR_ID"] = gr_id
    r = _run_shim_directly(
        sh, "--plan", str(plan_file), "--resume-from", "request-copilot",
        timeout=60,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}"

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    assert "implement-gh" not in stages, f"resume should skip implement: {stages}"
    assert "commit-pr" not in stages, f"resume should skip commit-pr: {stages}"
    # request-copilot calls gh (not claude) so it's not in the claude log,
    # but ghreview and ghaddress must have run.
    assert "ghreview" in stages
    assert "ghaddress" in stages


def test_ghgremlin_resume_from_wait_copilot(tmp_path):
    """`--resume-from wait-copilot` skips through ghreview; ghaddress must run."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    plan_file = sh.repo / "spec.md"
    plan_file.write_text("# Plan wait-copilot resume\n", encoding="utf-8")

    r = _launch_gh(sh, "--plan", str(plan_file))
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    sh.fake_claude_log.write_text("", encoding="utf-8")
    sh.env["GR_ID"] = gr_id
    r = _run_shim_directly(
        sh, "--plan", str(plan_file), "--resume-from", "wait-copilot",
        timeout=60,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}"

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    assert "implement-gh" not in stages, f"resume should skip implement: {stages}"
    assert "commit-pr" not in stages, f"resume should skip commit-pr: {stages}"
    assert "ghreview" not in stages, f"resume should skip ghreview: {stages}"
    # wait-copilot calls gh (not claude), but ghaddress must have run.
    assert "ghaddress" in stages


def test_ghgremlin_resume_from_ghaddress(tmp_path):
    """`--resume-from ghaddress` skips all earlier stages; only ghaddress runs."""
    sh = setup_shell_env(tmp_path, with_gh=True, with_origin=True)
    plan_file = sh.repo / "spec.md"
    plan_file.write_text("# Plan ghaddress resume\n", encoding="utf-8")

    r = _launch_gh(sh, "--plan", str(plan_file))
    assert r.returncode == 0
    gr_id = r.stdout.strip()
    state_dir = sh.state_root / "claude-gremlins" / gr_id
    assert wait_for_finished(state_dir, timeout=120)

    sh.fake_claude_log.write_text("", encoding="utf-8")
    sh.env["GR_ID"] = gr_id
    r = _run_shim_directly(
        sh, "--plan", str(plan_file), "--resume-from", "ghaddress",
        timeout=60,
    )
    assert r.returncode == 0, f"stderr:\n{r.stderr}\nstdout:\n{r.stdout}"

    log = read_fake_claude_log(sh.fake_claude_log)
    stages = [e["stage"] for e in log]
    assert "implement-gh" not in stages, f"resume should skip implement: {stages}"
    assert "commit-pr" not in stages, f"resume should skip commit-pr: {stages}"
    assert "ghreview" not in stages, f"resume should skip ghreview: {stages}"
    assert "ghaddress" in stages
