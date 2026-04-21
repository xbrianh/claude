#!/usr/bin/env python3
"""Background gremlin for the /localgremlin skill.

Runs under the _bg launcher (which exports GR_ID and manages state.json under
${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins/<GR_ID>/). Direct
invocations have no GR_ID and nest their artifacts under
$STATE_ROOT/direct/<ts>-<rand>/artifacts/ so they're visually separated from
real gremlins and can be pruned on a simpler age-based heuristic.

Artifacts (plan.md, the three review-code-*.md files, and raw stream-json
traces) live under that session dir — outside the product branch — so they
survive worktree removal and aren't committed into whatever branch the
implementation stage produced.

Stages: plan → implement → review-code (triple-lens parallel) → address-code.
The gremlin shells out to ~/.claude/skills/_bg/set-stage.sh at each boundary
so `/gremlins` and the session-summary hook can see where it is; sub_stage
for review-code is a {holistic, detail, scope} dict that flips running→done
as each reviewer thread finishes.
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import List, Optional, Tuple

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SET_STAGE_SH = pathlib.Path.home() / ".claude" / "skills" / "_bg" / "set-stage.sh"
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
GR_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

CLAUDE_FLAGS = [
    "--permission-mode", "bypassPermissions",
    "--output-format", "stream-json",
    "--verbose",
]


# ---------------------------------------------------------------------------
# Child-process tracking
# ---------------------------------------------------------------------------
# Reviewer threads spawn their own `claude -p` subprocesses in parallel. On
# SIGINT/SIGTERM we need to terminate every live child so a Ctrl-C'd run
# doesn't leave orphaned claude processes burning tokens (the bash equivalent
# was `trap 'kill -- -$$'`). We keep a module-level list of Popens under a
# reentrant lock — signal handlers run on the main thread and may land while
# _track/_untrack already hold it, which would deadlock a plain Lock in that
# narrow window. Children are added on spawn and removed on wait().
_children_lock = threading.RLock()
_children: List[subprocess.Popen] = []


def _track(p: subprocess.Popen) -> None:
    with _children_lock:
        _children.append(p)


def _untrack(p: subprocess.Popen) -> None:
    with _children_lock:
        try:
            _children.remove(p)
        except ValueError:
            pass


def _reap_all() -> None:
    with _children_lock:
        procs = list(_children)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    deadline = time.time() + 2.0
    for p in procs:
        remaining = max(0.0, deadline - time.time())
        try:
            p.wait(timeout=remaining)
        except Exception:
            pass
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


def _signal_handler(signum, frame):
    _reap_all()
    sys.exit(130)


def die(msg: str) -> None:
    sys.stderr.write(f"error: {msg}\n")
    sys.stderr.flush()
    sys.exit(1)


# ---------------------------------------------------------------------------
# Stage bookkeeping
# ---------------------------------------------------------------------------

def set_stage(stage: str, sub_stage=None) -> None:
    """Shell out to set-stage.sh. No-op without GR_ID or when the helper is
    missing/non-executable. Never raises — stage bookkeeping must not break
    a running gremlin."""
    gr_id = os.environ.get("GR_ID")
    if not gr_id:
        return
    try:
        if not SET_STAGE_SH.exists() or not os.access(str(SET_STAGE_SH), os.X_OK):
            return
    except Exception:
        return
    args = [str(SET_STAGE_SH), gr_id, stage]
    if sub_stage is not None:
        try:
            args.append(json.dumps(sub_stage))
        except Exception:
            return
    try:
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stream-JSON logger
# ---------------------------------------------------------------------------

def _trunc(s, n: int = 200) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = s.replace("\n", " ")
    return s[:n] + "..." if len(s) > n else s


def _emit_event(prefix: str, evt: dict) -> None:
    t = evt.get("type")
    out = sys.stderr
    if t == "system":
        if evt.get("subtype") != "init":
            return
        out.write(
            f"{prefix}init session={evt.get('session_id', '?')} "
            f"model={evt.get('model', '?')} cwd={evt.get('cwd', '?')}\n"
        )
    elif t == "assistant":
        content = (evt.get("message") or {}).get("content") or []
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                out.write(f"{prefix}text: {_trunc(c.get('text', ''))}\n")
            elif ct == "tool_use":
                inp = c.get("input") or {}
                arg = ""
                if isinstance(inp, dict):
                    for k in ("file_path", "command", "pattern", "url", "output_file"):
                        v = inp.get(k)
                        if v:
                            arg = v
                            break
                out.write(f"{prefix}tool: {c.get('name', '?')} {_trunc(str(arg))}\n")
    elif t == "user":
        content = (evt.get("message") or {}).get("content") or []
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            err = " ERROR" if c.get("is_error") is True else ""
            body = c.get("content")
            if isinstance(body, list):
                body_s = " ".join(
                    (p.get("text") or "") for p in body if isinstance(p, dict)
                )
            elif isinstance(body, str):
                body_s = body
            elif body is None:
                body_s = ""
            else:
                body_s = str(body)
            out.write(f"{prefix}result{err}: {_trunc(body_s)}\n")
    elif t == "result":
        cost = evt.get("total_cost_usd", evt.get("cost_usd", "?"))
        out.write(
            f"{prefix}final: subtype={evt.get('subtype', '?')} "
            f"turns={evt.get('num_turns', '?')} cost={cost}\n"
        )
    out.flush()


def log_stream(label: str, raw_path: pathlib.Path, stdout) -> None:
    """Tee raw stream-json lines from `stdout` to `raw_path` and emit a
    human-readable trace to stderr. Malformed lines are skipped silently
    (parity with the bash `jq ... || true` — a truncated JSON line from a
    crashing `claude -p` must not abort the stage)."""
    prefix = f"[{label}] " if label else ""
    with open(raw_path, "ab") as raw:
        for line in stdout:
            raw.write(line)
            raw.flush()
            try:
                evt = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue
            try:
                _emit_event(prefix, evt)
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def run_claude(model: str, prompt: str, label: str, raw_path: pathlib.Path) -> None:
    """Spawn `claude -p`, stream its stdout through log_stream, and raise
    RuntimeError on non-zero exit."""
    cmd = ["claude", "-p", "--model", model, *CLAUDE_FLAGS, prompt]
    # Default bufsize (-1) gives a BufferedReader with 8 KiB reads, so
    # readline() scans for '\n' in-buffer instead of doing one os.read() per
    # byte. Streaming latency is preserved (readline returns on '\n' or EOF,
    # it doesn't block for the buffer to fill) and throughput on the big
    # implement-stage stream-json traces jumps by orders of magnitude.
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=None,
        start_new_session=False,
    )
    _track(p)
    try:
        assert p.stdout is not None
        log_stream(label, raw_path, p.stdout)
        p.stdout.close()
        rc = p.wait()
    finally:
        _untrack(p)
    if rc != 0:
        raise RuntimeError(f"claude -p (model={model}, label={label}) exited {rc}")


def run_review(
    model: str,
    out_file: pathlib.Path,
    focus: str,
    context: str,
    where_field: str,
    label: str,
    raw_path: pathlib.Path,
) -> None:
    """Generic reviewer runner. CONTEXT describes what is being reviewed;
    FOCUS is the lens prose; WHERE_FIELD is the field label used to cite
    findings (e.g. `**File:** path:line` for code reviews)."""
    prompt = f"""Read surrounding code as needed — don't review in isolation.

