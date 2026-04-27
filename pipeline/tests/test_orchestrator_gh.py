"""Tests for pipeline.orchestrators.gh and supporting git helpers.

Uses FakeClaudeClient throughout — no real claude subprocess or gh CLI calls
(gh calls are monkeypatched at the subprocess.run level).
"""

import json
import pathlib
import subprocess

import pytest

from conftest import MINIMAL_EVENTS
from pipeline.clients.fake import FakeClaudeClient
from pipeline.git import (
    EmptyImpl,
    DirtyOnly,
    HeadAdvanced,
    DivergentHead,
    PreImplState,
    classify_impl_outcome,
    create_handoff_branch,
    record_pre_impl_state,
    sweep_stale_handoff_branches,
)
from pipeline.orchestrators.gh import gh_main, _parse_gh_args, _parse_issue_ref


# ---------------------------------------------------------------------------
# Helper: minimal stream-json event list containing a PR URL in a tool_result
# ---------------------------------------------------------------------------

def _issue_events(issue_url: str = "https://github.com/owner/repo/issues/42") -> list:
    return [
        {"type": "system", "subtype": "init", "session_id": "session-plan-1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-plan-1",
                        "name": "Bash",
                        "input": {"command": f"gh issue create --title 'foo'"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-plan-1",
                        "content": issue_url,
                    }
                ]
            },
        },
        {"type": "result", "subtype": "success"},
    ]


def _pr_events(pr_url: str = "https://github.com/owner/repo/pull/101") -> list:
    return [
        {"type": "system", "subtype": "init", "session_id": "session-commit-1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-pr-1",
                        "name": "Bash",
                        "input": {"command": "gh pr create --base main"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu-pr-1",
                        "content": pr_url,
                    }
                ]
            },
        },
        {"type": "result", "subtype": "success"},
    ]


IMPL_EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "session-impl-1"},
    {"type": "result", "subtype": "success"},
]


# ---------------------------------------------------------------------------
# Common patches for gh_main smoke tests
# ---------------------------------------------------------------------------

def _patch_common(monkeypatch, tmp_path, *, state_data: dict = None):
    """Apply standard monkeypatches for gh_main smoke tests."""
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda n: f"/fake/{n}" if n in ("claude", "gh") else None)
    monkeypatch.setattr("pipeline.orchestrators.gh.install_signal_handlers", lambda c: None)
    monkeypatch.setattr("pipeline.orchestrators.gh.get_repo", lambda: "owner/repo")
    monkeypatch.setattr("pipeline.orchestrators.gh._load_core_principles", lambda: "Be good.")

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setattr("pipeline.orchestrators.gh.resolve_session_dir", lambda: session_dir)

    state_file = tmp_path / "state.json"
    initial = {
        "id": "gr-test",
        "kind": "ghgremlin",
        "stage": "starting",
        "bail_class": "",
    }
    if state_data:
        initial.update(state_data)
    state_file.write_text(json.dumps(initial))
    monkeypatch.setattr("pipeline.orchestrators.gh.resolve_state_file", lambda: state_file)

    # patch_state reads/writes state_file — let it use the real implementation
    # (no-op without GR_ID but the file is explicitly patched via resolve_state_file)
    monkeypatch.setattr("pipeline.orchestrators.gh.patch_state", lambda **kw: None)

    # set_stage is a no-op in tests (no GR_ID env var set)
    # — the real implementation already no-ops without GR_ID.

    return session_dir, state_file


_real_subprocess_run = subprocess.run


