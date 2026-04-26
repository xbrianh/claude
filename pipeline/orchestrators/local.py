"""Orchestrator entry points for the local pipeline.

Three callables map onto three CLI subcommands:

- ``local_main`` — full plan → implement → review-code → address-code chain
  (``python -m pipeline.cli local``); the gremlin pipeline.
- ``review_main`` — review-code stage only (``python -m pipeline.cli review``).
  Standalone replacement for the old ``localreview.py``.
- ``address_main`` — address-code stage only (``python -m pipeline.cli
  address``). Standalone replacement for the old ``localaddress.py``.

Each builds a real ``SubprocessClaudeClient`` by default; tests inject a
``FakeClaudeClient`` via the ``client`` argument.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import List, Optional

from ..clients.claude import ClaudeClient, SubprocessClaudeClient
from ..git import in_git_repo
from ..runner import install_signal_handlers, run_stages
from ..stages.address_code import run_address_code_stage
from ..stages.implement import run_implement_stage
from ..stages.plan import run_plan_stage
from ..stages.review_code import run_review_code_stage
from ..state import resolve_session_dir, set_stage

MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
VALID_RESUME_STAGES = ["plan", "implement", "review-code", "address-code"]

# `pragmatic-developer.md` is sourced from the synced agents/ dir under
# ~/.claude. The package lives at ~/.claude/pipeline/, so the agent file
# is two parents up from this module's location.
AGENT_FILE = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "agents"
    / "pragmatic-developer.md"
)


def die(msg: str) -> None:
    sys.stderr.write(f"error: {msg}\n")
    sys.stderr.flush()
    sys.exit(1)


def _load_core_principles() -> str:
    if not AGENT_FILE.exists():
        die(f"missing agent file: {AGENT_FILE}")
    text = AGENT_FILE.read_text(encoding="utf-8")
    in_section = False
    section_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## Core Principles"):
            in_section = True
        elif in_section and line.startswith("## "):
            break
        elif in_section:
            section_lines.append(line)
    if not section_lines:
        die("could not find '## Core Principles' section in pragmatic-developer.md")
    return "".join(section_lines).rstrip()


# ---------------------------------------------------------------------------
# Full local pipeline (plan → implement → review-code → address-code)
# ---------------------------------------------------------------------------

def _parse_local_args(argv: List[str]) -> argparse.Namespace:
    # Short-only model flags to preserve the bash `getopts "p:i:x:a:b:c:"`
    # contract — no `--plan-model` etc. leak in via argparse's default
    # long-form expansion. Long-form flags: `--resume-from` (Phase B rescue)
    # and `--plan` (skip the plan stage, read plan from a file instead).
    usage = (
        'usage: pipeline.cli local [-p <plan-model>] [-i <impl-model>] '
        '[-x <address-model>] [-a <holistic-review-model>] '
        '[-b <detail-review-model>] [-c <scope-review-model>] '
        '[--resume-from <stage>] [--plan <path>] "<instructions>"'
    )
    parser = argparse.ArgumentParser(add_help=False, usage=usage)
    parser.add_argument("-p", dest="plan_model", default="sonnet")
    parser.add_argument("-i", dest="impl", default="sonnet")
    parser.add_argument("-x", dest="address", default="sonnet")
    parser.add_argument("-a", dest="holistic", default="sonnet")
    parser.add_argument("-b", dest="detail", default="sonnet")
    parser.add_argument("-c", dest="scope", default="sonnet")
    parser.add_argument("--resume-from", dest="resume_from", default=None,
                        choices=VALID_RESUME_STAGES)
    parser.add_argument("--plan", dest="plan_path", default=None)
    parser.add_argument("instructions", nargs="*")
    args = parser.parse_args(argv)
    # launch.sh resume may pass an empty-string positional when a --plan
    # gremlin is resumed; treat that as "no positional supplied" rather than
    # a literal empty-string instruction. Narrowed to the resume path so the
    # fresh-launch mutex (`--plan foo.md ""`) still fires on a literal empty
    # string passed alongside --plan.
    if args.resume_from:
        args.instructions = [s for s in args.instructions if s]
    if args.plan_path:
        if args.instructions:
            die("--plan and positional instructions are mutually exclusive")
    else:
        if not args.instructions:
            die(usage)
    for m in (args.plan_model, args.impl, args.address, args.holistic, args.detail, args.scope):
        if not MODEL_RE.match(m):
            die(f"invalid model: {m}")
    return args


def local_main(argv: List[str], *, client: Optional[ClaudeClient] = None) -> int:
    if client is None:
        client = SubprocessClaudeClient()
    install_signal_handlers(client)

    args = _parse_local_args(argv)
    instructions = " ".join(args.instructions)

    if shutil.which("claude") is None:
        die("claude CLI not found")

    session_dir = resolve_session_dir()
    plan_file = session_dir / "plan.md"
    review_code_a = session_dir / f"review-code-holistic-{args.holistic}.md"
    review_code_b = session_dir / f"review-code-detail-{args.detail}.md"
    review_code_c = session_dir / f"review-code-scope-{args.scope}.md"

    print(f"==> session: {session_dir}", flush=True)

    # --plan staging happens up front (before the --resume-from precondition
    # checks below) so `--plan <path> --resume-from implement` works: the
    # `implement` precondition requires plan.md to exist, and if we staged
    # --plan afterwards the precondition would fire first on fresh + resume
    # combos. On resume we skip re-copying — session_dir/plan.md is the
    # durable snapshot per the spec's rescue-determinism rule — and only
    # require the source file on a fresh launch (no snapshot yet).
    plan_copied_from_source = False
    if args.plan_path and not plan_file.exists():
        src = pathlib.Path(args.plan_path)
        if not src.is_file():
            die(f"--plan: file not found: {args.plan_path}")
        if src.stat().st_size == 0:
            die(f"--plan: file is empty: {args.plan_path}")
        shutil.copyfile(src, plan_file)
        plan_copied_from_source = True

    is_git = in_git_repo()
    core_principles = _load_core_principles()

    # Resume preconditions
    start_idx = 0
    if args.resume_from:
        start_idx = VALID_RESUME_STAGES.index(args.resume_from)
        if start_idx >= VALID_RESUME_STAGES.index("implement"):
            if not plan_file.exists() or plan_file.stat().st_size == 0:
                die(f"--resume-from {args.resume_from} requires existing {plan_file}")
        if start_idx >= VALID_RESUME_STAGES.index("review-code"):
            if is_git:
                porcelain = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True, text=True, check=False,
                )
                has_dirty = bool(porcelain.stdout.strip())
                r = subprocess.run(
                    ["git", "rev-list", "--count", "HEAD"],
                    capture_output=True, text=True, check=False,
                )
                has_commits = (r.returncode == 0 and int(r.stdout.strip() or "0") > 0)
                if not has_dirty and not has_commits:
                    die(f"--resume-from {args.resume_from} requires implementation changes in the worktree")
            else:
                has_files = False
                for dirpath, dirnames, filenames in os.walk("."):
                    dirnames[:] = [d for d in dirnames if d != ".git"]
                    try:
                        sd_res = session_dir.resolve()
                        if pathlib.Path(dirpath).resolve() == sd_res:
                            dirnames[:] = []
                            continue
                    except Exception:
                        pass
                    if filenames:
                        has_files = True
                        break
                if not has_files:
                    die(f"--resume-from {args.resume_from} requires implementation changes in the worktree")
        if start_idx >= VALID_RESUME_STAGES.index("address-code"):
            for rf in (review_code_a, review_code_b, review_code_c):
                if not rf.exists() or rf.stat().st_size == 0:
                    die(f"--resume-from {args.resume_from} requires existing {rf}")

    # Stage callables. plan_text is read just-in-time so a mid-stage failure
    # plus resume picks up whatever the plan stage produced.
    plan_text_holder: dict = {}

    def stage_plan() -> None:
        if args.plan_path:
            if plan_copied_from_source:
                print(f"==> [1/4] plan supplied via --plan (copied) -> {plan_file}", flush=True)
            else:
                print(f"==> [1/4] plan reused from snapshot -> {plan_file}", flush=True)
        else:
            set_stage("plan")
            print(f"==> [1/4] planning (model: {args.plan_model}) -> {plan_file}", flush=True)
            run_plan_stage(
                client=client,
                plan_model=args.plan_model,
                plan_file=plan_file,
                instructions=instructions,
                raw_path=session_dir / "stream-plan.jsonl",
            )

    def stage_implement() -> None:
        # Plan text must exist by now. Read fresh so a resume reads the
        # snapshot from disk rather than relying on in-memory state.
        plan_text = plan_file.read_text(encoding="utf-8")
        plan_text_holder["text"] = plan_text
        set_stage("implement")
        print(f"==> [2/4] implementing (model: {args.impl}, from {plan_file})", flush=True)
        run_implement_stage(
            client=client,
            impl_model=args.impl,
            plan_file=plan_file,
            plan_text=plan_text,
            core_principles=core_principles,
            session_dir=session_dir,
            is_git=is_git,
        )

    def stage_review_code() -> None:
        plan_text = plan_text_holder.get("text") or plan_file.read_text(encoding="utf-8")
        set_stage("review-code")
        print(
            f"==> [3/4] reviewing code in parallel "
            f"(models: {args.holistic}, {args.detail}, {args.scope})",
            flush=True,
        )
        a, b, c = run_review_code_stage(
            client=client,
            session_dir=session_dir,
            plan_text=plan_text,
            holistic=args.holistic,
            detail=args.detail,
            scope=args.scope,
            is_git=is_git,
        )
        print(f"    holistic code review ({args.holistic}): {a}", flush=True)
        print(f"    detail code review   ({args.detail}): {b}", flush=True)
        print(f"    scope code review    ({args.scope}): {c}", flush=True)

    def stage_address_code() -> None:
        set_stage("address-code")
        print(f"==> [4/4] addressing code reviews (model: {args.address})", flush=True)
        run_address_code_stage(
            client=client,
            session_dir=session_dir,
            address_model=args.address,
            is_git=is_git,
        )

    stages = [
        ("plan", stage_plan),
        ("implement", stage_implement),
        ("review-code", stage_review_code),
        ("address-code", stage_address_code),
    ]
    run_stages(stages, resume_from=args.resume_from)

    print("", flush=True)
    print(f"done. session artifacts in: {session_dir}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Standalone review-code (was localreview.py)
# ---------------------------------------------------------------------------

def _parse_review_args(argv: List[str]) -> argparse.Namespace:
    usage = (
        "usage: pipeline.cli review [--dir <path>] [--plan <path>] "
        "[-a <holistic-model>] [-b <detail-model>] [-c <scope-model>]"
    )
    parser = argparse.ArgumentParser(add_help=False, usage=usage)
    parser.add_argument("--dir", dest="dir", default=".")
    parser.add_argument("--plan", dest="plan", default=None)
    parser.add_argument("-a", dest="holistic", default="sonnet")
    parser.add_argument("-b", dest="detail", default="sonnet")
    parser.add_argument("-c", dest="scope", default="sonnet")
    args = parser.parse_args(argv)
    for m in (args.holistic, args.detail, args.scope):
        if not MODEL_RE.match(m):
            die(f"invalid model: {m}")
    return args


def review_main(argv: List[str], *, client: Optional[ClaudeClient] = None) -> int:
    if client is None:
        client = SubprocessClaudeClient()
    install_signal_handlers(client)
    args = _parse_review_args(argv)

    if shutil.which("claude") is None:
        die("claude CLI not found")

    session_dir = pathlib.Path(args.dir).resolve()
    if not session_dir.is_dir():
        die(f"--dir does not exist: {session_dir}")

    plan_text = ""
    if args.plan is not None:
        plan_path = pathlib.Path(args.plan)
        if not plan_path.exists():
            die(f"--plan does not exist: {plan_path}")
        if not plan_path.is_file():
            die(f"--plan is not a file: {plan_path}")
        if plan_path.stat().st_size == 0:
            die(f"--plan is empty: {plan_path}")
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            die(f"--plan is not valid UTF-8 text: {plan_path}")
        except OSError as exc:
            die(f"failed to read --plan {plan_path}: {exc}")

    is_git = in_git_repo()
    if is_git:
        # Refuse to spawn three reviewers on an empty diff. HEAD~1 may not
        # exist (initial commit); in that case we require dirty tree to have
        # something worth reviewing.
        head1_exists = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD~1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0
        has_commit_diff = False
        if head1_exists:
            has_commit_diff = subprocess.run(
                ["git", "diff", "--quiet", "HEAD~1", "HEAD"],
                check=False,
            ).returncode != 0
        has_dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        ).stdout.strip())
        if not has_commit_diff and not has_dirty:
            if not head1_exists:
                die("nothing to review: no commit history beyond HEAD and working tree is clean")
            die("nothing to review: HEAD~1..HEAD has no changes and working tree is clean")

    print(
        f"==> reviewing code in parallel "
        f"(models: {args.holistic}, {args.detail}, {args.scope})",
        flush=True,
    )
    review_a, review_b, review_c = run_review_code_stage(
        client=client,
        session_dir=session_dir,
        plan_text=plan_text,
        holistic=args.holistic,
        detail=args.detail,
        scope=args.scope,
        is_git=is_git,
    )
    print(f"    holistic code review ({args.holistic}): {review_a}", flush=True)
    print(f"    detail code review   ({args.detail}): {review_b}", flush=True)
    print(f"    scope code review    ({args.scope}): {review_c}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Standalone address-code (was localaddress.py)
# ---------------------------------------------------------------------------

def _parse_address_args(argv: List[str]) -> argparse.Namespace:
    usage = "usage: pipeline.cli address [--dir <path>] [-x <address-model>]"
    parser = argparse.ArgumentParser(add_help=False, usage=usage)
    parser.add_argument("--dir", dest="dir", default=".")
    parser.add_argument("-x", dest="address", default="sonnet")
    args = parser.parse_args(argv)
    if not MODEL_RE.match(args.address):
        die(f"invalid model: {args.address}")
    return args


def address_main(argv: List[str], *, client: Optional[ClaudeClient] = None) -> int:
    if client is None:
        client = SubprocessClaudeClient()
    install_signal_handlers(client)
    args = _parse_address_args(argv)

    if shutil.which("claude") is None:
        die("claude CLI not found")

    session_dir = pathlib.Path(args.dir).resolve()
    if not session_dir.is_dir():
        die(f"--dir does not exist: {session_dir}")

    is_git = in_git_repo()

    print(f"==> addressing code reviews (model: {args.address})", flush=True)
    run_address_code_stage(
        client=client,
        session_dir=session_dir,
        address_model=args.address,
        is_git=is_git,
    )
    return 0
