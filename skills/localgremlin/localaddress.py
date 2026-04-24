#!/usr/bin/env python3
"""Standalone address-code stage for the /localaddress skill.

Reads the three review-code-*.md files produced by /localreview (or
/localgremlin) from --dir and addresses their findings. In a git repo,
creates one 'Address review feedback' commit. No push.

Entry point for the sibling _core.py `run_address_code_stage` helper,
which is the single source of truth shared with localgremlin.py.
"""

import argparse
import pathlib
import shutil
import sys
from typing import List

from _core import (
    MODEL_RE,
    die,
    in_git_repo,
    install_signal_handlers,
    run_address_code_stage,
)


def parse_args(argv: List[str]) -> argparse.Namespace:
    usage = "usage: localaddress.py [--dir <path>] [-x <address-model>]"
    parser = argparse.ArgumentParser(add_help=False, usage=usage)
    parser.add_argument("--dir", dest="dir", default=".")
    parser.add_argument("-x", dest="address", default="sonnet")
    args = parser.parse_args(argv)
    if not MODEL_RE.match(args.address):
        die(f"invalid model: {args.address}")
    return args


def main(argv: List[str]) -> int:
    install_signal_handlers()
    args = parse_args(argv)

    if shutil.which("claude") is None:
        die("claude CLI not found")

    session_dir = pathlib.Path(args.dir).resolve()
    if not session_dir.is_dir():
        die(f"--dir does not exist: {session_dir}")

    is_git = in_git_repo()

    print(f"==> addressing code reviews (model: {args.address})", flush=True)
    run_address_code_stage(
        session_dir=session_dir,
        address_model=args.address,
        is_git=is_git,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
