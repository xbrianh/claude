#!/usr/bin/env python3
# /gremlins — on-demand status of background gremlins.
# Reads every ${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins/<id>/state.json,
# applies the shared liveness classifier inline, and prints one scannable line per
# gremlin.
#
# Exit 0 always: an unexpected error logs to stderr and falls through. Same
# "never break a session" principle as the session-summary hook.

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BG_STALL_SECS = int(os.environ.get("BG_STALL_SECS") or 2700)

STATE_ROOT = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")),
    "claude-gremlins",
)

FMT = "%-5s  %-47s  %-22s  %-28s  %-5s  %s"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso_to_epoch(iso: str):
    """Parse ISO-8601 string to a UTC epoch float. Returns None on failure."""
    if not iso:
        return None
    try:
        # Python < 3.11 does not accept 'Z' suffix directly.
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def humanize_age(started_at: str) -> str:
    """Return a human-readable age string like 5s, 12m, 3h, 2d."""
    epoch = iso_to_epoch(started_at)
    if epoch is None:
        return "-"
    diff = int(time.time() - epoch)
    if diff < 60:
        return f"{diff}s"
    if diff < 3600:
        return f"{diff // 60}m"
    if diff < 86400:
        return f"{diff // 3600}h"
    return f"{diff // 86400}d"


def display_id(gr_id: str) -> str:
    """Compact old-format IDs to their trailing rand6 hex; pass new-format through."""
    if re.match(r"^[0-9]{8}-[0-9]{6}-[0-9]+-([a-f0-9]{6}|xxxxxx)$", gr_id):
        return gr_id.rsplit("-", 1)[-1]
    return gr_id


def render_sub_stage(sub) -> str:
    """Format sub_stage: dict → key=val,... ; string passthrough; empty → ''."""
    if sub is None or sub == "":
        return ""
    if isinstance(sub, dict):
        if not sub:
            return ""
        return ",".join(f"{k}={json.dumps(v)}" for k, v in sub.items())
    return str(sub)


