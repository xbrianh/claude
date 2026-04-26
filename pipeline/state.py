"""Stage and bail bookkeeping wrappers.

Both helpers shell out to the canonical bash scripts under
``$HOME/.claude/skills/_bg/`` rather than reimplementing the state.json
patching logic in Python. The bash scripts are also invoked by non-pipeline
code paths (``session-summary.sh`` is a hook; ``liveness.sh`` is sourced by
``gremlins.py``), so leaving them as the single source of truth keeps the
on-disk vocabulary stable across pipeline and non-pipeline writers.

Both helpers are no-ops outside a gremlin context (no ``GR_ID``) and never
raise — stage/bail bookkeeping must not break a running gremlin.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import secrets
import subprocess

SET_STAGE_SH = pathlib.Path.home() / ".claude" / "skills" / "_bg" / "set-stage.sh"
SET_BAIL_SH = pathlib.Path.home() / ".claude" / "skills" / "_bg" / "set-bail.sh"

GR_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# The four bail-class strings written to state.json.bail_class. Byte-stable
# across the migration — these strings appear in state.json files written by
# the old code that the new code must continue to read. See
# ``pipeline/DESIGN.md §Bail-class vocabulary``.
BAIL_CLASS_REVIEWER_REQUESTED_CHANGES = "reviewer_requested_changes"
BAIL_CLASS_SECURITY = "security"
BAIL_CLASS_SECRETS = "secrets"
BAIL_CLASS_OTHER = "other"


def set_stage(stage: str, sub_stage=None) -> None:
    """Shell out to set-stage.sh. No-op without GR_ID or when the helper is
    missing/non-executable."""
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


def resolve_session_dir() -> pathlib.Path:
    """Resolve the artifacts directory for the current run.

    Under a gremlin (``GR_ID`` set and valid), nests under
    ``$STATE_ROOT/<gr_id>/artifacts/`` so the launcher's state.json and the
    pipeline artifacts share a parent. Direct invocations (no ``GR_ID``, or a
    malformed ``GR_ID`` treated as absent rather than raising) nest under
    ``$STATE_ROOT/direct/<ts>-<rand>/artifacts/`` so they're visually separated
    from real gremlins and can be pruned on a simpler age-based heuristic.
    """
    state_root = pathlib.Path(
        os.environ.get("XDG_STATE_HOME")
        or os.path.join(os.path.expanduser("~"), ".local", "state")
    ) / "claude-gremlins"
    gr_id = os.environ.get("GR_ID", "")
    if gr_id and not GR_ID_RE.match(gr_id):
        # Malformed GR_ID — treat as a direct invocation rather than raising a
        # raw Python traceback for malformed environment input.
        gr_id = ""
    if gr_id:
        session_dir = state_root / gr_id / "artifacts"
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        rand = secrets.token_hex(3)  # 6 hex chars
        session_dir = state_root / "direct" / f"{ts}-{rand}" / "artifacts"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def emit_bail(bail_class: str, bail_detail: str = "") -> None:
    """Shell out to set-bail.sh to record a bail_class (and optional detail)
    on the running gremlin's state.json. No-op without GR_ID or when the
    helper is missing — never raises."""
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


def resolve_state_file() -> "Optional[pathlib.Path]":
    """Return path to state.json for the current GR_ID, or None when no GR_ID is set."""
    gr_id = os.environ.get("GR_ID", "")
    if not gr_id or not GR_ID_RE.match(gr_id):
        return None
    state_root = pathlib.Path(
        os.environ.get("XDG_STATE_HOME")
        or os.path.join(os.path.expanduser("~"), ".local", "state")
    ) / "claude-gremlins"
    return state_root / gr_id / "state.json"


def patch_state(**fields) -> None:
    """Merge keyword fields into state.json atomically.

    No-op when GR_ID is unset, when state.json doesn't exist, or when the
    write fails — stage bookkeeping must not crash a running gremlin.
    """
    sf = resolve_state_file()
    if sf is None or not sf.exists():
        return
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        data.update(fields)
        tmp = sf.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.rename(sf)
    except Exception:
        pass


def check_bail(label: str = "stage") -> None:
    """Raise RuntimeError if a bail_class was written to state.json by the
    just-completed stage.  No-op without GR_ID or when state.json is absent."""
    sf = resolve_state_file()
    if sf is None or not sf.exists():
        return
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        bail_class = data.get("bail_class", "")
        if bail_class:
            raise RuntimeError(
                f"{label} bailed: bail_class={bail_class} (see state.json bail_detail)"
            )
    except RuntimeError:
        raise
    except Exception:
        pass
