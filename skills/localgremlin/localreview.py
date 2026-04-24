#!/usr/bin/env python3
"""Standalone review-code stage for the /localreview skill.

Runs the same triple-lens parallel reviewer fan-out as the review-code
stage of /localgremlin, but against arbitrary local changes rather than
a gremlin-managed worktree. No GR_ID, no gremlin state dir — review files
are written straight to --dir (defaulting to cwd).

Entry point for the sibling _core.py `run_review_code_stage` helper, which
is the single source of truth shared with localgremlin.py.
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
from typing import List

from _core import (
    MODEL_RE,
    die,
    in_git_repo,
    install_signal_handlers,
    run_review_code_stage,
)


def parse_args(argv: List[str]) -> argparse.Namespace:
    usage = (
        "usage: localreview.py [--dir <path>] [--plan <path>] "
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


def main(argv: List[str]) -> int:
    install_signal_handlers()
    args = parse_args(argv)

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
        if plan_path.stat().st_size == 0:
            die(f"--plan is empty: {plan_path}")
        plan_text = plan_path.read_text(encoding="utf-8")

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
            die("nothing to review: HEAD~1..HEAD has no changes and working tree is clean")

    print(f"==> reviewing code in parallel (models: {args.holistic}, {args.detail}, {args.scope})", flush=True)
    review_a, review_b, review_c = run_review_code_stage(
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