def liveness_of_state_file(sf: str, state=None) -> str:
    """
    Classify a gremlin's liveness from its state.json path.
    Returns one of: running, dead:<reason>, stalled:<reason>.
    Replicates liveness.sh inline — no shell-out.
    Pass an already-loaded state dict to avoid a second JSON parse.
    """
    if not os.path.isfile(sf):
        return ""
    wdir = os.path.dirname(sf)
    if state is None:
        try:
            with open(sf, encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception:
            return ""

    gr_status = state.get("status")
    gr_pid = state.get("pid")
    gr_exit_code = state.get("exit_code")

    # Terminal: finish.sh ran → `finished` marker exists.
    if os.path.isfile(os.path.join(wdir, "finished")):
        if gr_exit_code is not None and gr_exit_code != 0 and gr_exit_code != "null":
            return f"dead:exit {gr_exit_code}"
        return "dead:finished"

    if gr_status == "running":
        # PID gone but no finish marker → crashed silently.
        if gr_pid is not None and gr_pid != "null":
            try:
                os.kill(int(gr_pid), 0)
            except (OSError, ValueError):
                return f"dead:crashed (pid {gr_pid} gone)"

        # Stall heuristic: log file hasn't moved in BG_STALL_SECS.
        log_path = os.path.join(wdir, "log")
        if os.path.isfile(log_path):
            try:
                mtime = os.path.getmtime(log_path)
                age = int(time.time() - mtime)
                if age > BG_STALL_SECS:
                    return f"stalled:no log update {age // 60}m"
            except OSError:
                pass

        return "running"

    # Non-running status without a finished marker.
    if gr_exit_code is not None and gr_exit_code != 0 and gr_exit_code != "null":
        return f"dead:exit {gr_exit_code}"
    return f"dead:{gr_status or 'unknown'}"


# ---------------------------------------------------------------------------
# State directory helpers
# ---------------------------------------------------------------------------

def iter_state_files():
    """Yield (gr_id, state_file_path, wdir) for every gremlin in STATE_ROOT."""
    if not os.path.isdir(STATE_ROOT):
        return
    try:
        entries = sorted(os.listdir(STATE_ROOT))
    except OSError:
        return
    for name in entries:
        wdir = os.path.join(STATE_ROOT, name)
        sf = os.path.join(wdir, "state.json")
        if os.path.isfile(sf):
            yield name, sf, wdir


def load_state(sf: str):
    """Load state.json, returning a dict or None on failure."""
    try:
        with open(sf, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def kind_short(kind: str) -> str:
    if kind == "localgremlin":
        return "local"
    if kind == "ghgremlin":
        return "gh"
    return kind or ""


def git_toplevel() -> str:
    """Return the git toplevel of cwd, or cwd itself if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return os.getcwd()


# ---------------------------------------------------------------------------
# Duration parser for --since
# ---------------------------------------------------------------------------

def parse_duration(s: str) -> int:
    """Parse a duration string like 30s, 5m, 2h, 1d into seconds."""
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if not m:
        raise ValueError(f"unrecognised duration: {s!r} (expected e.g. 30s, 5m, 2h, 1d)")
    value, unit = int(m.group(1)), m.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def build_row(gr_id, sf, wdir, state, live):
    """Return a dict of display fields for a gremlin row."""
    raw_kind = state.get("kind", "")
    k = kind_short(raw_kind)
    pr = state.get("project_root", "")
    stage = state.get("stage") or "-"
    sub = state.get("sub_stage")
    desc = state.get("description") or state.get("instructions") or ""
    started_at = state.get("started_at") or ""

    sub_disp = render_sub_stage(sub)
    stage_disp = stage
    if sub_disp:
        stage_disp = f"{stage} ({sub_disp})"

    stage_trim = stage_disp[:22]
    live_trim = live[:28]
    desc_trim = desc[:60]
    age = humanize_age(started_at)
    sid = display_id(gr_id)

    return {
        "started_at": started_at,
        "kind": k,
        "sid": sid,
        "stage": stage_trim,
        "live": live_trim,
        "live_full": live,
        "age": age,
        "desc": desc_trim,
        "project_root": pr,
        "gr_id": gr_id,
        "wdir": wdir,
        "closed": os.path.isfile(os.path.join(wdir, "closed")),
        "state": state,
    }


def print_table(rows):
    """Print header + rows using the fixed format string."""
    print(FMT % ("KIND", "ID", "STAGE", "LIVENESS", "AGE", "DESCRIPTION"))
    for r in rows:
        print(FMT % (r["kind"], r["sid"], r["stage"], r["live"], r["age"], r["desc"]))


# ---------------------------------------------------------------------------
# Stop / rescue helpers
# ---------------------------------------------------------------------------

GREMLIN_STAGES = {
    "localgremlin": ["plan", "implement", "review-code", "address-code"],
    "ghgremlin": ["plan", "implement", "commit-pr", "request-copilot", "ghreview", "wait-copilot", "ghaddress"],
}

GREMLIN_SCRIPTS = {
    "localgremlin": "~/.claude/skills/localgremlin/localgremlin.py",
    "ghgremlin": "~/.claude/skills/ghgremlin/ghgremlin.sh",
}


def resolve_gremlin(target: str):
    """Resolve id prefix to a single (gr_id, sf, wdir) or print error and return None."""
    matches = []
    for gr_id, sf, wdir in iter_state_files():
        if target in gr_id:
            matches.append((gr_id, sf, wdir))
    if not matches:
        print(f"no gremlin matched: {target}")
        return None
    if len(matches) > 1:
        print(f"ambiguous id '{target}' matched {len(matches)} gremlins — use a longer prefix:")
        for gr_id, _, _ in matches:
            print(f"  {gr_id}")
        return None
    return matches[0]


def do_stop(target: str) -> bool:
    match = resolve_gremlin(target)
    if match is None:
        return False

    gr_id, sf, wdir = match
    state = load_state(sf)
    if not state:
        print(f"error: could not read state for {gr_id}")
        return False

    live = liveness_of_state_file(sf, state)

    if live == "dead:finished":
        print(f"gremlin {gr_id} already finished successfully — nothing to stop")
        return False
    if live == "dead:stopped":
        print(f"gremlin {gr_id} was already stopped")
        return False
    if live.startswith("dead:"):
        print(f"gremlin {gr_id} is already dead ({live})")
        print("Use 'rescue' to diagnose and continue from the failed stage.")
        return False

    stage = state.get("stage") or "-"
    pid = state.get("pid")

    if pid is None:
        print(f"error: no PID in state for {gr_id}")
        return False
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        print(f"error: invalid PID {pid!r} in state for {gr_id}")
        return False

    # Derive process group and send SIGTERM to the whole group.
    pgid = None
    try:
        ps_result = subprocess.run(
            ["ps", "-o", "pgid=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        pgid_str = ps_result.stdout.strip()
        if pgid_str:
            pgid = int(pgid_str)
    except Exception:
        pass

    if pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"warning: could not signal process group {pgid}: {e}")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"warning: could not signal pid {pid}: {e}")

    # Poll for finish.sh to write the finished marker.
    finished_path = os.path.join(wdir, "finished")
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if os.path.isfile(finished_path):
            break
        time.sleep(0.5)

    # If still absent, write it and patch state.json manually.
    if not os.path.isfile(finished_path):
        try:
            pathlib.Path(finished_path).touch()
        except OSError:
            pass
        now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        state["status"] = "stopped"
        state["exit_code"] = 130
        state["ended_at"] = now_iso
        try:
            with open(sf, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
        except OSError as e:
            print(f"warning: could not patch state.json: {e}")

    print(f"stopped gremlin {gr_id} (stage: {stage})")
    return True


def build_rescue_prompt(state, wdir, log_tail, artifact_paths):
    kind = state.get("kind", "localgremlin")
    stage = state.get("stage") or "unknown"
    instructions = state.get("instructions") or "(not recorded)"
    description = state.get("description") or ""
    workdir = state.get("workdir") or wdir

    stages = GREMLIN_STAGES.get(kind, [])
    gremlin_script = GREMLIN_SCRIPTS.get(kind, f"~/.claude/skills/{kind}/{kind}.sh")

    if stage in stages:
        remaining = stages[stages.index(stage) + 1:]
    else:
        remaining = stages

    artifacts_section = ""
    if artifact_paths:
        artifacts_section = (
            "Existing artifacts already produced (read these for context before acting):\n"
            + "\n".join(f"  - {p}" for p in artifact_paths)
            + "\n\n"
        )

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    rescue_note_path = os.path.join(wdir, "artifacts", f"rescue-{timestamp}.md")
    log_tail_safe = log_tail.replace("```", "` ` `")

    return f"""You are rescuing a failed background gremlin.

## Original task

Kind: {kind}
Description: {description}
Failed at stage: {stage}

Instructions:
{instructions}

## Gremlin reference

Gremlin script: {gremlin_script}
Stage order for {kind}: {' → '.join(stages)}
Remaining stages to complete (after failed stage): {' → '.join(remaining) or '(none — failed stage was the last one)'}

{artifacts_section}## Failure log (last ~200 lines)

```
{log_tail_safe}
```

## What to do

1. Diagnose the failure from the log above.
2. Fix the underlying issue in this worktree (working directory: {workdir}).
3. Re-run the failed stage ({stage}) and then complete the remaining stages: {', '.join(remaining) or '(none)'}.
   For each stage, perform what the pipeline script would have done.
   Commit your changes when the implement stage is complete.
4. Write a brief rescue note to `{rescue_note_path}` describing what failed and what you fixed.

Work directly in the current directory. Do not re-invoke the gremlin script.
"""


def do_rescue(target: str) -> bool:
    match = resolve_gremlin(target)
    if match is None:
        return False

    gr_id, sf, wdir = match
    state = load_state(sf)
    if not state:
        print(f"error: could not read state for {gr_id}")
        return False

    live = liveness_of_state_file(sf, state)

    if live == "running":
        print(f"gremlin {gr_id} is still running — use 'stop' first, then rescue")
        return False
    if live == "dead:finished":
        print(f"gremlin {gr_id} finished successfully — nothing to rescue")
        return False
    if live.startswith("stalled:"):
        print(f"gremlin {gr_id} is stalled but its process is still alive — stopping it first...")
        if not do_stop(target):
            print("error: could not stop the stalled gremlin — aborting rescue")
            return False

    workdir = state.get("workdir")
    if not workdir:
        print(f"error: no workdir recorded in state for {gr_id} — cannot rescue")
        return False

    stage = state.get("stage") or "unknown"

    log_path = os.path.join(wdir, "log")
    log_tail = ""
    if os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
                log_tail = "".join(lines[-200:])
        except OSError:
            log_tail = "(could not read log)"

    artifacts_dir = os.path.join(wdir, "artifacts")
    artifact_paths = []
    if os.path.isdir(artifacts_dir):
        for fname in sorted(os.listdir(artifacts_dir)):
            fpath = os.path.join(artifacts_dir, fname)
            if os.path.isfile(fpath):
                artifact_paths.append(fpath)

    prompt = build_rescue_prompt(state, wdir, log_tail, artifact_paths)

    print(f"Rescuing gremlin {gr_id} (stage: {stage}, liveness: {live})")
    print(f"Working directory: {workdir}")
    print("Running rescue agent inline — Ctrl-C to abort.")
    print()

    try:
        result = subprocess.run(["claude", "-p", prompt], cwd=workdir)
        if result.returncode == 0:
            # Marker first, then state.json — mirrors finish.sh. The marker is
            # the authoritative signal for `dead:finished`; if the state write
            # fails after the marker exists, liveness still classifies correctly.
            try:
                pathlib.Path(os.path.join(wdir, "finished")).touch()
            except OSError as e:
                print(f"warning: could not touch finished marker: {e}")
            now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state["status"] = "done"
            state["exit_code"] = 0
            state["ended_at"] = now_iso
            try:
                with open(sf, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, indent=2)
            except OSError as e:
                print(f"warning: could not patch state.json: {e}")
            # Unlike finish.sh, we do not `git worktree remove` here — the
            # rescued worktree is preserved so the operator can inspect before
            # running `/gremlins land`, which has its own cleanup path.

            print()
            print(f"Rescue completed for {gr_id}.")
            print(f"Run /gremlins land {gr_id} if you are satisfied with the result.")
            return True
        else:
            print()
            print(f"Rescue agent exited with code {result.returncode}.")
            print(f"Inspect the log at {log_path} and worktree at {workdir} for details.")
            return False
    except FileNotFoundError:
        print("error: 'claude' CLI not found in PATH")
        return False
    except KeyboardInterrupt:
        print("\nRescue aborted by user.")
        return False


def do_close(target: str) -> bool:
    match = resolve_gremlin(target)
    if match is None:
        return False

    gr_id, sf, wdir = match
    state = load_state(sf)
    if not state:
        print(f"error: could not read state for {gr_id}")
        return False

    live = liveness_of_state_file(sf, state)
    if live == "running" or live.startswith("stalled:"):
        print(f"gremlin {gr_id} is still live ({live}) — use 'stop' first, then close")
        return False

    closed_marker = os.path.join(wdir, "closed")
    if os.path.isfile(closed_marker):
        print(f"gremlin {gr_id} already closed")
        return True

    try:
        with open(closed_marker, "a"):
            pass
    except OSError as e:
        print(f"error: could not write closed marker: {e}")
        return False

    print(f"closed {gr_id} ({live})")
    return True


def expected_branch(state: dict, gr_id: str):
    """Return the durable branch name for a gremlin, or None if there isn't one."""
    kind = state.get("kind", "")
    if kind == "localgremlin":
        return f"bg/localgremlin/{gr_id}"
    return None


def _fast_forward_main(cwd):
    """Attempt to fast-forward local main to origin/main after a gh PR merge."""
    r = subprocess.run(["git", "fetch", "origin"], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print(f"warning: git fetch origin failed: {r.stderr.strip()}")
        return
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=cwd,
    )
    current = r.stdout.strip()
    if current == "main":
        r = subprocess.run(
            ["git", "merge", "--ff-only", "origin/main"],
            capture_output=True, text=True, cwd=cwd,
        )
        if r.returncode != 0:
            print("warning: local main has diverged from origin/main — fast-forward not possible; update manually")
        else:
            print("Fast-forwarded local main.")
    else:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "main", "origin/main"],
            capture_output=True, cwd=cwd,
        )
        if r.returncode == 0:
            r = subprocess.run(
                ["git", "branch", "-f", "main", "origin/main"],
                capture_output=True, text=True, cwd=cwd,
            )
            if r.returncode == 0:
                print("Fast-forwarded local main.")
            else:
                print(f"warning: could not fast-forward main: {r.stderr.strip()}")
        else:
            print("warning: local main has diverged from origin/main — update manually")