{context}

Structure your review as markdown:

# Review ({model})

## Summary
2-4 sentences overall.

## Findings
For each actionable finding:
### <short title>
- {where_field}
- **Severity:** blocker | major | minor | nit
- **What:** what's wrong
- **Fix:** concrete suggestion

If there are no issues worth raising, write a Findings section that says so explicitly.

Do NOT make any code changes — only write the review file.

{focus}

Write your review to `{out_file}`."""
    run_claude(model, prompt, label, raw_path)


# ---------------------------------------------------------------------------
# Triple-reviewer fan-out
# ---------------------------------------------------------------------------

class ReviewWorker(threading.Thread):
    """Runs one lens's reviewer in its own thread. Exceptions are captured
    on `self.error` so the main thread can decide whether to die()."""

    def __init__(
        self,
        *,
        model: str,
        out_file: pathlib.Path,
        focus: str,
        context: str,
        where_field: str,
        label: str,
        raw_path: pathlib.Path,
    ) -> None:
        super().__init__(daemon=True)
        self.model = model
        self.out_file = out_file
        self.focus = focus
        self.context = context
        self.where_field = where_field
        self.label = label
        self.raw_path = raw_path
        self.error: Optional[Exception] = None

    def run(self) -> None:
        try:
            run_review(
                self.model,
                self.out_file,
                self.focus,
                self.context,
                self.where_field,
                self.label,
                self.raw_path,
            )
        except Exception as e:  # noqa: BLE001
            self.error = e
            sys.stderr.write(f"review {self.model} failed: {e}\n")
            sys.stderr.flush()


def run_triple_review(
    context: str,
    focuses: Tuple[str, str, str],
    out_files: Tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    models: Tuple[str, str, str],
    where_field: str,
    session_dir: pathlib.Path,
) -> None:
    """Spawn three reviewer threads, emit sub_stage updates as each finishes,
    and die() if any worker failed or produced an empty output file."""
    model_a, model_b, model_c = models
    out_a, out_b, out_c = out_files
    focus_a, focus_b, focus_c = focuses

    # Stable lens labels (not model names) as sub_stage keys so the shape is
    # unambiguous when two lenses share a model. Model name is embedded in
    # the value so status output can show it. The lens key also goes into
    # each raw-trace filename so three reviewers sharing a model (the default
    # sonnet×3 case) don't concurrently append to the same .jsonl.
    lens_keys = ("holistic", "detail", "scope")

    workers = [
        ReviewWorker(
            model=model_a, out_file=out_a, focus=focus_a, context=context,
            where_field=where_field, label=f"review-code:{model_a}",
            raw_path=session_dir / f"stream-review-code-{lens_keys[0]}-{model_a}.jsonl",
        ),
        ReviewWorker(
            model=model_b, out_file=out_b, focus=focus_b, context=context,
            where_field=where_field, label=f"review-code:{model_b}",
            raw_path=session_dir / f"stream-review-code-{lens_keys[1]}-{model_b}.jsonl",
        ),
        ReviewWorker(
            model=model_c, out_file=out_c, focus=focus_c, context=context,
            where_field=where_field, label=f"review-code:{model_c}",
            raw_path=session_dir / f"stream-review-code-{lens_keys[2]}-{model_c}.jsonl",
        ),
    ]
    statuses = ["running", "running", "running"]

    def emit_sub_stage() -> None:
        sub = {
            lens_keys[i]: f"{statuses[i]} ({workers[i].model})"
            for i in range(3)
        }
        set_stage("review-code", sub)

    for w in workers:
        w.start()
    emit_sub_stage()

    while any(s == "running" for s in statuses):
        changed = False
        for i, w in enumerate(workers):
            if statuses[i] == "running" and not w.is_alive():
                w.join()
                statuses[i] = "done"
                changed = True
        if changed:
            emit_sub_stage()
        if any(s == "running" for s in statuses):
            time.sleep(2)

    failures = [w.model for w in workers if w.error is not None]
    if failures:
        die("one or more reviews failed")
    for w, out in zip(workers, out_files):
        if not out.exists() or out.stat().st_size == 0:
            die(f"review {w.model} did not produce {out}")


# ---------------------------------------------------------------------------
# Session dir + implementation-change detection
# ---------------------------------------------------------------------------

def resolve_session_dir() -> pathlib.Path:
    state_root = pathlib.Path(
        os.environ.get("XDG_STATE_HOME")
        or os.path.join(os.path.expanduser("~"), ".local", "state")
    ) / "claude-gremlins"
    gr_id = os.environ.get("GR_ID", "")
    if gr_id:
        if not GR_ID_RE.match(gr_id):
            die(f"invalid GR_ID: {gr_id}")
        session_dir = state_root / gr_id / "artifacts"
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        rand = secrets.token_hex(3)  # 6 hex chars
        session_dir = state_root / "direct" / f"{ts}-{rand}" / "artifacts"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def in_git_repo() -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def git_head() -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def changes_outside_git(sentinel: pathlib.Path, session_dir: pathlib.Path) -> bool:
    try:
        threshold = sentinel.stat().st_mtime
    except Exception:
        return False
    cwd = pathlib.Path(".").resolve()
    try:
        session_resolved = session_dir.resolve()
    except Exception:
        session_resolved = session_dir
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        dp = pathlib.Path(dirpath)
        try:
            dp_resolved = dp.resolve()
            if dp_resolved == session_resolved or session_resolved in dp_resolved.parents:
                dirnames[:] = []
                continue
        except Exception:
            pass
        for f in filenames:
            fp = dp / f
            try:
                if fp.stat().st_mtime > threshold:
                    return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    # Short-only flags to preserve the bash `getopts "p:i:x:a:b:c:"` contract —
    # no `--plan-model` etc. leak in via argparse's default long-form expansion.
    usage = (
        'usage: localgremlin.py [-p <plan-model>] [-i <impl-model>] '
        '[-x <address-model>] [-a <holistic-review-model>] '
        '[-b <detail-review-model>] [-c <scope-review-model>] "<instructions>"'
    )
    parser = argparse.ArgumentParser(add_help=False, usage=usage)
    parser.add_argument("-p", dest="plan", default="sonnet")
    parser.add_argument("-i", dest="impl", default="sonnet")
    parser.add_argument("-x", dest="address", default="sonnet")
    parser.add_argument("-a", dest="holistic", default="sonnet")
    parser.add_argument("-b", dest="detail", default="sonnet")
    parser.add_argument("-c", dest="scope", default="sonnet")
    parser.add_argument("instructions", nargs="*")
    # No try/except around parse_args: argparse already prints its own
    # `usage: …\nlocalgremlin.py: error: <specific>` to stderr before
    # raising SystemExit. Wrapping it would bury the specific error behind
    # a second copy of the usage line.
    args = parser.parse_args(argv)
    if not args.instructions:
        die(usage)
    for m in (args.plan, args.impl, args.address, args.holistic, args.detail, args.scope):
        if not MODEL_RE.match(m):
            die(f"invalid model: {m}")
    return args


def main(argv: List[str]) -> int:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    args = parse_args(argv)
    instructions = " ".join(args.instructions)

    if shutil.which("claude") is None:
        die("claude CLI not found")

    session_dir = resolve_session_dir()
    plan_file = session_dir / "plan.md"
    review_code_a = session_dir / f"review-code-holistic-{args.holistic}.md"
    review_code_b = session_dir / f"review-code-detail-{args.detail}.md"
    review_code_c = session_dir / f"review-code-scope-{args.scope}.md"

    print(f"==> session: {session_dir}", flush=True)

    is_git = in_git_repo()

    # Reviewer focuses. Two complementary lenses (holistic / detail) are told
    # to stay out of each other's lane so the reviews are complementary.
    lens_files = {
        "holistic": SCRIPT_DIR / "lens-holistic-code.md",
        "detail": SCRIPT_DIR / "lens-detail-code.md",
        "scope": SCRIPT_DIR / "lens-scope-code.md",
    }
    for name, path in lens_files.items():
        if not path.exists() or path.stat().st_size == 0:
            die(f"missing or empty lens file: {path}")
    # Explicit utf-8 — lens files contain em-dashes and other non-ASCII, so
    # relying on the process default encoding would crash under a non-UTF-8
    # locale (e.g. a minimal container with LANG=C).
    focus_a = lens_files["holistic"].read_text(encoding="utf-8")
    focus_b = lens_files["detail"].read_text(encoding="utf-8")
    focus_c = lens_files["scope"].read_text(encoding="utf-8")

    pragmatic_dev_file = (SCRIPT_DIR / "../../agents/pragmatic-developer.md").resolve()
    if not pragmatic_dev_file.exists():
        die(f"missing agent file: {pragmatic_dev_file}")
    agent_text = pragmatic_dev_file.read_text(encoding="utf-8")
    in_section = False
    section_lines: list[str] = []
    for line in agent_text.splitlines(keepends=True):
        if line.startswith("## Core Principles"):
            in_section = True
        elif in_section and line.startswith("## "):
            break
        elif in_section:
            section_lines.append(line)
    if not section_lines:
        die("could not find '## Core Principles' section in pragmatic-developer.md")
    core_principles = "".join(section_lines).rstrip()

    # Stages run back-to-back; inserting sleeps >~5 min between them drops the Anthropic prompt cache TTL and loses inter-stage cache benefits.
    # ----- plan -----
    set_stage("plan")
    print(f"==> [1/4] planning (model: {args.plan}) -> {plan_file}", flush=True)
    plan_prompt = f"""Create a detailed implementation plan for the following task and write it to the file `{plan_file}`. Use this structure:

