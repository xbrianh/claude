"""Orchestrator entry point for the boss pipeline.

Port of ``skills/bossgremlin/bossgremlin.py``.  The boss_state.json schema is
preserved byte-for-byte so in-flight chains work across the migration.

Receives pipeline args forwarded by launch.sh:
  boss_main --plan <spec-path> --chain-kind local|gh [--model <model>]
  [--resume-from <stage>]    ← added by launch.sh --resume; ignored (we use
                                boss_state.json for resumption)

GR_ID env var is set by launch.sh.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import List, Tuple

from ..gh_utils import get_repo, parse_issue_ref, view_issue
from ..state import patch_state

STATE_ROOT = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")),
    "claude-gremlins",
)

POLL_INTERVAL = 5  # seconds between finished-marker polls
HANDOFF_TIMEOUT = int(os.environ.get("BOSSGREMLIN_HANDOFF_TIMEOUT", "3600"))
# Bounds every interaction with `origin` (chain-start fetch of the default
# branch and per-handoff fetch of the target branch).
HANDOFF_FETCH_TIMEOUT = int(os.environ.get("BOSSGREMLIN_HANDOFF_FETCH_TIMEOUT", "60"))
GH_VIEW_TIMEOUT = 30  # seconds; bounds `gh repo view` at chain start

_current_proc = None
_stop_requested = False


def _sigterm_handler(signum, frame):
    global _stop_requested
    _stop_requested = True
    log("received SIGTERM — stopping after current operation")
    if _current_proc is not None:
        try:
            _current_proc.send_signal(signal.SIGTERM)
        except Exception:
            pass


signal.signal(signal.SIGTERM, _sigterm_handler)


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def die(msg: str) -> None:
    log(f"fatal: {msg}")
    sys.exit(1)


def check_stop() -> None:
    if _stop_requested:
        log("stop requested — exiting")
        sys.exit(130)


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def set_stage(gr_id: str, stage: str) -> None:
    script = os.path.expanduser("~/.claude/skills/_bg/set-stage.sh")
    if os.access(script, os.X_OK):
        subprocess.run([script, gr_id, stage], capture_output=True)


def run_proc(cmd: list, **kwargs) -> int:
    """Run subprocess, returning exit code. Forwards SIGTERM on stop."""
    global _current_proc
    proc = subprocess.Popen(cmd, **kwargs)
    _current_proc = proc
    try:
        proc.wait()
    except Exception:
        proc.kill()
        proc.wait()
    finally:
        _current_proc = None
    return proc.returncode


def get_head_ref(project_root: str) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=project_root,
    )
    if r.returncode != 0:
        die(f"git rev-parse HEAD failed in {project_root}: {r.stderr.strip()}")
    return r.stdout.strip()


def get_current_branch(project_root: str) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=project_root,
    )
    if r.returncode != 0:
        return ""
    branch = r.stdout.strip()
    return "" if branch == "HEAD" else branch


def get_default_branch(project_root: str) -> str:
    """Resolve the repo's default branch via gh CLI. Calls die() on failure."""
    try:
        r = subprocess.run(
            ["gh", "repo", "view", "--json", "defaultBranchRef",
             "-q", ".defaultBranchRef.name"],
            capture_output=True, text=True, cwd=project_root,
            timeout=GH_VIEW_TIMEOUT,
        )
    except FileNotFoundError:
        die("gh CLI not found on PATH — required to resolve default branch for gh chain")
    except subprocess.TimeoutExpired:
        die(f"gh repo view timed out after {GH_VIEW_TIMEOUT}s in {project_root}")
    if r.returncode != 0:
        die(f"gh repo view failed in {project_root}: {r.stderr.strip()}")
    name = r.stdout.strip()
    if not name:
        die(f"gh repo view returned empty default branch in {project_root}")
    return name