def _cleanup_gremlin(gr_id: str, sf: str, wdir: str, state: dict, cwd, *,
                     delete_branch: bool = True, check_cwd: bool = False) -> bool:
    """Touch closed marker, remove worktree, optionally delete branch, remove state dir.

    Returns False only when check_cwd=True and we're inside the worktree; all
    other steps are best-effort (warnings printed on failure).
    """
    workdir = state.get("workdir") or ""

    if check_cwd and workdir and os.path.exists(workdir):
        cwd_real = os.path.realpath(os.getcwd())
        worktree_real = os.path.realpath(workdir)
        if cwd_real == worktree_real or cwd_real.startswith(worktree_real + os.sep):
            print("you are inside this gremlin's worktree — cd elsewhere before running this command")
            return False

    # Mark closed before cleanup so a partial failure doesn't allow a re-run.
    try:
        pathlib.Path(os.path.join(wdir, "closed")).touch()
    except OSError:
        pass

    if workdir and os.path.exists(workdir):
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", workdir],
            capture_output=True, cwd=cwd,
        )
        if r.returncode == 0:
            print(f"removed worktree {workdir}")
        else:
            try:
                shutil.rmtree(workdir)
                print(f"removed worktree {workdir}")
            except OSError as e:
                print(f"warning: could not remove worktree {workdir}: {e}")

    if delete_branch:
        branch = state.get("branch") or expected_branch(state, gr_id)
        if branch:
            r = subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True, text=True, cwd=cwd,
            )
            if r.returncode == 0:
                print(f"deleted branch {branch}")
            elif "not found" not in r.stderr:
                print(f"warning: could not delete branch {branch}: {r.stderr.strip()}")

    try:
        shutil.rmtree(wdir)
        print(f"removed state directory {wdir}")
    except OSError as e:
        print(f"warning: could not remove state directory {wdir}: {e}")

    return True


