"""Tests for pipeline.orchestrators.boss."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

import pipeline.orchestrators.boss as boss_mod
from pipeline.orchestrators.boss import (
    _summarize_for_log,
    boss_main,
    get_child_bail_detail,
    get_child_bail_reason,
    init_boss_state,
    load_boss_state,
    save_boss_state,
)

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gremlin_state(tmp_path, gr_id="test-boss-aabb12"):
    """Write minimal state.json and directory for boss_main."""
    state_dir = tmp_path / gr_id
    state_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "id": gr_id,
        "kind": "bossgremlin",
        "project_root": str(project_root),
        "workdir": str(workdir),
        "status": "running",
    }))
    return state_dir, project_root, workdir


def _common_boss_patches(monkeypatch, tmp_path, gr_id):
    """Shared monkeypatches for boss_main integration tests."""
    monkeypatch.setenv("GR_ID", gr_id)
    monkeypatch.setattr(boss_mod, "STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(boss_mod, "_stop_requested", False)
    monkeypatch.setattr(boss_mod, "set_stage", lambda *a: None)
    monkeypatch.setattr(boss_mod, "get_head_ref", lambda p: "abc123def456abc1")
    monkeypatch.setattr(boss_mod, "get_current_branch", lambda p: "main")


# ---------------------------------------------------------------------------
# _summarize_for_log
# ---------------------------------------------------------------------------

def test_summarize_empty():
    assert _summarize_for_log("") == ""


def test_summarize_single_line():
    assert _summarize_for_log("hello world") == "hello world"


def test_summarize_collapses_newlines():
    assert _summarize_for_log("line one\nline two\nline three") == "line one line two line three"


def test_summarize_truncates():
    long_text = "x" * 300
    result = _summarize_for_log(long_text, limit=240)
    assert len(result) == 240
    assert result.endswith("...")


def test_summarize_exact_limit():
    text = "y" * 240
    assert _summarize_for_log(text, limit=240) == text


# ---------------------------------------------------------------------------
# get_child_bail_reason / get_child_bail_detail
# ---------------------------------------------------------------------------

def test_get_child_bail_reason_missing_state(tmp_path):
    orig = boss_mod.STATE_ROOT
    boss_mod.STATE_ROOT = str(tmp_path)
    try:
        assert get_child_bail_reason("no-such-child") == ""
    finally:
        boss_mod.STATE_ROOT = orig


def test_get_child_bail_reason_reads_bail_reason(tmp_path):
    child_dir = tmp_path / "child-aaa"
    child_dir.mkdir()
    (child_dir / "state.json").write_text(json.dumps({
        "bail_reason": "structural",
        "bail_class": "something_else",
    }))
    orig = boss_mod.STATE_ROOT
    boss_mod.STATE_ROOT = str(tmp_path)
    try:
        assert get_child_bail_reason("child-aaa") == "structural"
    finally:
        boss_mod.STATE_ROOT = orig


def test_get_child_bail_reason_falls_back_to_bail_class(tmp_path):
    child_dir = tmp_path / "child-bbb"
    child_dir.mkdir()
    (child_dir / "state.json").write_text(json.dumps({"bail_class": "unsalvageable"}))
    orig = boss_mod.STATE_ROOT
    boss_mod.STATE_ROOT = str(tmp_path)
    try:
        assert get_child_bail_reason("child-bbb") == "unsalvageable"
    finally:
        boss_mod.STATE_ROOT = orig


def test_get_child_bail_detail_missing(tmp_path):
    orig = boss_mod.STATE_ROOT
    boss_mod.STATE_ROOT = str(tmp_path)
    try:
        assert get_child_bail_detail("no-such-child") == ""
    finally:
        boss_mod.STATE_ROOT = orig


def test_get_child_bail_detail_reads_field(tmp_path):
    child_dir = tmp_path / "child-ccc"
    child_dir.mkdir()
    (child_dir / "state.json").write_text(json.dumps({"bail_detail": "phase A failed: no plan found"}))
    orig = boss_mod.STATE_ROOT
    boss_mod.STATE_ROOT = str(tmp_path)
    try:
        assert get_child_bail_detail("child-ccc") == "phase A failed: no plan found"
    finally:
        boss_mod.STATE_ROOT = orig


# ---------------------------------------------------------------------------
# init_boss_state / save / load round-trip
# ---------------------------------------------------------------------------

def test_init_boss_state_schema(tmp_path):
    state = init_boss_state(
        spec_path="/tmp/spec.md",
        chain_kind="local",
        chain_base_ref="abc123def456",
        target_branch="main",
        state_dir=str(tmp_path),
    )
    assert state["spec_path"] == "/tmp/spec.md"
    assert state["chain_kind"] == "local"
    assert state["chain_base_ref"] == "abc123def456"
    assert state["target_branch"] == "main"
    assert state["current_plan"] == "/tmp/spec.md"
    assert state["handoff_count"] == 0
    assert state["current_child_id"] is None
    assert state["children"] == []
    assert state["handoff_records"] == []
    assert state["operator_followups"] == []

    on_disk = json.loads((tmp_path / "boss_state.json").read_text())
    assert on_disk == state


def test_save_load_round_trip(tmp_path):
    state = {
        "spec_path": "/tmp/spec.md",
        "chain_kind": "gh",
        "chain_base_ref": "deadbeef12345678",
        "target_branch": "main",
        "current_plan": "/tmp/spec.md",
        "handoff_count": 2,
        "current_child_id": "child-xyz-abc123",
        "children": [{"id": "child-abc", "outcome": "landed"}],
        "handoff_records": [],
        "operator_followups": ["Do task X"],
    }
    save_boss_state(str(tmp_path), state)
    loaded = load_boss_state(str(tmp_path))
    assert loaded == state


# ---------------------------------------------------------------------------
# Resume fixture: load sample boss_state.json
# ---------------------------------------------------------------------------

def test_resume_fixture_parses():
    """boss_state_sample.json loads without error and has the expected shape."""
    fixture = FIXTURES_DIR / "boss_state_sample.json"
    state = json.loads(fixture.read_text())

    assert state["chain_kind"] == "gh"
    assert state["handoff_count"] == 5
    assert len(state["children"]) == 4
    assert state["current_child_id"] is not None
    assert all("id" in c and "outcome" in c for c in state["children"])

    required_record_keys = {
        "timestamp", "n", "plan_in", "plan_out", "signal_file",
        "exit_state", "child_plan", "bail_reason", "operator_followups",
    }
    assert all(required_record_keys <= set(r.keys()) for r in state["handoff_records"])


def test_resume_fixture_child_outcomes():
    """Completed children have expected outcomes (landed or rescued-then-landed)."""
    state = json.loads((FIXTURES_DIR / "boss_state_sample.json").read_text())
    outcomes = {c["outcome"] for c in state["children"]}
    assert outcomes <= {"landed", "rescued-then-landed"}


def test_resume_fixture_handoff_exit_states():
    """All handoff records in the fixture have recognized exit_states."""
    state = json.loads((FIXTURES_DIR / "boss_state_sample.json").read_text())
    valid = {"next-plan", "chain-done", "bail"}
    for rec in state["handoff_records"]:
        assert rec["exit_state"] in valid


# ---------------------------------------------------------------------------
# Child sequencing: handoff → launch → wait → land → handoff → chain-done
# ---------------------------------------------------------------------------

def test_chain_done_after_one_child(tmp_path, monkeypatch):
    """Boss completes: handoff1→next-plan, child runs and lands, handoff2→chain-done."""
    gr_id = "test-boss-aabb12"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")
    child_plan = tmp_path / "child-plan.md"
    child_plan.write_text("# Child plan\n")

    calls = []
    handoff_results = iter([
        ("next-plan", {"exit_state": "next-plan", "child_plan": str(child_plan), "operator_followups": []}),
        ("chain-done", {"exit_state": "chain-done", "operator_followups": []}),
    ])

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        exit_state, sig = next(handoff_results)
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text(f"# Handoff {n}\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = sig.get("operator_followups", [])
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z",
            "n": n,
            "plan_in": boss_state["spec_path"],
            "plan_out": out_path,
            "signal_file": out_path.replace(".md", ".state.json"),
            "exit_state": exit_state,
            "child_plan": sig.get("child_plan"),
            "bail_reason": None,
            "operator_followups": sig.get("operator_followups", []),
        })
        calls.append(("handoff", exit_state))
        return exit_state, sig

    def fake_launch_child(gr_id, launch_kind, child_plan_path):
        calls.append(("launch", launch_kind))
        child_id = "child-abc-123456"
        child_dir = tmp_path / child_id
        child_dir.mkdir(exist_ok=True)
        (child_dir / "state.json").write_text(json.dumps({"exit_code": 0}))
        (child_dir / "finished").write_text("")
        return child_id

    def fake_land_child(child_id):
        calls.append(("land", child_id))
        return True

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)
    monkeypatch.setattr(boss_mod, "launch_child", fake_launch_child)
    monkeypatch.setattr(boss_mod, "land_child", fake_land_child)

    result = boss_main(["--plan", str(spec), "--chain-kind", "local"])
    assert result == 0
    assert calls == [
        ("handoff", "next-plan"),
        ("launch", "localgremlin"),
        ("land", "child-abc-123456"),
        ("handoff", "chain-done"),
    ]

    final_state = load_boss_state(str(state_dir))
    assert len(final_state["children"]) == 1
    assert final_state["children"][0] == {"id": "child-abc-123456", "outcome": "landed"}
    assert final_state["current_child_id"] is None


def test_chain_uses_ghgremlin_for_gh_kind(tmp_path, monkeypatch):
    """Boss passes 'ghgremlin' as the launch_kind when chain-kind=gh."""
    gr_id = "test-boss-gh-cc3344"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)
    monkeypatch.setattr(boss_mod, "get_default_branch", lambda p: "main")
    monkeypatch.setattr(boss_mod, "get_remote_branch_sha", lambda p, b: "deadbeef12345678")

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")
    child_plan = tmp_path / "child-plan.md"
    child_plan.write_text("# Child plan\n")

    launch_kinds = []
    handoff_results = iter([
        ("next-plan", {"exit_state": "next-plan", "child_plan": str(child_plan), "operator_followups": []}),
        ("chain-done", {"exit_state": "chain-done", "operator_followups": []}),
    ])

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        exit_state, sig = next(handoff_results)
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text(f"# Handoff {n}\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = sig.get("operator_followups", [])
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z", "n": n,
            "plan_in": boss_state["spec_path"], "plan_out": out_path,
            "signal_file": "", "exit_state": exit_state,
            "child_plan": sig.get("child_plan"), "bail_reason": None,
            "operator_followups": sig.get("operator_followups", []),
        })
        return exit_state, sig

    def fake_launch_child(gr_id, launch_kind, child_plan_path):
        launch_kinds.append(launch_kind)
        child_id = "child-gh-cc3344"
        child_dir = tmp_path / child_id
        child_dir.mkdir(exist_ok=True)
        (child_dir / "state.json").write_text(json.dumps({"exit_code": 0}))
        (child_dir / "finished").write_text("")
        return child_id

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)
    monkeypatch.setattr(boss_mod, "launch_child", fake_launch_child)
    monkeypatch.setattr(boss_mod, "land_child", lambda cid: True)

    result = boss_main(["--plan", str(spec), "--chain-kind", "gh"])
    assert result == 0
    assert launch_kinds == ["ghgremlin"]


def test_chain_bail_on_handoff(tmp_path, monkeypatch):
    """Boss calls die() when handoff returns bail."""
    gr_id = "test-boss-bail-dd5566"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text("# Handoff\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = []
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z", "n": n,
            "plan_in": boss_state["spec_path"], "plan_out": out_path,
            "signal_file": "", "exit_state": "bail",
            "child_plan": None, "bail_reason": "spec is done", "operator_followups": [],
        })
        return "bail", {"exit_state": "bail", "reason": "spec is done"}

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)

    with pytest.raises(SystemExit) as exc_info:
        boss_main(["--plan", str(spec), "--chain-kind", "local"])
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Handoff signal parsing: operator_followups separation from child_plan
# ---------------------------------------------------------------------------

def test_operator_followups_stored_in_boss_state(tmp_path, monkeypatch):
    """operator_followups from handoff signal are persisted in boss_state, not forwarded to child.

    The contract under test: boss stores operator_followups in boss_state["operator_followups"]
    and passes the child_plan path (not the operator items) to launch_child.
    """
    gr_id = "test-boss-opfollowup-ee7788"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")
    child_plan = tmp_path / "child-plan.md"
    child_plan.write_text("# Child plan\nDo the implementation.\n")

    operator_items = ["After landing: run sync.sh push", "After landing: verify e2e"]
    launch_args = []  # (launch_kind, child_plan_path) captured per call

    handoff_results = iter([
        (
            "next-plan",
            {
                "exit_state": "next-plan",
                "child_plan": str(child_plan),
                "operator_followups": operator_items,
            },
        ),
        ("chain-done", {"exit_state": "chain-done", "operator_followups": operator_items}),
    ])

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        exit_state, sig = next(handoff_results)
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text(f"# Handoff {n}\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = sig.get("operator_followups", [])
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z", "n": n,
            "plan_in": boss_state["spec_path"], "plan_out": out_path,
            "signal_file": "", "exit_state": exit_state,
            "child_plan": sig.get("child_plan"), "bail_reason": None,
            "operator_followups": sig.get("operator_followups", []),
        })
        return exit_state, sig

    def fake_launch_child(gr_id, launch_kind, child_plan_path):
        launch_args.append((launch_kind, child_plan_path))
        child_id = "child-op-test-ff9900"
        child_dir = tmp_path / child_id
        child_dir.mkdir(exist_ok=True)
        (child_dir / "state.json").write_text(json.dumps({"exit_code": 0}))
        (child_dir / "finished").write_text("")
        return child_id

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)
    monkeypatch.setattr(boss_mod, "launch_child", fake_launch_child)
    monkeypatch.setattr(boss_mod, "land_child", lambda cid: True)

    result = boss_main(["--plan", str(spec), "--chain-kind", "local"])
    assert result == 0

    # Boss launched exactly one child using the child_plan path from the handoff signal.
    assert len(launch_args) == 1
    _, launched_plan_path = launch_args[0]
    assert launched_plan_path == str(child_plan)

    # operator_followups are stored in boss_state, not forwarded as a separate argument.
    final_state = load_boss_state(str(state_dir))
    assert final_state["operator_followups"] == operator_items


# ---------------------------------------------------------------------------
# Resume path: boss_state.json with current_child_id set
# ---------------------------------------------------------------------------

def test_resume_picks_up_in_flight_child(tmp_path, monkeypatch):
    """When boss_state.json has current_child_id, boss resumes from the wait loop."""
    gr_id = "test-resume-boss-aabb12"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")

    child_id = "in-flight-child-cc3344"
    child_dir = tmp_path / child_id
    child_dir.mkdir()
    (child_dir / "state.json").write_text(json.dumps({"exit_code": 0}))
    (child_dir / "finished").write_text("")

    # Pre-populate boss_state.json with current_child_id already set.
    boss_state = {
        "spec_path": str(spec),
        "chain_kind": "local",
        "chain_base_ref": "abc123def456abc1",
        "target_branch": "main",
        "current_plan": str(spec),
        "handoff_count": 1,
        "current_child_id": child_id,
        "children": [],
        "handoff_records": [],
        "operator_followups": [],
    }
    (state_dir / "boss_state.json").write_text(json.dumps(boss_state))

    calls = []

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text(f"# Handoff {n}\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = []
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z", "n": n,
            "plan_in": str(spec), "plan_out": out_path,
            "signal_file": "", "exit_state": "chain-done",
            "child_plan": None, "bail_reason": None, "operator_followups": [],
        })
        calls.append("handoff")
        return "chain-done", {"exit_state": "chain-done", "operator_followups": []}

    def fake_land_child(cid):
        calls.append(("land", cid))
        return True

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)
    monkeypatch.setattr(boss_mod, "land_child", fake_land_child)

    result = boss_main(["--plan", str(spec), "--chain-kind", "local"])
    assert result == 0

    # Boss should have landed the in-flight child first, then run handoff.
    assert calls == [("land", child_id), "handoff"]

    final_state = load_boss_state(str(state_dir))
    assert final_state["children"][0] == {"id": child_id, "outcome": "landed"}


def test_resume_fixture_in_boss_main(tmp_path, monkeypatch):
    """Load boss_state_sample.json fixture, simulate resume, verify correct child index."""
    gr_id = "test-resume-fixture-boss-112233"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)

    # Load the fixture and adapt paths to tmp_path.
    fixture_state = json.loads((FIXTURES_DIR / "boss_state_sample.json").read_text())
    child_id = fixture_state["current_child_id"]

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")
    fixture_state["spec_path"] = str(spec)
    fixture_state["current_plan"] = str(spec)

    # Create child dir with finished marker.
    child_dir = tmp_path / child_id
    child_dir.mkdir()
    (child_dir / "state.json").write_text(json.dumps({"exit_code": 0}))
    (child_dir / "finished").write_text("")

    (state_dir / "boss_state.json").write_text(json.dumps(fixture_state))

    calls = []

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text(f"# Handoff {n}\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = []
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z", "n": n,
            "plan_in": str(spec), "plan_out": out_path,
            "signal_file": "", "exit_state": "chain-done",
            "child_plan": None, "bail_reason": None, "operator_followups": [],
        })
        calls.append("handoff")
        return "chain-done", {"exit_state": "chain-done", "operator_followups": []}

    def fake_land_child(cid):
        calls.append(("land", cid))
        return True

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)
    monkeypatch.setattr(boss_mod, "land_child", fake_land_child)

    result = boss_main(["--plan", str(spec), "--chain-kind", "gh"])
    assert result == 0

    # Resumed with the in-flight child from the fixture.
    assert ("land", child_id) in calls
    final_state = load_boss_state(str(state_dir))
    # The 4 completed children from the fixture + the resumed one = 5 total.
    assert len(final_state["children"]) == 5


# ---------------------------------------------------------------------------
# Rescue-then-land
# ---------------------------------------------------------------------------

def test_rescue_then_land(tmp_path, monkeypatch):
    """Child fails rescue once then succeeds; outcome recorded as rescued-then-landed."""
    gr_id = "test-boss-rescue-gg9900"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")
    child_plan = tmp_path / "child-plan.md"
    child_plan.write_text("# Child plan\n")

    # Child starts failed, then succeeds after rescue.
    child_id = "rescue-child-hh1122"
    child_dir = tmp_path / child_id
    child_dir.mkdir()
    # Initially failed (exit_code != 0, finished marker present).
    child_state = {"exit_code": 1}
    (child_dir / "state.json").write_text(json.dumps(child_state))
    (child_dir / "finished").write_text("")

    calls = []
    rescue_call_count = [0]

    handoff_results = iter([
        ("next-plan", {"exit_state": "next-plan", "child_plan": str(child_plan), "operator_followups": []}),
        ("chain-done", {"exit_state": "chain-done", "operator_followups": []}),
    ])

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        exit_state, sig = next(handoff_results)
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text(f"# Handoff {n}\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = sig.get("operator_followups", [])
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z", "n": n,
            "plan_in": boss_state["spec_path"], "plan_out": out_path,
            "signal_file": "", "exit_state": exit_state,
            "child_plan": sig.get("child_plan"), "bail_reason": None,
            "operator_followups": sig.get("operator_followups", []),
        })
        calls.append(("handoff", exit_state))
        return exit_state, sig

    def fake_launch_child(gr_id, launch_kind, child_plan_path):
        calls.append(("launch", launch_kind))
        return child_id

    def fake_rescue_child(cid):
        calls.append(("rescue", cid))
        rescue_call_count[0] += 1
        # After rescue, flip the child to success.
        (child_dir / "state.json").write_text(json.dumps({"exit_code": 0}))
        return True

    def fake_land_child(cid):
        calls.append(("land", cid))
        return True

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)
    monkeypatch.setattr(boss_mod, "launch_child", fake_launch_child)
    monkeypatch.setattr(boss_mod, "rescue_child", fake_rescue_child)
    monkeypatch.setattr(boss_mod, "land_child", fake_land_child)

    result = boss_main(["--plan", str(spec), "--chain-kind", "local"])
    assert result == 0

    assert calls == [
        ("handoff", "next-plan"),
        ("launch", "localgremlin"),
        ("rescue", child_id),
        ("land", child_id),
        ("handoff", "chain-done"),
    ]

    final_state = load_boss_state(str(state_dir))
    assert final_state["children"][0] == {"id": child_id, "outcome": "rescued-then-landed"}


def test_bail_after_rescue_refused(tmp_path, monkeypatch):
    """Boss halts (die) when rescue is refused for a failed child."""
    gr_id = "test-boss-bail-rescue-ii3344"
    state_dir, project_root, workdir = _make_gremlin_state(tmp_path, gr_id)
    _common_boss_patches(monkeypatch, tmp_path, gr_id)

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")
    child_plan = tmp_path / "child-plan.md"
    child_plan.write_text("# Child plan\n")

    child_id = "bail-child-jj5566"
    child_dir = tmp_path / child_id
    child_dir.mkdir()
    (child_dir / "state.json").write_text(json.dumps({
        "exit_code": 1,
        "bail_reason": "unsalvageable",
    }))
    (child_dir / "finished").write_text("")

    def fake_run_handoff(gr_id, state_dir, boss_state, project_root, boss_workdir, model):
        n = boss_state["handoff_count"] + 1
        out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
        pathlib.Path(out_path).write_text(f"# Handoff {n}\n")
        boss_state["handoff_count"] = n
        boss_state["current_plan"] = out_path
        boss_state["operator_followups"] = []
        boss_state["handoff_records"].append({
            "timestamp": "2026-01-01T00:00:00Z", "n": n,
            "plan_in": boss_state["spec_path"], "plan_out": out_path,
            "signal_file": "", "exit_state": "next-plan",
            "child_plan": str(child_plan), "bail_reason": None, "operator_followups": [],
        })
        return "next-plan", {"exit_state": "next-plan", "child_plan": str(child_plan), "operator_followups": []}

    monkeypatch.setattr(boss_mod, "run_handoff", fake_run_handoff)
    monkeypatch.setattr(boss_mod, "launch_child", lambda *a: child_id)
    monkeypatch.setattr(boss_mod, "rescue_child", lambda cid: False)

    with pytest.raises(SystemExit) as exc_info:
        boss_main(["--plan", str(spec), "--chain-kind", "local"])
    assert exc_info.value.code == 1

    final_state = load_boss_state(str(state_dir))
    child_entry = final_state["children"][0]
    assert child_entry["id"] == child_id
    assert "bailed" in child_entry["outcome"]
    assert final_state["current_child_id"] is None
