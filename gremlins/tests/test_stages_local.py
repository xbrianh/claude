import pathlib
import subprocess

import pytest

from conftest import MINIMAL_EVENTS
from gremlins.clients.fake import FakeClaudeClient
from gremlins.stages.address_code import run_address_code_stage
from gremlins.stages.implement import run_implement_stage
from gremlins.stages.plan import run_plan_stage


def _init_git_repo(path: pathlib.Path) -> None:
    """Create a git repo with a first commit so HEAD exists and the tree is clean."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# plan stage
# ---------------------------------------------------------------------------

def test_plan_stage_raises_when_file_absent(tmp_path):
    client = FakeClaudeClient(fixtures={"plan": MINIMAL_EVENTS})
    plan_file = tmp_path / "plan.md"
    # FakeClaudeClient won't create the plan file — stage must raise.
    with pytest.raises(RuntimeError, match="plan stage did not produce"):
        run_plan_stage(
            client=client,
            plan_model="sonnet",
            plan_file=plan_file,
            instructions="do stuff",
            raw_path=tmp_path / "stream-plan.jsonl",
        )
    assert len(client.calls) == 1
    assert client.calls[0].label == "plan"
    assert client.calls[0].model == "sonnet"


def test_plan_stage_succeeds_when_file_exists(tmp_path):
    plan_file = tmp_path / "plan.md"

    class _WritingClient(FakeClaudeClient):
        def run(self, prompt, *, label, **kwargs):
            plan_file.write_text("# Plan\nDo stuff.\n")
            return super().run(prompt, label=label, **kwargs)

    client = _WritingClient(fixtures={"plan": MINIMAL_EVENTS})
    run_plan_stage(
        client=client,
        plan_model="haiku",
        plan_file=plan_file,
        instructions="do stuff",
        raw_path=tmp_path / "stream-plan.jsonl",
    )
    assert plan_file.exists()
    assert client.calls[0].label == "plan"
    assert client.calls[0].model == "haiku"


# ---------------------------------------------------------------------------
# implement stage
# ---------------------------------------------------------------------------

def test_implement_stage_raises_on_empty_diff(tmp_path, monkeypatch):
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    _init_git_repo(git_dir)
    monkeypatch.chdir(git_dir)

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    client = FakeClaudeClient(fixtures={"implement": MINIMAL_EVENTS})
    with pytest.raises(RuntimeError, match="no changes"):
        run_implement_stage(
            client=client,
            impl_model="sonnet",
            plan_file=session_dir / "plan.md",
            plan_text="# Plan\nDo stuff.\n",
            core_principles="Be good.",
            session_dir=session_dir,
            is_git=True,
        )
    assert len(client.calls) == 1
    assert client.calls[0].label == "implement"


# ---------------------------------------------------------------------------
# address-code stage
# ---------------------------------------------------------------------------

def test_address_code_stage_calls_client_with_review_content(tmp_path):
    session_dir = tmp_path
    review_text = "# Detail Review\n\n## Findings\nLooks good.\n"
    (session_dir / "review-code-detail-sonnet.md").write_text(review_text)

    client = FakeClaudeClient(fixtures={"address-code": MINIMAL_EVENTS})
    run_address_code_stage(
        client=client,
        session_dir=session_dir,
        address_model="sonnet",
        is_git=False,
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.label == "address-code"
    assert call.model == "sonnet"
    assert "Detail Review" in call.prompt