def fetch_origin_branch(project_root: str, branch: str, *, context: str) -> None:
    """Fetch origin/<branch> with a bounded timeout. Calls die() on failure.

    Uses an explicit refspec so a branch name starting with `-` cannot be
    parsed by `git fetch` as an option.
    """
    refspec = f"refs/heads/{branch}:refs/remotes/origin/{branch}"
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", refspec],
            capture_output=True, text=True, cwd=project_root,
            timeout=HANDOFF_FETCH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        die(f"git fetch origin {refspec} timed out after {HANDOFF_FETCH_TIMEOUT}s {context}")
    if fetch.returncode != 0:
        die(f"git fetch origin {refspec} failed {context}: {fetch.stderr.strip()}")


def get_remote_branch_sha(project_root: str, branch: str) -> str:
    """Fetch origin/<branch> and return its SHA. Calls die() on failure."""
    fetch_origin_branch(project_root, branch, context="at chain start")
    r = subprocess.run(
        ["git", "rev-parse", f"refs/remotes/origin/{branch}"],
        capture_output=True, text=True, cwd=project_root,
    )
    if r.returncode != 0:
        die(f"git rev-parse refs/remotes/origin/{branch} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def init_boss_state(spec_path: str, chain_kind: str, chain_base_ref: str,
                    target_branch: str, state_dir: str,
                    issue_url: str = "", issue_num: str = "") -> dict:
    boss_state = {
        "spec_path": spec_path,
        "chain_kind": chain_kind,
        "chain_base_ref": chain_base_ref,
        "target_branch": target_branch,
        "current_plan": spec_path,
        "handoff_count": 0,
        "current_child_id": None,
        "children": [],
        "handoff_records": [],
        # Source of the spec: empty for local-file inputs, populated when
        # --plan was a GitHub issue reference. Persisted so `/gremlins`
        # status can show the issue link and so resume never re-fetches.
        "issue_url": issue_url,
        "issue_num": issue_num,
        # Latest operator_followups list reported by handoff. Each handoff
        # rewrites this with the conservative carry-forward set the handoff
        # agent produced, so by chain-done it holds the final list of
        # operator tasks the human still owes between phase landings.
        "operator_followups": [],
    }
    save_json(os.path.join(state_dir, "boss_state.json"), boss_state)
    return boss_state


def load_boss_state(state_dir: str) -> dict:
    return load_json(os.path.join(state_dir, "boss_state.json"))


def save_boss_state(state_dir: str, boss_state: dict) -> None:
    save_json(os.path.join(state_dir, "boss_state.json"), boss_state)


