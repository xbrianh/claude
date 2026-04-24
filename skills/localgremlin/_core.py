"""Shared primitives for the /localgremlin, /localreview, and /localaddress
skills.

The orchestrator (`localgremlin.py`) and the two standalone CLIs
(`localreview.py`, `localaddress.py`) share identical review-code and
address-code stage logic; this module is the single source of truth for
that code.

Gremlin bookkeeping helpers (`set_stage`, `emit_bail`) are no-ops outside a
gremlin context (no `GR_ID`), so standalone invocations of the CLIs use the
same code paths without trying to patch a state.json that does not exist.
"""

import datetime
import glob
import json
import os
import pathlib
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import List, Optional, Tuple

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SET_STAGE_SH = pathlib.Path.home() / ".claude" / "skills" / "_bg" / "set-stage.sh"
SET_BAIL_SH = pathlib.Path.home() / ".claude" / "skills" / "_bg" / "set-bail.sh"
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


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


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


def emit_bail(bail_class: str, bail_detail: str = "") -> None:
    """Shell out to set-bail.sh to record a bail_class (and optional detail)
    on the running gremlin's state.json. Read by /gremlins rescue --headless
    to decide whether to attempt automated recovery. No-op without GR_ID or
    when the helper is missing — never raises, the stage is already failing
    and bail bookkeeping must not mask the underlying error."""
    gr_id = os.environ.get("GR_ID")
    if not gr_id:
        return
    try:
        if not SET_BAIL_SH.exists() or not os.access(str(SET_BAIL_SH), os.X_OK):
            return
    except Exception:
        return
    try:
        subprocess.run(
            [str(SET_BAIL_SH), gr_id, bail_class, bail_detail],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
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
            elif ct == "thinking":
                thought = c.get("thinking", "") or ""
                out.write(f"{prefix}think: {_trunc(thought)}\n")
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

`{out_file}` is the canonical and required location for your review output in every case, including any short-circuit one-liner the lens tells you to emit. Do not emit the verdict only to chat; write it to `{out_file}` and then stop."""
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
# Session dir + git helpers
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


# ---------------------------------------------------------------------------
# Lens loading
# ---------------------------------------------------------------------------

def load_lenses() -> Tuple[str, str, str]:
    """Return (holistic, detail, scope) lens prose from the sibling
    lens-*-code.md files. Die if any is missing or empty — the gremlin
    can't review without all three lenses."""
    lens_files = {
        "holistic": SCRIPT_DIR / "lens-holistic-code.md",
        "detail": SCRIPT_DIR / "lens-detail-code.md",
        "scope": SCRIPT_DIR / "lens-scope-code.md",
    }
    for path in lens_files.values():
        if not path.exists() or path.stat().st_size == 0:
            die(f"missing or empty lens file: {path}")
    # Explicit utf-8 — lens files contain em-dashes and other non-ASCII, so
    # relying on the process default encoding would crash under a non-UTF-8
    # locale (e.g. a minimal container with LANG=C).
    return (
        lens_files["holistic"].read_text(encoding="utf-8"),
        lens_files["detail"].read_text(encoding="utf-8"),
        lens_files["scope"].read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# Stage-level helpers: review-code and address-code
# ---------------------------------------------------------------------------

def run_review_code_stage(
    session_dir: pathlib.Path,
    plan_text: str,
    holistic: str,
    detail: str,
    scope: str,
    is_git: bool,
) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Execute the review-code stage: load lenses, fan out three reviewers,
    and return the three output paths. Emits bail_class=other on failure
    when running under a gremlin (no-op otherwise). Shared by the
    orchestrator and /localreview.

    Passing ``plan_text=""`` (empty string, not None) intentionally omits
    the plan block from the review prompt entirely — this is the contract
    that lets standalone ``/localreview`` callers run without ``--plan``.
    Any non-empty ``plan_text`` (even whitespace-only) takes the with-plan
    branch and is rendered verbatim into the prompt.

    Stale ``review-code-<lens>-*.md`` files for each lens are unlinked
    before spawning the reviewers so a ``--resume-from review-code`` with
    different ``-a/-b/-c`` models cannot leave the directory with two
    files for the same lens (which would later confuse
    ``run_address_code_stage``'s glob-based discovery).
    """
    review_code_a = session_dir / f"review-code-holistic-{holistic}.md"
    review_code_b = session_dir / f"review-code-detail-{detail}.md"
    review_code_c = session_dir / f"review-code-scope-{scope}.md"

    # Clean up stale per-lens review files from a previous run with
    # different reviewer models. Without this, --resume-from review-code
    # with changed -a/-b/-c would leave two files for the same lens and
    # break run_address_code_stage's uniqueness check.
    for lens in ("holistic", "detail", "scope"):
        for stale in session_dir.glob(f"review-code-{lens}-*.md"):
            try:
                stale.unlink()
            except OSError:
                pass

    focus_a, focus_b, focus_c = load_lenses()

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
    # Omit the plan block entirely when no plan was supplied (standalone
    # /localreview without --plan); sending a bare "The plan for this change
    # is:" header with empty body would confuse the reviewer.
    if plan_text:
        code_review_context = (
            f"The plan for this change is:\n\n{plan_text}\n\n{code_scope}"
        )
    else:
        code_review_context = code_scope

    # Wrap so any infrastructure failure (claude -p crash, missing output
    # file, etc.) records bail_class=other before the SystemExit propagates.
    # Headless rescue can attempt the `other` class — but at least the bail
    # field tells callers *something* failed during review-code rather than
    # leaving them to grep the log.
    try:
        run_triple_review(
            context=code_review_context,
            focuses=(focus_a, focus_b, focus_c),
            out_files=(review_code_a, review_code_b, review_code_c),
            models=(holistic, detail, scope),
            where_field="**File:** `path/to/file.ext:<line>`",
            session_dir=session_dir,
        )
    except (SystemExit, Exception) as exc:
        emit_bail("other", f"review-code stage failed: {exc}"[:200])
        raise

    return review_code_a, review_code_b, review_code_c


def run_address_code_stage(
    session_dir: pathlib.Path,
    address_model: str,
    is_git: bool,
) -> None:
    """Execute the address-code stage: glob for the three review files,
    build the address prompt, and invoke claude. Emits bail_class=other
    on failure when running under a gremlin (no-op otherwise) — including
    failures during glob/validation before claude is spawned. Shared by
    the orchestrator and /localaddress."""
    # Outer try/except so *any* stage failure (missing or ambiguous review
    # files, invalid model in a filename, read errors, or the claude -p
    # subprocess itself) records a bail marker before SystemExit
    # propagates. Without this wrapping the pre-claude failure paths would
    # exit via die() without ever calling emit_bail, and headless rescue
    # would have no bail_class to act on.
    try:
        review_files = {}
        for lens in ("holistic", "detail", "scope"):
            matches = sorted(glob.glob(str(session_dir / f"review-code-{lens}-*.md")))
            if not matches:
                die(f"no review-code-{lens}-*.md file found in {session_dir}")
            if len(matches) > 1:
                die(
                    f"multiple review-code-{lens}-*.md files in {session_dir}: "
                    f"{', '.join(matches)}"
                )
            review_files[lens] = pathlib.Path(matches[0])

        # Model names are embedded in the filenames as the suffix between
        # the lens tag and the `.md` extension. Extracting them here keeps
        # the address prompt's per-lens model labels in sync with the files
        # that were actually produced, for both orchestrator and standalone
        # callers. Validate against MODEL_RE so a malformed filename (e.g.
        # review-code-holistic-.md, or one with unexpected characters)
        # fails loudly instead of producing an empty/garbled prompt label.
        def _model_from(path: pathlib.Path, lens: str) -> str:
            stem = path.stem  # review-code-<lens>-<model>
            prefix = f"review-code-{lens}-"
            model = stem[len(prefix):] if stem.startswith(prefix) else ""
            if not model or not MODEL_RE.match(model):
                die(
                    f"cannot extract a valid model name from review file: "
                    f"{path.name}"
                )
            return model

        model_a = _model_from(review_files["holistic"], "holistic")
        model_b = _model_from(review_files["detail"], "detail")
        model_c = _model_from(review_files["scope"], "scope")

        text_a = review_files["holistic"].read_text(encoding="utf-8")
        text_b = review_files["detail"].read_text(encoding="utf-8")
        text_c = review_files["scope"].read_text(encoding="utf-8")

        address_commit_instr = ""
        if is_git:
            address_commit_instr = (
                "After making all fixes, stage the changed files by name and "
                "create a single git commit titled 'Address review feedback' whose "
                "body references all three review files. Do not push."
            )

        # Only attached when running under a gremlin so direct invocations
        # (no GR_ID) don't see prompt instructions for a helper they can't
        # usefully invoke.
        bail_section = ""
        if os.environ.get("GR_ID"):
            bail_section = """

If a finding asks you to change something that touches secrets/credentials, or you decline to address one or more findings for any other reason that should halt automated recovery, run the bail helper before finishing:
  - `~/.claude/skills/_bg/set-bail.sh "$GR_ID" secrets "<one-line reason>"` if the blocked finding touches secrets.
  - `~/.claude/skills/_bg/set-bail.sh "$GR_ID" other "<one-line reason>"` for any other reason you cannot proceed.
Do not call this helper if you successfully addressed every actionable finding.
"""

        address_prompt = f"""Three independent code reviews of the most recent implementation follow. The reviewers have different lenses by design, so their findings will mostly be complementary rather than overlapping — still deduplicate where they do overlap. For every actionable finding you agree with, make the fix in the code. For findings you disagree with or choose to skip, note them briefly in your final summary with a reason.

---
**Holistic reviewer** (model: {model_a}):

{text_a}

---
**Detail reviewer** (model: {model_b}):

{text_b}

---
**Scope reviewer** (model: {model_c}):

{text_c}

---

{address_commit_instr}{bail_section}

End with a short summary (to stdout) of: what you addressed, what you skipped and why."""
        run_claude(
            address_model, address_prompt, "address-code",
            session_dir / "stream-address.jsonl",
        )
    except (SystemExit, Exception) as exc:
        emit_bail("other", f"address-code stage failed: {exc}"[:200])
        raise