## Context
What problem are we solving and why.

## Approach
High-level strategy. Why this approach over alternatives.

## Tasks
- [ ] Task 1: concrete, specific description
- [ ] Task 2: concrete, specific description

## Open questions
Anything that needs discussion before implementation.

Read any relevant code in the repo to inform the plan. Do NOT make any code changes yet — only write the plan file.

Task: {instructions}"""
    run_claude(args.plan, plan_prompt, "plan", session_dir / "stream-plan.jsonl")
    if not plan_file.exists() or plan_file.stat().st_size == 0:
        die(f"plan stage did not produce {plan_file}")
    plan_text = plan_file.read_text(encoding="utf-8")

    # ----- implement -----
    set_stage("implement")
    print(f"==> [2/4] implementing (model: {args.impl}, from {plan_file})", flush=True)
    pre_head = ""
    pre_sentinel: Optional[pathlib.Path] = None
    if is_git:
        pre_head = git_head()
    else:
        pre_sentinel = session_dir / ".pre-impl"
        pre_sentinel.touch()

    # The commit message references `plan.md` (basename) rather than the
    # absolute session-dir path, which is user-specific and would end up in
    # git history otherwise.
    impl_commit_instr = "."
    if is_git:
        impl_commit_instr = (
            ", stage the changed files by name and create a single git commit "
            "with a clear message that references the implementation plan "
            "(refer to it as `plan.md` in the commit message, not by absolute "
            "path). Do NOT create any meta/scaffolding files in the repo — no "
            "`.claude-workflow/` directory, no `plan.md`, no review docs, no "
            "notes-to-self. Do not push."
        )
    impl_prompt = (
        f"When writing code, follow these principles:\n\n{core_principles}\n\n"
        f"{plan_text}\n\n"
        f"Implement every task in the plan above by editing code in this repo. "
        f"When the implementation is complete{impl_commit_instr}"
    )
    run_claude(args.impl, impl_prompt, "implement", session_dir / "stream-implement.jsonl")

    # Spec invariant: an empty implementation must never flow into code review.
    if is_git:
        post_head = git_head()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        if post_head == pre_head and not porcelain.stdout.strip():
            die("implementation stage produced no changes; aborting")
    else:
        assert pre_sentinel is not None
        if not changes_outside_git(pre_sentinel, session_dir):
            die("implementation stage produced no changes; aborting")

    # ----- review-code -----
    set_stage("review-code")
    print(
        f"==> [3/4] reviewing code in parallel "
        f"(models: {args.holistic}, {args.detail}, {args.scope})",
        flush=True,
    )
    if is_git:
        code_scope = (
            "Review the changes introduced by the most recent commit "
            "(HEAD vs HEAD~1) plus any uncommitted working-tree changes. "
            "Use `git diff HEAD~1 HEAD` and `git diff` to see the scope."
        )
    else:
        code_scope = (
            "Review the uncommitted changes in this directory (`git diff` if "
            "available, otherwise inspect recently modified files)."
        )
    code_review_context = (
        f"The plan for this change is:\n\n{plan_text}\n\n{code_scope}"
    )
    run_triple_review(
        context=code_review_context,
        focuses=(focus_a, focus_b, focus_c),
        out_files=(review_code_a, review_code_b, review_code_c),
        models=(args.holistic, args.detail, args.scope),
        where_field="**File:** `path/to/file.ext:<line>`",
        session_dir=session_dir,
    )
    print(f"    holistic code review ({args.holistic}): {review_code_a}", flush=True)
    print(f"    detail code review   ({args.detail}): {review_code_b}", flush=True)
    print(f"    scope code review    ({args.scope}): {review_code_c}", flush=True)

    # ----- address-code -----
    set_stage("address-code")
    print(f"==> [4/4] addressing code reviews (model: {args.address})", flush=True)
    address_commit_instr = ""
    if is_git:
        address_commit_instr = (
            "After making all fixes, stage the changed files by name and "
            "create a single git commit titled 'Address review feedback' whose "
            "body references all three review files. Do not push."
        )
    text_a = review_code_a.read_text(encoding="utf-8")
    text_b = review_code_b.read_text(encoding="utf-8")
    text_c = review_code_c.read_text(encoding="utf-8")
    address_prompt = f"""Three independent code reviews of the most recent implementation follow. The reviewers have different lenses by design, so their findings will mostly be complementary rather than overlapping — still deduplicate where they do overlap. For every actionable finding you agree with, make the fix in the code. For findings you disagree with or choose to skip, note them briefly in your final summary with a reason.

---
**Holistic reviewer** (model: {args.holistic}):

{text_a}

---
**Detail reviewer** (model: {args.detail}):

{text_b}

---
**Scope reviewer** (model: {args.scope}):

{text_c}

---

{address_commit_instr}

End with a short summary (to stdout) of: what you addressed, what you skipped and why."""
    run_claude(
        args.address, address_prompt, "address-code",
        session_dir / "stream-address.jsonl",
    )

    print("", flush=True)
    print(f"done. session artifacts in: {session_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