def run_handoff(gr_id: str, state_dir: str, boss_state: dict,
                project_root: str, boss_workdir: str, model: str) -> tuple:
    """Run handoff agent. Returns (exit_state, signal dict).

    Updates boss_state in place (handoff_count, current_plan, handoff_records).
    Calls die() on infrastructure failure.

    Children of a local chain squash-land into the boss's own workdir HEAD,
    while children of a gh chain push to origin/<target_branch>. Pick the
    right cwd/rev so handoff sees the actually-landed work, not a stale ref
    in the user's repo that may never advance during the chain.
    """
    set_stage(gr_id, "handoff")
    handoff_script = os.path.expanduser("~/.claude/skills/handoff/handoff.py")
    if not os.access(handoff_script, os.X_OK):
        die(f"handoff.py not executable at {handoff_script}")

    n = boss_state["handoff_count"] + 1
    out_path = os.path.join(state_dir, f"handoff-{n:03d}.md")
    signal_path = os.path.join(state_dir, f"handoff-{n:03d}.state.json")
    current_plan = boss_state["current_plan"]
    spec_path = boss_state["spec_path"]
    base_ref = boss_state["chain_base_ref"]
    chain_kind = boss_state.get("chain_kind")
    target_branch = boss_state.get("target_branch", "")

    rev_args: list = []
    if chain_kind == "local":
        if not boss_workdir or not os.path.isdir(boss_workdir):
            die(f"boss workdir not usable for local chain handoff: {boss_workdir!r}")
        handoff_cwd = boss_workdir
        rev_label = "HEAD"
    elif chain_kind == "gh":
        if not target_branch:
            die("gh chain has no target branch — cannot resolve remote ref for handoff")
        # Refresh the remote-tracking ref so we see PRs that landed on the
        # remote. Bound the fetch so an unreachable origin can't stall the
        # chain indefinitely between handoffs.
        fetch_origin_branch(project_root, target_branch, context="before handoff")
        handoff_cwd = project_root
        rev_label = f"origin/{target_branch}"
        rev_args = ["--rev", rev_label]
    else:
        die(f"unknown chain_kind: {chain_kind!r}")

    # Only forward --spec once the rolling plan has diverged from the spec.
    # On handoff #1, init_boss_state seeds current_plan = spec_path, so
    # passing --spec would render the same document twice in the prompt.
    forward_spec = bool(spec_path) and spec_path != current_plan
    spec_log = spec_path if forward_spec else "(none)"
    log(f"handoff {n}: plan={current_plan}, spec={spec_log}, base={base_ref[:12]}, rev={rev_label}, cwd={handoff_cwd}")
    spec_args = ["--spec", spec_path] if forward_spec else []
    cmd = [
        handoff_script,
        "--plan", current_plan,
        *spec_args,
        "--out", out_path,
        "--base", base_ref,
        "--model", model,
        "--timeout", str(HANDOFF_TIMEOUT),
        *rev_args,
    ]
    rc = run_proc(cmd, cwd=handoff_cwd)
    check_stop()

    if rc != 0:
        die(f"handoff agent exited {rc}")

    if not os.path.isfile(signal_path):
        die(f"handoff signal file not written: {signal_path}")

    try:
        sig = load_json(signal_path)
    except Exception as exc:
        die(f"could not parse handoff signal file {signal_path}: {exc}")

    exit_state = sig.get("exit_state")
    if exit_state not in ("next-plan", "chain-done", "bail"):
        die(f"handoff signal file has unrecognized exit_state: {exit_state!r}")

    # Coerce operator_followups to a list of strings. Old handoff signals
    # predating the field land here as None or absent; treat that as no
    # followups so a chain that started under an older handoff still reads.
    raw_followups = sig.get("operator_followups")
    if isinstance(raw_followups, list):
        followups = [str(item) for item in raw_followups if str(item).strip()]
    else:
        followups = []

    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    boss_state["handoff_records"].append({
        "timestamp": now,
        "n": n,
        "plan_in": current_plan,
        "plan_out": out_path,
        "signal_file": signal_path,
        "exit_state": exit_state,
        "child_plan": sig.get("child_plan"),
        "bail_reason": sig.get("reason"),
        "operator_followups": followups,
    })
    boss_state["handoff_count"] = n
    boss_state["operator_followups"] = followups
    if os.path.isfile(out_path):
        boss_state["current_plan"] = out_path

    log(f"handoff {n} result: {exit_state}")
    if followups:
        log(f"  operator follow-ups carried by handoff {n}: {len(followups)}")
        for item in followups:
            log(f"    - {item}")
    return exit_state, sig


def launch_child(gr_id: str, launch_kind: str, child_plan: str) -> str:
    """Launch a child gremlin. Returns child gremlin ID."""
    global _current_proc
    launcher = os.path.expanduser("~/.claude/skills/_bg/launch.sh")
    if not os.access(launcher, os.X_OK):
        die(f"launch.sh not executable at {launcher}")

    cmd = [launcher, "--parent", gr_id, "--print-id", launch_kind, "--plan", child_plan]
    log(f"launching child ({launch_kind}): {child_plan}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    _current_proc = proc
    try:
        stdout, _ = proc.communicate()
    finally:
        _current_proc = None
    check_stop()

    if proc.returncode != 0:
        die(f"launch.sh exited {proc.returncode}")

    child_id = stdout.strip()
    if not child_id:
        die("launch.sh --print-id produced no output")

    log(f"child launched: {child_id}")
    return child_id


def wait_for_child(child_id: str, gr_id: str) -> bool:
    """Poll until child has a finished marker. Returns True on clean exit (exit_code 0).

    On stop request, sends stop to the child before exiting.
    """
    child_wdir = os.path.join(STATE_ROOT, child_id)
    finished_path = os.path.join(child_wdir, "finished")
    state_path = os.path.join(child_wdir, "state.json")

    log(f"waiting for child {child_id}...")
    while True:
        if _stop_requested:
            log(f"stop requested — stopping child {child_id}")
            gremlins = os.path.expanduser("~/.claude/skills/gremlins/gremlins.py")
            if os.access(gremlins, os.X_OK):
                subprocess.run([gremlins, "stop", child_id], capture_output=True)
            sys.exit(130)

        if os.path.isfile(finished_path):
            break

        if os.path.isfile(state_path):
            try:
                state = load_json(state_path)
                if state.get("status") == "running":
                    pid = state.get("pid")
                    if pid is not None:
                        try:
                            os.kill(int(pid), 0)
                        except (OSError, ValueError):
                            log(f"child {child_id} crashed (pid {pid} gone)")
                            break
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)

    if os.path.isfile(state_path):
        try:
            state = load_json(state_path)
            return state.get("exit_code") == 0
        except Exception:
            pass
    return False


