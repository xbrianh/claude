"""ClaudeClient Protocol and the real subprocess-based implementation.

The Protocol shape mirrors the contract described in `gremlins/DESIGN.md
§ClaudeClient interface`. The ``SubprocessClaudeClient`` is the production
implementation: it spawns ``claude -p`` with the configured flags, tees
raw stream-json to ``raw_path`` if given, and emits a one-line-per-event
human trace to stderr (the parity contract that the bash ``progress_tee``
filter and the old ``_core._emit_event`` printer fulfilled separately).

Phase 1 only exercises the stream-json path used by the local pipeline;
``output_format='text'``, ``resume_session``, ``capture_events``, and
``on_event`` are wired through so Phase 3's ghgremlin port can use the
same client without a second pass.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

CLAUDE_FLAGS_BASE = [
    "--permission-mode", "bypassPermissions",
    "--verbose",
]


@dataclass
class CompletedRun:
    """Outcome of a single ``claude -p`` invocation.

    ``exit_code`` is always populated. ``session_id`` is extracted from the
    stream-json ``system.init`` event when available (None for text-mode runs
    or runs that crashed before emitting init). ``text_result`` holds the
    captured stdout for ``output_format='text'`` runs and is None otherwise.
    ``events`` is populated only when ``capture_events=True``; each entry is
    one parsed stream-json event. ``cost_usd`` is extracted from the
    stream-json ``result`` event when available.
    """

    exit_code: int
    session_id: Optional[str] = None
    text_result: Optional[str] = None
    events: Optional[List[dict]] = None
    cost_usd: Optional[float] = None


class ClaudeClient(Protocol):
    def run(
        self,
        prompt: str,
        *,
        label: str,
        model: Optional[str] = None,
        raw_path: Optional[pathlib.Path] = None,
        output_format: str = "stream-json",
        resume_session: Optional[str] = None,
        extra_flags: Sequence[str] = (),
        capture_events: bool = False,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> CompletedRun:
        ...

    def reap_all(self) -> None:
        ...

    @property
    def total_cost_usd(self) -> float:
        ...


# ---------------------------------------------------------------------------
# Stream-JSON event tracing
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


def stream_events(
    stdout,
    *,
    prefix: str = "",
    raw_path: Optional[pathlib.Path] = None,
    capture: bool = False,
    on_event: Optional[Callable[[dict], None]] = None,
) -> Tuple[Optional[str], Optional[float], Optional[List[dict]]]:
    """Read stream-json lines from stdout, render via _emit_event.

    Returns (session_id, cost_usd, events). events is None when capture=False.
    """
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    events: Optional[List[dict]] = [] if capture else None

    raw = None
    if raw_path is not None:
        raw = open(raw_path, "ab")
    try:
        for line in stdout:
            if raw is not None:
                raw.write(line)
                raw.flush()
            try:
                evt = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue
            if (
                session_id is None
                and evt.get("type") == "system"
                and evt.get("subtype") == "init"
            ):
                sid = evt.get("session_id")
                if isinstance(sid, str):
                    session_id = sid
            if evt.get("type") == "result":
                raw_cost = evt.get("total_cost_usd", evt.get("cost_usd"))
                if isinstance(raw_cost, (int, float)):
                    cost_usd = float(raw_cost)
            if events is not None:
                events.append(evt)
            try:
                _emit_event(prefix, evt)
            except Exception:
                pass
            if on_event is not None:
                try:
                    on_event(evt)
                except Exception:
                    pass
    finally:
        if raw is not None:
            raw.close()
    return session_id, cost_usd, events


# ---------------------------------------------------------------------------
# SubprocessClaudeClient
# ---------------------------------------------------------------------------

class SubprocessClaudeClient:
    """Production ClaudeClient: spawns ``claude -p`` subprocesses.

    Owns the live-children list so ``reap_all()`` (called from the SIGINT/
    SIGTERM handlers installed by ``runner.install_signal_handlers``) can
    terminate every concurrently-running ``claude -p`` before the orchestrator
    exits — the parity contract for the ``trap 'kill -- -$$'`` shape that
    bash gremlins relied on.
    """

    def __init__(self) -> None:
        # Reentrant lock: signal handlers run on the main thread and may land
        # while _track/_untrack already hold it. A plain Lock would deadlock
        # in that narrow window.
        self._lock = threading.RLock()
        self._children: List[subprocess.Popen] = []
        self._total_cost_usd: float = 0.0

    # --- child-process tracking -------------------------------------------

    def _track(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._children.append(p)

    def _untrack(self, p: subprocess.Popen) -> None:
        with self._lock:
            try:
                self._children.remove(p)
            except ValueError:
                pass

    def reap_all(self) -> None:
        with self._lock:
            procs = list(self._children)
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

    @property
    def total_cost_usd(self) -> float:
        return self._total_cost_usd

    # --- main entry point -------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        label: str,
        model: Optional[str] = None,
        raw_path: Optional[pathlib.Path] = None,
        output_format: str = "stream-json",
        resume_session: Optional[str] = None,
        extra_flags: Sequence[str] = (),
        capture_events: bool = False,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> CompletedRun:
        cmd = ["claude", "-p"]
        if model is not None:
            cmd += ["--model", model]
        cmd += list(CLAUDE_FLAGS_BASE)
        cmd += ["--output-format", output_format]
        if resume_session is not None:
            cmd += ["--resume", resume_session]
        cmd += list(extra_flags)
        cmd.append(prompt)

        # Default bufsize (-1) gives a BufferedReader with 8 KiB reads, so
        # readline() scans for '\n' in-buffer instead of doing one os.read()
        # per byte. Streaming latency is preserved (readline returns on '\n'
        # or EOF, it doesn't block for the buffer to fill) and throughput
        # on the big implement-stage stream-json traces jumps by orders of
        # magnitude.
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,
            start_new_session=False,
        )
        self._track(p)

        session_id: Optional[str] = None
        text_chunks: List[str] = []
        events: Optional[List[dict]] = None  # populated only in stream-json mode
        prefix = f"[{label}] " if label else ""
        cost_usd: Optional[float] = None

        try:
            assert p.stdout is not None
            if output_format == "stream-json":
                session_id, cost_usd, events = stream_events(
                    p.stdout,
                    prefix=prefix,
                    raw_path=raw_path,
                    capture=capture_events,
                    on_event=on_event,
                )
                if cost_usd is not None:
                    with self._lock:
                        self._total_cost_usd += cost_usd
            else:
                # text mode — capture stdout, no per-event trace.
                data = p.stdout.read()
                if isinstance(data, bytes):
                    text_chunks.append(data.decode("utf-8", errors="replace"))
                else:
                    text_chunks.append(str(data))
            p.stdout.close()
            rc = p.wait()
        finally:
            self._untrack(p)

        if rc != 0:
            raise RuntimeError(
                f"claude -p (model={model}, label={label}) exited {rc}"
            )

        return CompletedRun(
            exit_code=rc,
            session_id=session_id,
            text_result="".join(text_chunks) if output_format != "stream-json" else None,
            events=events,
            cost_usd=cost_usd,
        )
