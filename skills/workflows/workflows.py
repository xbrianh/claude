#!/usr/bin/env python3
# /workflows — on-demand status of background workflow pipelines.
# Reads every ${XDG_STATE_HOME:-$HOME/.local/state}/claude-workflows/<id>/state.json,
# applies the shared liveness classifier inline, and prints one scannable line per
# workflow.
#
# Exit 0 always: an unexpected error logs to stderr and falls through. Same
# "never break a session" principle as the session-summary hook.

import argparse
import datetime
import json
import os
import re
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
    "claude-workflows",
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


def display_id(wf_id: str) -> str:
    """Compact old-format IDs to their trailing rand6 hex; pass new-format through."""
    if re.match(r"^[0-9]{8}-[0-9]{6}-[0-9]+-([a-f0-9]{6}|xxxxxx)$", wf_id):
        return wf_id.rsplit("-", 1)[-1]
    return wf_id


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
    Classify a workflow's liveness from its state.json path.
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

    wf_status = state.get("status")
    wf_pid = state.get("pid")
    wf_exit_code = state.get("exit_code")

    # Terminal: finish.sh ran → `finished` marker exists.
    if os.path.isfile(os.path.join(wdir, "finished")):
        if wf_exit_code is not None and wf_exit_code != 0 and wf_exit_code != "null":
            return f"dead:exit {wf_exit_code}"
        return "dead:finished"

    if wf_status == "running":
        # PID gone but no finish marker → crashed silently.
        if wf_pid is not None and wf_pid != "null":
            try:
                os.kill(int(wf_pid), 0)
            except (OSError, ValueError):
                return f"dead:crashed (pid {wf_pid} gone)"

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
    if wf_exit_code is not None and wf_exit_code != 0 and wf_exit_code != "null":
        return f"dead:exit {wf_exit_code}"
    return f"dead:{wf_status or 'unknown'}"


# ---------------------------------------------------------------------------
# State directory helpers
# ---------------------------------------------------------------------------

def iter_state_files():
    """Yield (wf_id, state_file_path, wdir) for every workflow in STATE_ROOT."""
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
    if kind == "localimplement":
        return "local"
    if kind == "ghimplement":
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

def build_row(wf_id, sf, wdir, state, live):
    """Return a dict of display fields for a workflow row."""
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
    sid = display_id(wf_id)

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
        "wf_id": wf_id,
        "wdir": wdir,
        "state": state,
    }


def print_table(rows):
    """Print header + rows using the fixed format string."""
    print(FMT % ("KIND", "ID", "STAGE", "LIVENESS", "AGE", "DESCRIPTION"))
    for r in rows:
        print(FMT % (r["kind"], r["sid"], r["stage"], r["live"], r["age"], r["desc"]))


# ---------------------------------------------------------------------------
# Ack helpers
# ---------------------------------------------------------------------------

def do_ack(target: str):
    """Acknowledge a single workflow by substring match."""
    matches = []
    for wf_id, sf, wdir in iter_state_files():
        if target in wf_id:
            matches.append((wf_id, sf, wdir))

    if not matches:
        print(f"no workflow matched: {target}")
        return
    if len(matches) > 1:
        print(f"ambiguous id '{target}' matched {len(matches)} workflows — use a longer prefix:")
        for wf_id, _, _ in matches:
            print(f"  {wf_id}")
        return

    wf_id, sf, wdir = matches[0]
    live = liveness_of_state_file(sf)
    if live.startswith("dead:"):
        try:
            with open(os.path.join(wdir, "acknowledged"), "a"):
                pass
        except OSError:
            pass
        print(f"acknowledged {wf_id} ({live})")
    else:
        print(
            f"skipping {wf_id} ({live} is still running; "
            "only dead/finished workflows can be acknowledged)"
        )


def do_ack_all():
    """Acknowledge every dead workflow."""
    matched = 0
    for wf_id, sf, wdir in iter_state_files():
        live = liveness_of_state_file(sf)
        if live.startswith("dead:"):
            try:
                with open(os.path.join(wdir, "acknowledged"), "a"):
                    pass
            except OSError:
                pass
            print(f"acknowledged {wf_id} ({live})")
            matched += 1
    if matched == 0:
        print("nothing to acknowledge.")


# ---------------------------------------------------------------------------
# List / drill-in view
# ---------------------------------------------------------------------------

def collect_rows(here_root=None, kind_filter=None, since_secs=None,
                 liveness_filter=None, include_acknowledged=False):
    """
    Collect and return a list of row dicts, sorted by started_at ascending.

    here_root         — if set, restrict to workflows with this project_root.
    kind_filter       — if set ('local' or 'gh'), restrict to that kind.
    since_secs        — if set, restrict to workflows started within this many seconds.
    liveness_filter   — if set, a set of prefixes ('running', 'dead', 'stalled').
    include_acknowledged — if True, include acknowledged workflows (for drill-in / --recent).
    """
    now = time.time()
    rows = []
    for wf_id, sf, wdir in iter_state_files():
        if not include_acknowledged and os.path.isfile(os.path.join(wdir, "acknowledged")):
            continue

        state = load_state(sf)
        if not state:
            continue
        wf_id_from_state = state.get("id") or wf_id
        if not wf_id_from_state:
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

        row = build_row(wf_id_from_state, sf, wdir, state, live)
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
        include_acknowledged=False,
    )

    if not rows:
        if here_root is not None:
            print(f"No active workflows for project: {here_root}")
        else:
            print("No active workflows on this machine.")
        return

    print_table(rows)


