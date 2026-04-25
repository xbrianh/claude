#!/usr/bin/env python3
"""bossgremlin.py — manager for chained gremlin workflows.

Launched by launch.sh as kind=bossgremlin. Orchestrates a serial chain of
child gremlins (all local or all gh), with the handoff agent deciding what
to do between steps. Never reads plan/diff/review content — its decision
surface is: handoff .state.json exit states, the finished marker, and the
exit codes of land and rescue.

Receives pipeline args from launch.sh:
  bossgremlin.py --plan <spec-path> --chain-kind local|gh [--model <model>]
  [--resume-from <stage>]    ← added by launch.sh --resume; ignored (we use
                                boss_state.json for resumption)

GR_ID env var is set by launch.sh.
"""

import argparse
import datetime
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

STATE_ROOT = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")),
    "claude-gremlins",
)

POLL_INTERVAL = 5  # seconds between finished-marker polls
HANDOFF_TIMEOUT = int(os.environ.get("BOSSGREMLIN_HANDOFF_TIMEOUT", "3600"))
HANDOFF_FETCH_TIMEOUT = int(os.environ.get("BOSSGREMLIN_HANDOFF_FETCH_TIMEOUT", "60"))

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


def init_boss_state(spec_path: str, chain_kind: str, chain_base_ref: str,
                    target_branch: str, state_dir: str) -> dict:
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
        # remote (including merges done via the GitHub UI or another machine,
        # which never trigger _fast_forward_main locally). Bound the fetch so
        # an unreachable origin can't stall the chain indefinitely between
        # handoffs.
        try:
            fetch = subprocess.run(
                ["git", "fetch", "origin", target_branch],
                capture_output=True, text=True, cwd=project_root,
                timeout=HANDOFF_FETCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            die(f"git fetch origin {target_branch} timed out after {HANDOFF_FETCH_TIMEOUT}s")
        if fetch.returncode != 0:
            die(f"git fetch origin {target_branch} failed: {fetch.stderr.strip()}")
        handoff_cwd = project_root
        rev_label = f"origin/{target_branch}"
        rev_args = ["--rev", rev_label]
    else:
        die(f"unknown chain_kind: {chain_kind!r}")

    log(f"handoff {n}: plan={current_plan}, base={base_ref[:12]}, rev={rev_label}, cwd={handoff_cwd}")
    cmd = [
        handoff_script,
        "--plan", current_plan,
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
    })
    boss_state["handoff_count"] = n
    if os.path.isfile(out_path):
        boss_state["current_plan"] = out_path

    log(f"handoff {n} result: {exit_state}")
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


def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--plan", required=True)
    p.add_argument("--chain-kind", required=True, choices=["local", "gh"])
    p.add_argument("--model", default="sonnet")
    p.add_argument("--resume-from", default=None)
    args, _ = p.parse_known_args(argv)
    return args


def main(argv):
    args = parse_args(argv)

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

    spec_path = str(pathlib.Path(args.plan).resolve())
    chain_kind = args.chain_kind
    launch_kind = {"local": "localgremlin", "gh": "ghgremlin"}[chain_kind]

    boss_state_file = os.path.join(state_dir, "boss_state.json")
    if not os.path.isfile(boss_state_file):
        log(f"chain start: kind={chain_kind}, spec={spec_path}")
        chain_base_ref = get_head_ref(project_root)
        target_branch = get_current_branch(project_root)
        log(f"base ref: {chain_base_ref[:12]}, target branch: {target_branch or '(detached)'}")
        boss_state = init_boss_state(
            spec_path=spec_path,
            chain_kind=chain_kind,
            chain_base_ref=chain_base_ref,
            target_branch=target_branch,
            state_dir=state_dir,
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
                set_stage(gr_id, "done")
                save_boss_state(state_dir, boss_state)
                sys.exit(0)

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
                    # Re-running the pipeline via rescue cannot clear this
                    # blocker, so halt the chain for operator action.
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
                log(f"rescue refused for {current_child_id} ({bail_reason or 'no bail_reason'})")
                boss_state["children"].append({
                    "id": current_child_id,
                    "outcome": f"bailed:{bail_reason}" if bail_reason else "bailed",
                })
                boss_state["current_child_id"] = None
                save_boss_state(state_dir, boss_state)
                die(f"chain halted: child {current_child_id} failed and rescue was refused")

            was_rescued = True
            log(f"rescue relaunched {current_child_id} — waiting again")


if __name__ == "__main__":
    main(sys.argv[1:])