def do_rm(target: str) -> bool:
    match = resolve_gremlin(target)
    if match is None:
        return False

    gr_id, sf, wdir = match
    state = load_state(sf)
    if not state:
        print(f"error: could not read state for {gr_id}")
        return False

    live = liveness_of_state_file(sf, state)

    if not live:
        print(f"error: could not determine liveness for {gr_id}")
        return False

    if live == "running" or live.startswith("stalled:"):
        print(f"gremlin {gr_id} is still live ({live}) — use 'stop' first, then rm")
        return False

    project_root = state.get("project_root") or ""
    cwd_for_git = project_root if project_root and os.path.isdir(project_root) else None

    if not _cleanup_gremlin(gr_id, sf, wdir, state, cwd_for_git,
                             delete_branch=True, check_cwd=True):
        return False

    print(f"rm: gremlin {gr_id} cleaned up")
    return True


# ---------------------------------------------------------------------------
# Land helpers
# ---------------------------------------------------------------------------

def _compose_commit_message(plan_path: str):
    """Return (subject, body) distilled from plan.md's ## Context and ## Tasks."""
    try:
        with open(plan_path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return "Land gremlin branch", ""

    m = re.search(r'^##\s+Context\s*\n(.*?)(?=^##\s|\Z)', content, re.MULTILINE | re.DOTALL)
    if not m:
        return "Land gremlin branch", ""

    para = next(
        (p.strip() for p in re.split(r'\n\n+', m.group(1).strip()) if p.strip()),
        "",
    )
    if not para:
        return "Land gremlin branch", ""

    subject = " ".join(para.split())
    subject = re.sub(
        r'^(?:implement\s+|add\s+support\s+for\s+|this\s+change\s+|this\s+pr\s+)',
        "", subject, flags=re.IGNORECASE,
    )
    if subject:
        subject = subject[0].upper() + subject[1:]

    if len(subject) > 72:
        cut = subject[:72]
        boundary = cut.rfind(" ")
        subject = cut[:boundary] if boundary > 0 else cut

    tm = re.search(r'^##\s+Tasks\s*\n(.*?)(?=^##\s|\Z)', content, re.MULTILINE | re.DOTALL)
    body = ""
    if tm:
        done = re.findall(r'^\s*-\s+\[x\]\s+(.+)', tm.group(1), re.MULTILINE | re.IGNORECASE)
        if done:
            body = "\n".join(f"- {t.strip()}" for t in done[:8])

    return subject, body


def _land_local(gr_id: str, sf: str, wdir: str, state: dict) -> bool:
    """Squash-land a local gremlin branch onto the current branch."""
    setup_kind = state.get("setup_kind", "")
    if setup_kind != "worktree-branch":
        print(f"gremlin {gr_id} has setup_kind={setup_kind!r} — only worktree-branch gremlins support local landing")
        return False

    branch = state.get("branch", "")
    if not branch:
        print(f"error: no branch field in state for {gr_id}")
        return False

    project_root = state.get("project_root") or ""
    cwd = project_root if project_root and os.path.isdir(project_root) else None

    # Safety: refuse if cwd is inside the gremlin's worktree.
    workdir = state.get("workdir") or ""
    if workdir and os.path.exists(workdir):
        cwd_real = os.path.realpath(os.getcwd())
        worktree_real = os.path.realpath(workdir)
        if cwd_real == worktree_real or cwd_real.startswith(worktree_real + os.sep):
            print("you are inside this gremlin's worktree — cd elsewhere before landing")
            return False

    r = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True, cwd=cwd,
    )
    if r.returncode != 0:
        print(f"error: gremlin branch {branch!r} does not exist — may already have been cleaned up")
        return False

    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=cwd,
    )
    if r.returncode != 0:
        print("error: could not determine current branch")
        return False
    current = r.stdout.strip()
    if current == branch:
        print(f"error: currently on gremlin branch {branch!r} — switch to your target branch first")
        return False

    r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=cwd)
    if r.stdout.strip():
        print("error: working tree is not clean — commit or stash changes before landing")
        return False

    r = subprocess.run(["git", "merge-base", "HEAD", branch], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print(f"error: could not compute merge-base between HEAD and {branch!r}")
        return False
    merge_base = r.stdout.strip()

    r = subprocess.run(
        ["git", "rev-list", "--count", f"{merge_base}..{branch}"],
        capture_output=True, text=True, cwd=cwd,
    )
    if int(r.stdout.strip() or "0") < 1:
        print(f"error: gremlin branch {branch!r} has no commits above merge-base")
        return False

    plan_path = os.path.join(wdir, "artifacts", "plan.md")
    if not os.path.isfile(plan_path):
        print(f"error: plan.md not found at {plan_path}")
        return False

    print(f"Squash-merging {branch} onto {current}...")
    r = subprocess.run(["git", "merge", "--squash", branch], cwd=cwd)
    if r.returncode != 0:
        reset_ok = subprocess.run(
            ["git", "reset", "--hard", "HEAD"], capture_output=True, cwd=cwd,
        ).returncode == 0
        subprocess.run(["git", "clean", "-fd"], capture_output=True, cwd=cwd)
        suffix = "working tree restored" if reset_ok else "manual cleanup may be needed"
        print(f"error: git merge --squash failed — {suffix}")
        return False

    subject, body = _compose_commit_message(plan_path)
    commit_msg = f"{subject}\n\n{body}" if body else subject

    r = subprocess.run(["git", "commit", "-m", commit_msg], cwd=cwd)
    if r.returncode != 0:
        print("error: git commit failed")
        return False

    print(f"Landed {branch} onto {current}.")
    _cleanup_gremlin(gr_id, sf, wdir, state, cwd, delete_branch=True)
    return True


def _land_gh(gr_id: str, sf: str, wdir: str, state: dict, force: bool = False) -> bool:
    """Merge a gh gremlin's PR and clean up."""
    pr_url = state.get("pr_url", "")
    if not pr_url:
        print(f"error: no pr_url in state for {gr_id}")
        print("This gremlin may have been launched before pr_url tracking was added to ghgremlin.sh.")
        return False

    project_root = state.get("project_root") or ""
    cwd = project_root if project_root and os.path.isdir(project_root) else None

    print(f"Checking PR: {pr_url}")
    r = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json",
         "state,mergeable,reviewDecision,statusCheckRollup"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"error: could not fetch PR info: {r.stderr.strip()}")
        return False

    try:
        pr_info = json.loads(r.stdout)
    except json.JSONDecodeError:
        print("error: could not parse PR info response")
        return False

    pr_state = pr_info.get("state", "")
    mergeable = pr_info.get("mergeable", "")
    review_decision = pr_info.get("reviewDecision") or ""
    checks = pr_info.get("statusCheckRollup") or []

    if pr_state == "MERGED":
        print("PR already merged.")
        _fast_forward_main(cwd)
        _cleanup_gremlin(gr_id, sf, wdir, state, cwd, delete_branch=False)
        return True

    if pr_state == "CLOSED":
        if force:
            print("PR is closed (not merged) — force flag set, cleaning up without merge.")
            _cleanup_gremlin(gr_id, sf, wdir, state, cwd, delete_branch=False)
            return True
        print(f"PR is closed (not merged): {pr_url}")
        print("Use --force to skip merge and clean up only.")
        return False

    # PR is OPEN — check for blockers before merging
    if review_decision == "CHANGES_REQUESTED":
        print("error: PR has changes requested — address review comments before landing")
        print(f"  {pr_url}")
        return False

    failed = [c for c in checks if c.get("conclusion") in
              ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED")]
    if failed:
        names = ", ".join(c.get("name", "?") for c in failed[:3])
        print(f"error: PR has failed CI checks: {names}")
        print(f"  {pr_url}")
        return False

    if mergeable == "UNKNOWN":
        print("GitHub is computing mergeability — waiting 5s and retrying...")
        time.sleep(5)
        r = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "mergeable"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            try:
                mergeable = json.loads(r.stdout).get("mergeable", "UNKNOWN")
            except json.JSONDecodeError:
                pass

    if mergeable == "CONFLICTING":
        print("error: PR has merge conflicts — resolve them before landing")
        print(f"  {pr_url}")
        return False

    print(f"Merging: {pr_url}")
    r = subprocess.run(
        ["gh", "pr", "merge", pr_url, "--squash", "--delete-branch"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        if "already merged" in r.stdout.lower() or "already merged" in r.stderr.lower():
            print("PR was already merged.")
        else:
            print(f"error: gh pr merge failed: {r.stderr.strip() or r.stdout.strip()}")
            return False
    else:
        print("PR merged.")

    _fast_forward_main(cwd)
    _cleanup_gremlin(gr_id, sf, wdir, state, cwd, delete_branch=False)
    return True


def do_land(target: str, force: bool = False) -> bool:
    match = resolve_gremlin(target)
    if match is None:
        return False

    gr_id, sf, wdir = match
    state = load_state(sf)
    if not state:
        print(f"error: could not read state for {gr_id}")
        return False

    live = liveness_of_state_file(sf, state)
    if live == "running" or live.startswith("stalled:"):
        print(f"gremlin {gr_id} is still live ({live}) — use 'stop' first, then land")
        return False

    kind = state.get("kind", "")
    if kind == "localgremlin":
        if live != "dead:finished":
            print(f"gremlin {gr_id} is not finished (liveness: {live})")
            return False
        return _land_local(gr_id, sf, wdir, state)
    elif kind == "ghgremlin":
        return _land_gh(gr_id, sf, wdir, state, force=force)
    else:
        print(f"error: unknown gremlin kind {kind!r} — cannot land")
        return False


# ---------------------------------------------------------------------------
# List / drill-in view
# ---------------------------------------------------------------------------

def collect_rows(here_root=None, kind_filter=None, since_secs=None,
                 liveness_filter=None, include_closed=False):
    """
    Collect and return a list of row dicts, sorted by started_at ascending.

    here_root         — if set, restrict to gremlins with this project_root.
    kind_filter       — if set ('local' or 'gh'), restrict to that kind.
    since_secs        — if set, restrict to gremlins started within this many seconds.
    liveness_filter   — if set, a set of prefixes ('running', 'dead', 'stalled').
    include_closed    — if True, include closed gremlins (for drill-in / --recent).
    """
    now = time.time()
    rows = []
    for gr_id, sf, wdir in iter_state_files():
        if not include_closed and os.path.isfile(os.path.join(wdir, "closed")):
            continue

        state = load_state(sf)
        if not state:
            continue
        gr_id_from_state = state.get("id") or gr_id
        if not gr_id_from_state:
            continue

        live = liveness_of_state_file(sf, state)

        # --here filter
        if here_root is not None:
            if state.get("project_root", "") != here_root:
                continue

        # --kind filter
        if kind_filter is not None:
            if kind_short(state.get("kind", "")) != kind_filter:
                continue

        # --since filter
        if since_secs is not None:
            started_at = state.get("started_at") or ""
            epoch = iso_to_epoch(started_at)
            if epoch is None or (now - epoch) > since_secs:
                continue

        # liveness filter
        if liveness_filter:
            matched_live = any(live.startswith(prefix) for prefix in liveness_filter)
            if not matched_live:
                continue

        row = build_row(gr_id_from_state, sf, wdir, state, live)
        rows.append(row)

    rows.sort(key=lambda r: r["started_at"])
    return rows


def do_list(args, here_root=None):
    """Default list view."""
    liveness_filter = None
    if args.running or args.dead or args.stalled:
        liveness_filter = set()
        if args.running:
            liveness_filter.add("running")
        if args.dead:
            liveness_filter.add("dead:")
        if args.stalled:
            liveness_filter.add("stalled:")

    since_secs = None
    if args.since:
        try:
            since_secs = parse_duration(args.since)
        except ValueError as e:
            print(f"error: {e}")
            return

    rows = collect_rows(
        here_root=here_root,
        kind_filter=args.kind,
        since_secs=since_secs,
        liveness_filter=liveness_filter,
        include_closed=False,
    )

    # Running gremlins float to the top; within each group, older gremlins
    # appear first by started_at.
    rows.sort(key=lambda r: (r["live_full"] != "running", r["started_at"]))

    if not rows:
        if here_root is not None:
            print(f"No active gremlins for project: {here_root}")
        else:
            print("No active gremlins on this machine.")
        return

    print_table(rows)


def do_recent(args, here_root=None):
    """--recent [N]: show dead gremlins started within N hours."""
    n_hours = args.recent
    since_secs = n_hours * 3600

    rows = collect_rows(
        here_root=here_root,
        kind_filter=args.kind,
        since_secs=since_secs,
        liveness_filter={"dead:"},
        include_closed=True,
    )

    for row in rows:
        if row["closed"]:
            row["desc"] = row["desc"][:51] + " [closed]"

    if not rows:
        if here_root is not None:
            print(f"No recent gremlins for project: {here_root}")
        else:
            print("No recent gremlins on this machine.")
        return

    print_table(rows)


def do_drill_in(target: str):
    """Print every field of a uniquely-matched gremlin in a labeled block."""
    matches = []
    for gr_id, sf, wdir in iter_state_files():
        if target in gr_id:
            matches.append((gr_id, sf, wdir))

    if not matches:
        print(f"no gremlin matched: {target}")
        return
    if len(matches) > 1:
        print(f"ambiguous id '{target}' matched {len(matches)} gremlins — use a longer prefix:")
        for gr_id, _, _ in matches:
            print(f"  {gr_id}")
        return

    gr_id, sf, wdir = matches[0]
    state = load_state(sf)
    if not state:
        print(f"error: could not read state for {gr_id}")
        return

    live = liveness_of_state_file(sf)
    started_at = state.get("started_at") or ""
    age = humanize_age(started_at)

    # Convert started_at to local time for display.
    local_start = ""
    epoch = iso_to_epoch(started_at)
    if epoch is not None:
        local_start = datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S %Z")

    print(f"gremlin: {gr_id}")
    print(f"  liveness : {live}")
    print(f"  closed   : {'yes' if os.path.isfile(os.path.join(wdir, 'closed')) else 'no'}")
    print(f"  age      : {age}")
    if local_start:
        print(f"  started  : {local_start}")
    print("  state.json fields:")
    for key, val in state.items():
        print(f"    {key}: {json.dumps(val)}")

    print()
    print(f"  state directory: {wdir}")
    log_path = os.path.join(wdir, "log")
    artifacts_dir = os.path.join(wdir, "artifacts")
    artifact_paths = []
    if os.path.isdir(artifacts_dir):
        for fname in sorted(os.listdir(artifacts_dir)):
            fpath = os.path.join(artifacts_dir, fname)
            if os.path.isfile(fpath):
                artifact_paths.append(fpath)
    has_log = os.path.isfile(log_path)
    if has_log:
        print(f"    log: {log_path}")
    if artifact_paths:
        print("    artifacts:")
        for fpath in artifact_paths:
            print(f"      {fpath}")
    if not has_log and not artifact_paths:
        print("    (no log or artifacts)")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="gremlins.py",
        description="On-demand status of background gremlins.",
        epilog=(
            "Subcommands (positional, before flags):\n"
            "  stop <id>     Send SIGTERM to a running gremlin and wait for it to exit.\n"
            "  rescue <id>   Diagnose and resume a dead or stalled gremlin inline.\n"
            "  rm <id>       Delete a dead/finished gremlin's state directory, worktree, and branch.\n"
            "  close <id>    Mark a dead/finished gremlin as closed (hides it from the default view).\n"
            "  land <id>     Land a finished gremlin: squash-merge locally (local) or merge the PR (gh).\n"
            "                Pass --force to skip merge and clean up a closed gh PR.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    parser.add_argument(
        "--here", action="store_true",
        help="Only gremlins whose project_root matches this repo.",
    )
    parser.add_argument(
        "--running", action="store_true",
        help="Show only running gremlins.",
    )
    parser.add_argument(
        "--dead", action="store_true",
        help="Show only dead gremlins.",
    )
    parser.add_argument(
        "--stalled", action="store_true",
        help="Show only stalled gremlins.",
    )
    parser.add_argument(
        "--kind", choices=["local", "gh"], metavar="local|gh",
        help="Filter to a specific gremlin kind.",
    )
    parser.add_argument(
        "--since", metavar="DURATION",
        help="Show only gremlins started within DURATION (e.g. 30s, 5m, 2h, 1d).",
    )
    parser.add_argument(
        "--recent", nargs="?", const=24, type=int, metavar="N",
        help="Show recently-finished gremlins started within N hours (default 24). "
             "Mutually exclusive with --running/--dead/--stalled.",
    )
    parser.add_argument(
        "--watch", nargs="?", const=2, type=int, metavar="SEC",
        help="Refresh the view every SEC seconds (default 2). "
             "Mutually exclusive with positional id argument.",
    )
    parser.add_argument(
        "id_prefix", nargs="?", metavar="id-prefix",
        help="Substring to drill into a single gremlin. Mutually exclusive with --watch.",
    )
    parser.add_argument(
        "--all", action="store_true", help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def render_view(args, here_root):
    """Render whichever view the flags request. Used by both normal and --watch path."""
    if args.recent is not None and (args.running or args.stalled):
        print("error: --recent cannot be combined with --running/--stalled", file=sys.stderr)
        return

    if args.recent is not None:
        do_recent(args, here_root=here_root)
    else:
        do_list(args, here_root=here_root)


def _dispatch_subcommand():
    """
    Pre-process sys.argv for stop/rescue subcommands before argparse runs.
    Returns (handled: bool, ok: bool). handled=False means not a subcommand.
    """
    raw = sys.argv[1:]
    non_flags = [a for a in raw if not a.startswith("-")]
    if not non_flags or non_flags[0] not in ("stop", "rescue", "rm", "close", "land"):
        return False, False

    subcommand = non_flags[0]
    # Find the index of the subcommand in raw argv and take the next non-flag.
    sc_idx = next(i for i, a in enumerate(raw) if a == subcommand)
    trailing = [a for a in raw[sc_idx + 1:] if not a.startswith("-")]
    if not trailing:
        print(f"usage: gremlins {subcommand} <id-prefix>")
        sys.exit(1)

    target = trailing[0]
    if not os.path.isdir(STATE_ROOT):
        print("No gremlins have been launched on this machine.")
        sys.exit(0)

    if subcommand == "stop":
        ok = do_stop(target)
    elif subcommand == "rm":
        ok = do_rm(target)
    elif subcommand == "close":
        ok = do_close(target)
    elif subcommand == "land":
        force = "--force" in raw
        ok = do_land(target, force=force)
    else:
        ok = do_rescue(target)
    return True, ok


def main():
    handled, ok = _dispatch_subcommand()
    if handled:
        sys.exit(0 if ok else 1)

    args = parse_args()

    # --watch and positional drill-in are mutually exclusive.
    if args.watch is not None and args.id_prefix is not None:
        print("error: --watch cannot be combined with a positional id argument")
        sys.exit(0)

    # Early exit if state root doesn't exist.
    if not os.path.isdir(STATE_ROOT):
        print("No gremlins have been launched on this machine.")
        sys.exit(0)

    # Resolve --here once.
    here_root = None
    if args.here:
        here_root = git_toplevel()

    # Drill-in positional argument (no --watch).
    if args.id_prefix is not None:
        do_drill_in(args.id_prefix)
        sys.exit(0)

    # --watch loop.
    if args.watch is not None:
        interval = max(1, args.watch)
        stop = [False]

        def _handle_sigint(signum, frame):
            stop[0] = True

        signal.signal(signal.SIGINT, _handle_sigint)

        while not stop[0]:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            render_view(args, here_root)
            for _ in range(interval * 10):
                if stop[0]:
                    break
                time.sleep(0.1)
        sys.exit(0)

    # Default: single render.
    render_view(args, here_root)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"gremlins: unexpected error: {exc}", file=sys.stderr)
        sys.exit(0)