def _make_gh_subprocess(
    *,
    issue_body: str = "# Plan\nDo stuff.\n",
    copilot_state: str = "APPROVED",
    pr_diff: str = "diff --git a/f b/f\n",
):
    """Return a subprocess.run replacement that stubs gh CLI calls and delegates
    all other commands (e.g. git) to the real subprocess.run."""

    def fake_run(cmd, *args, **kwargs):
        prog = cmd[0] if cmd else ""
        if prog != "gh":
            # Let git and other real commands through unchanged
            return _real_subprocess_run(cmd, *args, **kwargs)

        sub = cmd[1] if len(cmd) > 1 else ""
        # gh issue view ... --json body --jq .body
        if sub == "issue" and "view" in cmd and "--jq" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=issue_body + "\n", stderr="")
        # gh issue view ... --json number,url,body  (for --plan issue-ref resolution)
        if sub == "issue" and "view" in cmd and "--json" in cmd:
            num = cmd[3] if len(cmd) > 3 else "42"
            data = json.dumps({
                "number": int(num),
                "url": f"https://github.com/owner/repo/issues/{num}",
                "body": issue_body,
            })
            return subprocess.CompletedProcess(cmd, 0, stdout=data, stderr="")
        # gh pr edit (request-copilot)
        if sub == "pr" and "edit" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        # gh pr diff
        if sub == "pr" and "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=pr_diff, stderr="")
        # gh api (wait-copilot)
        if sub == "api":
            return subprocess.CompletedProcess(cmd, 0, stdout=copilot_state + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


# ---------------------------------------------------------------------------
# classify_impl_outcome — all four branches (pure git, real temp repo)
# ---------------------------------------------------------------------------

def _init_git_repo(path: pathlib.Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_classify_empty_impl(tmp_path):
    _init_git_repo(tmp_path)
    pre = record_pre_impl_state(cwd=str(tmp_path))
    outcome = classify_impl_outcome(pre, cwd=str(tmp_path))
    assert isinstance(outcome, EmptyImpl)


def test_classify_dirty_only(tmp_path):
    _init_git_repo(tmp_path)
    pre = record_pre_impl_state(cwd=str(tmp_path))
    (tmp_path / "new.txt").write_text("dirty\n")
    outcome = classify_impl_outcome(pre, cwd=str(tmp_path))
    assert isinstance(outcome, DirtyOnly)


def test_classify_head_advanced(tmp_path):
    _init_git_repo(tmp_path)
    pre = record_pre_impl_state(cwd=str(tmp_path))
    (tmp_path / "feat.txt").write_text("feature\n")
    subprocess.run(["git", "add", "feat.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat"], cwd=tmp_path, check=True, capture_output=True)
    outcome = classify_impl_outcome(pre, cwd=str(tmp_path))
    assert isinstance(outcome, HeadAdvanced)
    assert outcome.commit_count == 1


def test_classify_divergent_head(tmp_path):
    _init_git_repo(tmp_path)
    pre = record_pre_impl_state(cwd=str(tmp_path))

    # Create an orphan branch (diverges from the init commit)
    subprocess.run(["git", "checkout", "--orphan", "orphan"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "rm", "-rf", "."], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "orphan.txt").write_text("orphan\n")
    subprocess.run(["git", "add", "orphan.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "orphan commit"], cwd=tmp_path, check=True, capture_output=True)

    outcome = classify_impl_outcome(pre, cwd=str(tmp_path))
    assert isinstance(outcome, DivergentHead)


# ---------------------------------------------------------------------------
# impl-handoff branch lifecycle (real temp git repo)
# ---------------------------------------------------------------------------

def test_handoff_branch_lifecycle(tmp_path):
    """create_handoff_branch creates a branch at current HEAD; sweep_stale removes merged ones."""
    _init_git_repo(tmp_path)

    # Make an implementation commit
    (tmp_path / "impl.txt").write_text("work\n")
    subprocess.run(["git", "add", "impl.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "impl"], cwd=tmp_path, check=True, capture_output=True)

    pre = PreImplState(
        head=subprocess.run(
            ["git", "rev-parse", "HEAD~1"], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip(),
        branch="",
    )

    handoff = create_handoff_branch(pre, cwd=str(tmp_path))
    assert handoff.startswith("ghgremlin-impl-handoff-")

    # Verify we're on the handoff branch
    current = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current == handoff

    # Create a "stale" handoff branch pointing to the same HEAD (simulating prior run)
    stale_branch = "ghgremlin-impl-handoff-9999"
    subprocess.run(
        ["git", "branch", stale_branch],
        cwd=tmp_path, check=True, capture_output=True,
    )

    # sweep_stale should delete the merged stale branch
    sweep_stale_handoff_branches(handoff, cwd=str(tmp_path))

    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/ghgremlin-impl-handoff-*"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    # Stale merged branch should be gone; current handoff should still exist
    assert handoff in refs
    assert stale_branch not in refs


# ---------------------------------------------------------------------------
# _parse_gh_args — arg parsing unit tests
# ---------------------------------------------------------------------------

def test_parse_instructions():
    args = _parse_gh_args(["add a login page"])
    # A single quoted string arrives as one element in argv
    assert args.instructions == ["add a login page"]
    assert args.plan_source is None
    assert args.resume_from is None
    assert args.model is None
    assert args.ref == ""


def test_parse_plan_source():
    args = _parse_gh_args(["--plan", "42"])
    assert args.plan_source == "42"
    assert args.instructions == []


def test_parse_model():
    args = _parse_gh_args(["--model", "claude-opus-4-7", "do stuff"])
    assert args.model == "claude-opus-4-7"


def test_parse_resume_from_commit_pr_rewinds(capsys):
    args = _parse_gh_args(["--plan", "42", "--resume-from", "commit-pr"])
    assert args.resume_from == "implement"
    captured = capsys.readouterr()
    assert "rewinding to implement" in captured.err


def test_parse_plan_and_instructions_mutual_exclusion():
    with pytest.raises(SystemExit):
        _parse_gh_args(["--plan", "42", "also some instructions"])


# ---------------------------------------------------------------------------
# _parse_issue_ref unit tests
# ---------------------------------------------------------------------------

def test_parse_issue_ref_numeric():
    repo, ref = _parse_issue_ref("42", "owner/repo")
    assert repo == "owner/repo"
    assert ref == "42"


def test_parse_issue_ref_hash_prefix():
    repo, ref = _parse_issue_ref("#42", "owner/repo")
    assert repo == "owner/repo"
    assert ref == "42"


def test_parse_issue_ref_cross_repo():
    repo, ref = _parse_issue_ref("other/repo#7", "owner/repo")
    assert repo == "other/repo"
    assert ref == "7"


def test_parse_issue_ref_full_url():
    repo, ref = _parse_issue_ref(
        "https://github.com/owner/repo/issues/123", "owner/repo"
    )
    assert repo == "owner/repo"
    assert ref == "123"


def test_parse_issue_ref_invalid():
    repo, ref = _parse_issue_ref("not-a-ref", "owner/repo")
    assert repo is None
    assert ref is None


# ---------------------------------------------------------------------------
# gh_main — smoke test: --plan issue-ref mode (plan stage skipped)
# ---------------------------------------------------------------------------

class _CommittingClient(FakeClaudeClient):
    """FakeClaudeClient that creates a git commit when the implement label runs."""

    def __init__(self, *args, git_dir: pathlib.Path = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._git_dir = git_dir

    def run(self, prompt, *, label, **kwargs):
        if label == "implement" and self._git_dir is not None:
            # Simulate implement creating a commit
            (self._git_dir / "impl.txt").write_text("impl\n")
            subprocess.run(["git", "add", "impl.txt"], cwd=self._git_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "impl: add impl.txt"],
                cwd=self._git_dir, check=True, capture_output=True,
            )
        return super().run(prompt, label=label, **kwargs)


def test_plan_mode_skips_plan_stage(tmp_path, monkeypatch):
    """--plan <issue-ref> resolves issue body without running the plan stage."""
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    session_dir, state_file = _patch_common(monkeypatch, tmp_path)

    monkeypatch.setattr(
        subprocess, "run",
        _make_gh_subprocess(issue_body="# Plan\nDo stuff.\n"),
    )

    scope_lens = (tmp_path / "lenses")
    scope_lens.mkdir(parents=True, exist_ok=True)
    # scope_review_pr.md is read by ghreview stage; patch the whole stage instead
    monkeypatch.setattr(
        "pipeline.orchestrators.gh.run_ghreview_stage",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "pipeline.orchestrators.gh.run_wait_copilot_stage",
        lambda **kw: "APPROVED",
    )
    monkeypatch.setattr(
        "pipeline.orchestrators.gh.run_request_copilot_stage",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "pipeline.orchestrators.gh.run_ghaddress_stage",
        lambda **kw: None,
    )

    client = _CommittingClient(
        git_dir=tmp_path,
        fixtures={
            "implement": IMPL_EVENTS,
            "commit-pr": _pr_events(),
        },
    )

    result = gh_main(["--plan", "42"], client=client)
    assert result == 0

    labels = [c.label for c in client.calls]
    # plan stage must NOT have been called
    assert "plan" not in labels
    assert "implement" in labels
    assert "commit-pr" in labels


def test_model_forwarded_to_all_stages(tmp_path, monkeypatch):
    """--model is forwarded to every client.run call."""
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    session_dir, state_file = _patch_common(monkeypatch, tmp_path)

    monkeypatch.setattr(
        subprocess, "run",
        _make_gh_subprocess(issue_body="# Plan\nDo stuff.\n"),
    )
    monkeypatch.setattr("pipeline.orchestrators.gh.run_ghreview_stage", lambda **kw: None)
    monkeypatch.setattr("pipeline.orchestrators.gh.run_wait_copilot_stage", lambda **kw: "APPROVED")
    monkeypatch.setattr("pipeline.orchestrators.gh.run_request_copilot_stage", lambda **kw: None)
    monkeypatch.setattr("pipeline.orchestrators.gh.run_ghaddress_stage", lambda **kw: None)

    client = _CommittingClient(
        git_dir=tmp_path,
        fixtures={
            "implement": IMPL_EVENTS,
            "commit-pr": _pr_events(),
        },
    )

    result = gh_main(["--plan", "42", "--model", "claude-opus-4-7"], client=client)
    assert result == 0

    for call in client.calls:
        assert call.model == "claude-opus-4-7", f"stage {call.label!r} got model={call.model!r}"


def test_resume_from_implement(tmp_path, monkeypatch):
    """--resume-from implement reloads issue_url from state.json and runs implement onward."""
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    state_data = {
        "issue_url": "https://github.com/owner/repo/issues/99",
        "issue_num": "99",
    }
    session_dir, state_file = _patch_common(monkeypatch, tmp_path, state_data=state_data)

    monkeypatch.setattr(
        subprocess, "run",
        _make_gh_subprocess(issue_body="# Resumed Plan\nDo more stuff.\n"),
    )
    monkeypatch.setattr("pipeline.orchestrators.gh.run_ghreview_stage", lambda **kw: None)
    monkeypatch.setattr("pipeline.orchestrators.gh.run_wait_copilot_stage", lambda **kw: "APPROVED")
    monkeypatch.setattr("pipeline.orchestrators.gh.run_request_copilot_stage", lambda **kw: None)
    monkeypatch.setattr("pipeline.orchestrators.gh.run_ghaddress_stage", lambda **kw: None)

    client = _CommittingClient(
        git_dir=tmp_path,
        fixtures={
            "implement": IMPL_EVENTS,
            "commit-pr": _pr_events(),
        },
    )

    # Simulate that the state.json has issue_url so _read_state_field can return it.
    # We need to reach into the patched resolve_state_file to make the pre-loop read work.
    import pipeline.orchestrators.gh as _gh_mod
    original_read = _gh_mod._read_state_field

    def _fake_read(sf, field):
        if field == "issue_url":
            return "https://github.com/owner/repo/issues/99"
        if field == "issue_num":
            return "99"
        return ""

    monkeypatch.setattr(_gh_mod, "_read_state_field", _fake_read)
    monkeypatch.setattr(_gh_mod, "_fetch_issue_body", lambda num, repo: "# Resumed Plan\nDo stuff.\n")

    result = gh_main(["--plan", "99", "--resume-from", "implement"], client=client)
    assert result == 0

    labels = [c.label for c in client.calls]
    assert "plan" not in labels
    assert "implement" in labels


def test_resume_from_ghreview(tmp_path, monkeypatch):
    """--resume-from ghreview reloads pr_url from state.json and skips earlier stages."""
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    session_dir, state_file = _patch_common(monkeypatch, tmp_path)

    import pipeline.orchestrators.gh as _gh_mod

    def _fake_read(sf, field):
        if field == "issue_url":
            return "https://github.com/owner/repo/issues/5"
        if field == "issue_num":
            return "5"
        if field == "pr_url":
            return "https://github.com/owner/repo/pull/200"
        if field == "model":
            return ""
        return ""

    monkeypatch.setattr(_gh_mod, "_read_state_field", _fake_read)
    monkeypatch.setattr(_gh_mod, "_fetch_issue_body", lambda num, repo: "# Plan\nContent.\n")

    ghreview_called = []
    monkeypatch.setattr(
        "pipeline.orchestrators.gh.run_ghreview_stage",
        lambda **kw: ghreview_called.append(kw["pr_url"]),
    )
    monkeypatch.setattr("pipeline.orchestrators.gh.run_wait_copilot_stage", lambda **kw: "APPROVED")
    monkeypatch.setattr("pipeline.orchestrators.gh.run_request_copilot_stage", lambda **kw: None)
    monkeypatch.setattr("pipeline.orchestrators.gh.run_ghaddress_stage", lambda **kw: None)
    monkeypatch.setattr(subprocess, "run", _make_gh_subprocess())

    client = FakeClaudeClient(fixtures={})

    result = gh_main(["--plan", "5", "--resume-from", "ghreview"], client=client)
    assert result == 0

    # No client.run calls (plan/implement/commit-pr all skipped)
    assert client.calls == []
    # ghreview was called with the correct PR URL
    assert ghreview_called == ["https://github.com/owner/repo/pull/200"]