def do_recent(args, here_root=None):
    """--recent [N]: show dead workflows started within N hours."""
    n_hours = args.recent
    since_secs = n_hours * 3600

    rows = collect_rows(
        here_root=here_root,
        kind_filter=args.kind,
        since_secs=since_secs,
        liveness_filter={"dead:"},
        include_acknowledged=True,
    )

    if not rows:
        if here_root is not None:
            print(f"No recent workflows for project: {here_root}")
        else:
            print("No recent workflows on this machine.")
        return

    print_table(rows)


def do_drill_in(target: str):
    """Print every field of a uniquely-matched workflow in a labeled block."""
    matches = []
    for wf_id, sf, wdir in iter_state_files():
        if target in wf_id:
            matches.append((wf_id, sf, wdir))

    if not matches:
        print(f"no workflow matched: {target}")
        return
    if len(matches) > 1:
        print(f"ambiguous id '{target}' matched {len(matches)} workflows — use a longer prefix:")
        for wf_id, _, _ in matches:
            print(f"  {wf_id}")
        return

    wf_id, sf, wdir = matches[0]
    state = load_state(sf)
    if not state:
        print(f"error: could not read state for {wf_id}")
        return

    live = liveness_of_state_file(sf)
    started_at = state.get("started_at") or ""
    age = humanize_age(started_at)

    # Convert started_at to local time for display.
    local_start = ""
    epoch = iso_to_epoch(started_at)
    if epoch is not None:
        local_start = datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S %Z")

    print(f"workflow: {wf_id}")
    print(f"  liveness : {live}")
    print(f"  age      : {age}")
    if local_start:
        print(f"  started  : {local_start}")
    print("  state.json fields:")
    for key, val in state.items():
        print(f"    {key}: {json.dumps(val)}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="workflows.py",
        description="On-demand status of background workflow pipelines.",
        add_help=True,
    )
    parser.add_argument(
        "--here", action="store_true",
        help="Only workflows whose project_root matches this repo.",
    )
    parser.add_argument(
        "--ack", metavar="TARGET",
        help="Acknowledge (hide) a dead/finished workflow. Accepts full id or substring.",
    )
    parser.add_argument(
        "--ack-all", action="store_true", dest="ack_all",
        help="Acknowledge every dead/finished workflow.",
    )
    parser.add_argument(
        "--running", action="store_true",
        help="Show only running workflows.",
    )
    parser.add_argument(
        "--dead", action="store_true",
        help="Show only dead workflows.",
    )
    parser.add_argument(
        "--stalled", action="store_true",
        help="Show only stalled workflows.",
    )
    parser.add_argument(
        "--kind", choices=["local", "gh"], metavar="local|gh",
        help="Filter to a specific workflow kind.",
    )
    parser.add_argument(
        "--since", metavar="DURATION",
        help="Show only workflows started within DURATION (e.g. 30s, 5m, 2h, 1d).",
    )
    parser.add_argument(
        "--recent", nargs="?", const=24, type=int, metavar="N",
        help="Show recently-finished workflows started within N hours (default 24). "
             "Mutually exclusive with --running/--dead/--stalled.",
    )
    parser.add_argument(
        "--watch", nargs="?", const=2, type=int, metavar="SEC",
        help="Refresh the view every SEC seconds (default 2). "
             "Mutually exclusive with positional id argument.",
    )
    parser.add_argument(
        "id_prefix", nargs="?", metavar="id-prefix",
        help="Substring to drill into a single workflow. Mutually exclusive with --watch.",
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
    has_liveness_filter = args.running or args.dead or args.stalled

    if args.recent is not None and has_liveness_filter:
        print("error: --recent cannot be combined with --running/--dead/--stalled", file=sys.stderr)
        return

    if args.recent is not None:
        do_recent(args, here_root=here_root)
    else:
        do_list(args, here_root=here_root)


def main():
    args = parse_args()

    # --watch and positional drill-in are mutually exclusive.
    if args.watch is not None and args.id_prefix is not None:
        print("error: --watch cannot be combined with a positional id argument")
        sys.exit(0)

    # Early exit if state root doesn't exist.
    if not os.path.isdir(STATE_ROOT):
        print("No workflows have been launched on this machine.")
        sys.exit(0)

    # Ack modes don't need here_root.
    if args.ack:
        do_ack(args.ack)
        sys.exit(0)

    if args.ack_all:
        do_ack_all()
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
        print(f"workflows: unexpected error: {exc}", file=sys.stderr)
        sys.exit(0)