def child_is_closed(child_id: str) -> bool:
    return os.path.isfile(os.path.join(STATE_ROOT, child_id, "closed"))


def get_child_bail_reason(child_id: str) -> str:
    state_path = os.path.join(STATE_ROOT, child_id, "state.json")
    if not os.path.isfile(state_path):
        return ""
    try:
        state = load_json(state_path)
        return state.get("bail_reason") or state.get("bail_class") or ""
    except Exception:
        return ""


def get_child_bail_detail(child_id: str) -> str:
    state_path = os.path.join(STATE_ROOT, child_id, "state.json")
    if not os.path.isfile(state_path):
        return ""
    try:
        state = load_json(state_path)
        return state.get("bail_detail") or ""
    except Exception:
        return ""


def _summarize_for_log(text: str, limit: int = 240) -> str:
    """Collapse to one line + cap length for boss-log readability.

    bail_detail is whatever the headless rescue agent chose to put there.
    Keep the boss log resilient against multi-line or runaway text without
    losing the underlying field in state.json (which we don't truncate).
    """
    if not text:
        return ""
    one_line = " ".join(text.split()).strip()
    if len(one_line) > limit:
        return one_line[: limit - 3] + "..."
    return one_line


def land_child(child_id: str) -> bool:
    gremlins = os.path.expanduser("~/.claude/skills/gremlins/gremlins.py")
    if not os.access(gremlins, os.X_OK):
        die(f"gremlins.py not executable at {gremlins}")
    log(f"landing child {child_id}")
    return run_proc([gremlins, "land", child_id]) == 0


def rescue_child(child_id: str) -> bool:
    gremlins = os.path.expanduser("~/.claude/skills/gremlins/gremlins.py")
    if not os.access(gremlins, os.X_OK):
        die(f"gremlins.py not executable at {gremlins}")
    log(f"rescuing child {child_id} (headless)")
    return run_proc([gremlins, "rescue", "--headless", child_id]) == 0


def _parse_boss_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--plan", required=True)
    p.add_argument("--chain-kind", required=True, choices=["local", "gh"])
    p.add_argument("--model", default="sonnet")
    p.add_argument("--resume-from", default=None)
    args, _ = p.parse_known_args(argv)
    return args


def _resolve_plan_source(plan: str, state_dir: str) -> Tuple[str, str, str]:
    """Resolve --plan into a snapshot under ``<state_dir>/spec.md``.

    Accepts the same forms as ghgremlin's --plan: a local file path, ``42`` /
    ``#42``, ``owner/name#42``, or a full ``https://github.com/.../issues/N``
    URL. The returned ``spec_path`` is always the snapshot — boss handoffs
    only ever read the snapshot, never the original input.

    Idempotent: if ``spec.md`` already exists with non-zero size, it is
    treated as authoritative and no re-fetch is performed. This handles the
    rescue edge case where a previous run wrote the snapshot but crashed
    before persisting boss_state.json.

    Returns ``(spec_path, issue_url, issue_num)``. ``issue_url`` /
    ``issue_num`` are empty strings for local-file inputs.
    """
    spec_dest = os.path.join(state_dir, "spec.md")

    if os.path.isfile(spec_dest) and os.path.getsize(spec_dest) > 0:
        log(f"reusing existing spec snapshot: {spec_dest}")
        return spec_dest, "", ""

    if os.path.isfile(plan):
        if os.path.getsize(plan) == 0:
            die(f"--plan: file is empty: {plan}")
        shutil.copyfile(plan, spec_dest)
        log(f"plan source (file): {plan} → {spec_dest}")
        return spec_dest, "", ""

    if shutil.which("gh") is None:
        die(f"--plan: gh CLI not found; required to resolve issue reference {plan!r}")

    try:
        repo = get_repo()
    except RuntimeError as exc:
        die(f"--plan: {exc}")

    target_repo, issue_ref = parse_issue_ref(plan, repo)
    if target_repo is None:
        die(f"--plan: not a readable file or recognized issue reference: {plan}")

    try:
        issue_data = view_issue(issue_ref, target_repo)
    except RuntimeError as exc:
        die(f"--plan: {exc}")

    body = issue_data.get("body") or ""
    if not body:
        die(f"--plan: issue {plan} has an empty body")

    issue_url = issue_data.get("url") or ""
    issue_num = str(issue_data.get("number") or "")

    with open(spec_dest, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    log(f"plan source (issue {target_repo}#{issue_ref}): {issue_url} → {spec_dest}")
    return spec_dest, issue_url, issue_num


def _maybe_set_description_from_spec(state_dir: str) -> None:
    """If state.json's description wasn't set explicitly, fill it from the
    spec snapshot's first heading. Mirrors gh's _update_description_from_plan.

    A no-op when state.json is missing or already has description_explicit=true,
    or when the snapshot has no recognizable heading.
    """
    state_file = os.path.join(state_dir, "state.json")
    spec_file = os.path.join(state_dir, "spec.md")
    if not os.path.isfile(state_file) or not os.path.isfile(spec_file):
        return
    try:
        data = load_json(state_file)
    except Exception:
        return
    if data.get("description_explicit"):
        return
    try:
        with open(spec_file, encoding="utf-8") as f:
            head_lines = [next(f) for _ in range(50)]
    except StopIteration:
        head_lines = []
    except Exception:
        return
    h1 = ""
    for line in head_lines:
        m = re.match(r"^#+\s+(.+)", line)
        if m:
            h1 = m.group(1).strip()[:60]
            break
    if h1:
        patch_state(description=h1)


def boss_main(argv: List[str]) -> int:
    args = _parse_boss_args(argv)

    gr_id = os.environ.get("GR_ID")
    if not gr_id:
        die("GR_ID env var not set (should be set by launch.sh)")

    state_dir = os.path.join(STATE_ROOT, gr_id)
    if not os.path.isdir(state_dir):
        die(f"state dir not found: {state_dir}")

    try:
        gremlin_state = load_json(os.path.join(state_dir, "state.json"))
    except Exception as exc:
        die(f"could not read state.json: {exc}")
    project_root = gremlin_state.get("project_root", "")
    if not project_root or not os.path.isdir(project_root):
        die(f"project_root not usable: {project_root!r}")
    boss_workdir = gremlin_state.get("workdir", "")

    chain_kind = args.chain_kind
    launch_kind = {"local": "localgremlin", "gh": "ghgremlin"}[chain_kind]

    boss_state_file = os.path.join(state_dir, "boss_state.json")
    if not os.path.isfile(boss_state_file):
        # Chain start: snapshot --plan into the state dir. The handoff agent
        # only ever reads the snapshot, so a deleted-or-modified original
        # input cannot perturb later handoffs. For issue refs, the fetch
        # happens here (after launch.sh has detached) so a transient GitHub
        # outage is reported in the boss log instead of failing the launch.
        spec_path, issue_url, issue_num = _resolve_plan_source(args.plan, state_dir)
        if issue_url:
            log(f"chain start: kind={chain_kind}, spec={spec_path}, issue={issue_url}")
        else:
            log(f"chain start: kind={chain_kind}, spec={spec_path}")
        _maybe_set_description_from_spec(state_dir)
        if chain_kind == "gh":
            # gh children open PRs from the repo's default branch and land
            # there, regardless of where the user happens to be. Anchor the
            # chain to origin/<default-branch> so handoff diffs land cleanly.
            target_branch = get_default_branch(project_root)
            chain_base_ref = get_remote_branch_sha(project_root, target_branch)
            log(f"base ref: {chain_base_ref[:12]} (origin/{target_branch}), target branch: {target_branch}")
        else:
            chain_base_ref = get_head_ref(project_root)
            target_branch = get_current_branch(project_root)
            log(f"base ref: {chain_base_ref[:12]}, target branch: {target_branch or '(detached)'}")
        boss_state = init_boss_state(
            spec_path=spec_path,
            chain_kind=chain_kind,
            chain_base_ref=chain_base_ref,
            target_branch=target_branch,
            state_dir=state_dir,
            issue_url=issue_url,
            issue_num=issue_num,
        )
    else:
        boss_state = load_boss_state(state_dir)
        log(f"resuming chain: kind={chain_kind}, completed children: {len(boss_state['children'])}")

    # Main loop: handoff → launch → wait → land/rescue → repeat
    while True:
        check_stop()
        current_child_id = boss_state.get("current_child_id")

        if current_child_id is None:
            # Step 1: run handoff to decide what to do next
            exit_state, sig = run_handoff(
                gr_id=gr_id,
                state_dir=state_dir,
                boss_state=boss_state,
                project_root=project_root,
                boss_workdir=boss_workdir,
                model=args.model,
            )
            save_boss_state(state_dir, boss_state)
            check_stop()

            if exit_state == "chain-done":
                log("chain complete")
                followups = boss_state.get("operator_followups") or []
                if followups:
                    log(
                        f"operator follow-ups ({len(followups)}) — owed by the "
                        f"human between phase landings:"
                    )
                    for item in followups:
                        log(f"  - {item}")
                else:
                    log("operator follow-ups: (none)")
                set_stage(gr_id, "done")
                save_boss_state(state_dir, boss_state)
                return 0

            if exit_state == "bail":
                reason = sig.get("reason") or "(no reason given)"
                log(f"handoff bailed: {reason}")
                save_boss_state(state_dir, boss_state)
                die(f"chain halted by handoff: {reason}")

            # next-plan: launch the next child
            child_plan = sig.get("child_plan")
            if not child_plan or not os.path.isfile(child_plan):
                die(f"handoff returned next-plan but child_plan not found: {child_plan!r}")

            check_stop()
            current_child_id = launch_child(gr_id, launch_kind, child_plan)
            boss_state["current_child_id"] = current_child_id
            save_boss_state(state_dir, boss_state)
            # Stop the freshly launched child if a stop was requested during
            # or just after launch (pre-wait window).
            if _stop_requested:
                log(f"stop requested — stopping newly launched child {current_child_id}")
                gremlins = os.path.expanduser("~/.claude/skills/gremlins/gremlins.py")
                if os.access(gremlins, os.X_OK):
                    subprocess.run([gremlins, "stop", current_child_id], capture_output=True)
                sys.exit(130)
            check_stop()

        else:
            # Resume path: already have a child in flight
            log(f"resuming with in-flight child: {current_child_id}")
            if child_is_closed(current_child_id):
                # `closed` is a UI hide flag, not a success/failure signal.
                # Inspect the finished marker and exit_code to determine outcome.
                child_wdir = os.path.join(STATE_ROOT, current_child_id)
                finished_path = os.path.join(child_wdir, "finished")
                state_path = os.path.join(child_wdir, "state.json")
                if os.path.isfile(finished_path) and os.path.isfile(state_path):
                    try:
                        child_state = load_json(state_path)
                        child_succeeded = child_state.get("exit_code") == 0
                    except Exception:
                        child_succeeded = False
                else:
                    child_succeeded = False

                if child_succeeded:
                    # Already finished successfully and closed — treat as landed
                    log(f"child {current_child_id} already finished and closed — treating as landed")
                    boss_state["children"].append({
                        "id": current_child_id,
                        "outcome": "landed",
                    })
                    boss_state["current_child_id"] = None
                    save_boss_state(state_dir, boss_state)
                    continue
                else:
                    # Closed but not successfully finished — operator may have
                    # manually hidden a failed child.  Halt for operator action.
                    log(
                        f"child {current_child_id} is closed but did not finish successfully"
                        f" — operator action required"
                    )
                    boss_state["current_child_id"] = None
                    save_boss_state(state_dir, boss_state)
                    die(
                        f"chain halted: child {current_child_id} was manually closed without"
                        f" successfully finishing — inspect and resume or reassign"
                    )

        # Step 2: inner loop — wait → land → (rescue → wait → land)* → bail
        was_rescued = False
        while True:
            check_stop()
            child_wdir = os.path.join(STATE_ROOT, current_child_id)

            if not os.path.isfile(os.path.join(child_wdir, "finished")):
                set_stage(gr_id, "waiting")
                success = wait_for_child(current_child_id, gr_id)
            else:
                try:
                    child_state = load_json(os.path.join(child_wdir, "state.json"))
                    success = child_state.get("exit_code") == 0
                except Exception:
                    success = False

            check_stop()

            if success:
                set_stage(gr_id, "landing")
                if land_child(current_child_id):
                    outcome = "rescued-then-landed" if was_rescued else "landed"
                    log(f"child {current_child_id} {outcome}")
                    boss_state["children"].append({"id": current_child_id, "outcome": outcome})
                    boss_state["current_child_id"] = None
                    save_boss_state(state_dir, boss_state)
                    break  # inner loop done; outer loop continues to next handoff
                else:
                    # The pipeline succeeded but land itself failed (e.g. merge
                    # conflict, branch protection rejection, squash conflict).
                    log(f"landing failed for {current_child_id} — operator action required")
                    boss_state["children"].append({
                        "id": current_child_id,
                        "outcome": "land-failed",
                    })
                    boss_state["current_child_id"] = None
                    save_boss_state(state_dir, boss_state)
                    die(
                        f"chain halted: child {current_child_id} pipeline succeeded but"
                        f" land failed (merge conflict or branch protection?) —"
                        f" resolve manually, then resume the boss"
                    )

            # Pipeline failure → rescue
            set_stage(gr_id, "rescuing")
            if not rescue_child(current_child_id):
                bail_reason = get_child_bail_reason(current_child_id)
                bail_detail = _summarize_for_log(
                    get_child_bail_detail(current_child_id)
                )
                # `structural` is distinct from `unsalvageable`: the agent
                # recognized a real bug in the pipeline source or a sibling
                # artifact that the chain can be salvaged from with a human
                # edit, but the agent isn't the right actor.
                if bail_reason == "structural":
                    log(
                        f"child {current_child_id} bailed: STRUCTURAL — "
                        f"pipeline/sibling-artifact bug, human edit required"
                    )
                    if bail_detail:
                        log(f"  diagnosis: {bail_detail}")
                elif bail_reason == "unsalvageable":
                    log(
                        f"child {current_child_id} bailed: UNSALVAGEABLE — run "
                        f"cannot be recovered (giving up)"
                    )
                    if bail_detail:
                        log(f"  detail: {bail_detail}")
                else:
                    # Other headless-rescue bail reasons also populate bail_detail.
                    log(f"rescue refused for {current_child_id} ({bail_reason or 'no bail_reason'})")
                    if bail_detail:
                        log(f"  detail: {bail_detail}")
                boss_state["children"].append({
                    "id": current_child_id,
                    "outcome": f"bailed:{bail_reason}" if bail_reason else "bailed",
                })
                boss_state["current_child_id"] = None
                save_boss_state(state_dir, boss_state)
                die(f"chain halted: child {current_child_id} failed and rescue was refused")

            was_rescued = True
            log(f"rescue relaunched {current_child_id} — waiting again")
