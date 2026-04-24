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

Review-code and address-code stage bodies live in `_core.py` so the
standalone /localreview and /localaddress skills execute the same code.
"""

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
from typing import List, Optional

from _core import (
    MODEL_RE,
    SCRIPT_DIR,
    die,
    git_head,
    in_git_repo,
    install_signal_handlers,
    resolve_session_dir,
    run_address_code_stage,
    run_claude,
    run_review_code_stage,
    set_stage,
)


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

VALID_RESUME_STAGES = ["plan", "implement", "review-code", "address-code"]


def parse_args(argv: List[str]) -> argparse.Namespace:
    # Short-only model flags to preserve the bash `getopts "p:i:x:a:b:c:"`
    # contract — no `--plan-model` etc. leak in via argparse's default
    # long-form expansion. Long-form flags: `--resume-from` (Phase B rescue)
    # and `--plan` (skip the plan stage, read plan from a file instead).
    usage = (
        'usage: localgremlin.py [-p <plan-model>] [-i <impl-model>] '
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
    # No try/except around parse_args: argparse already prints its own
    # `usage: …\nlocalgremlin.py: error: <specific>` to stderr before
    # raising SystemExit. Wrapping it would bury the specific error behind
    # a second copy of the usage line.
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
        # Source-file validation is deferred to main() so it can check the
        # session_dir/plan.md snapshot first: on resume the snapshot is the
        # durable record and the source may have been deleted or edited;
        # only a fresh launch (no snapshot) actually needs the source file.
    else:
        if not args.instructions:
            die(usage)
    for m in (args.plan_model, args.impl, args.address, args.holistic, args.detail, args.scope):
        if not MODEL_RE.match(m):
            die(f"invalid model: {m}")
    return args


def main(argv: List[str]) -> int:
    install_signal_handlers()

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

    # Resume plumbing: start_idx is the index into VALID_RESUME_STAGES of the
    # first stage to execute. Stages before it are skipped; artifacts they
    # would have produced must already exist on disk (enforced below).
    start_idx = 0
    if args.resume_from:
        start_idx = VALID_RESUME_STAGES.index(args.resume_from)
        # Precondition: implement/review-code/address-code all need plan.md.
        if start_idx >= VALID_RESUME_STAGES.index("implement"):
            if not plan_file.exists() or plan_file.stat().st_size == 0:
                die(f"--resume-from {args.resume_from} requires existing {plan_file}")
        # Precondition: review-code/address-code need evidence of the impl
        # stage. Mirrors the post-implement invariant below:
        #   - git mode: uncommitted changes OR any commit reachable from HEAD
        #     (we don't have pre_head on resume, so we accept any non-empty
        #     HEAD history as "impl happened").
        #   - non-git mode: the worktree has any non-metadata file. We don't
        #     have the pre-impl sentinel's mtime across a resume, so a stricter
        #     "modified since pre-impl" check isn't available — an empty
        #     worktree is the only unambiguous "nothing was implemented" signal.
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
                    # Skip the session dir (plan/review artifacts live there and
                    # aren't product evidence by themselves).
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
        # Precondition: address-code needs all three review files. Filenames
        # embed the reviewer-model name, so this also implicitly requires the
        # resume to use the same -a/-b/-c models as the original run. If the
        # original used custom models and the resume doesn't, the precondition
        # will fail with a clear error naming the missing file.
        if start_idx >= VALID_RESUME_STAGES.index("address-code"):
            for rf in (review_code_a, review_code_b, review_code_c):
                if not rf.exists() or rf.stat().st_size == 0:
                    die(f"--resume-from {args.resume_from} requires existing {rf}")

    # Stages run back-to-back; inserting sleeps >~5 min between them drops the Anthropic prompt cache TTL and loses inter-stage cache benefits.
    # ----- plan -----
    # When --plan <path> is set, the plan stage is a no-op: the source file
    # was copied into session_dir/plan.md earlier in main() (before the
    # --resume-from precondition check), or plan.md already existed on a
    # resume and the snapshot is authoritative per the spec's
    # rescue-determinism rule.
    if args.plan_path:
        if plan_copied_from_source:
            print(f"==> [1/4] plan supplied via --plan (copied) -> {plan_file}", flush=True)
        else:
            print(f"==> [1/4] plan reused from snapshot -> {plan_file}", flush=True)
    elif start_idx <= VALID_RESUME_STAGES.index("plan"):
        set_stage("plan")
        print(f"==> [1/4] planning (model: {args.plan_model}) -> {plan_file}", flush=True)
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
        run_claude(args.plan_model, plan_prompt, "plan", session_dir / "stream-plan.jsonl")
        if not plan_file.exists() or plan_file.stat().st_size == 0:
            die(f"plan stage did not produce {plan_file}")
    plan_text = plan_file.read_text(encoding="utf-8")

    # ----- implement -----
    if start_idx <= VALID_RESUME_STAGES.index("implement"):
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
    # A resumed review-code re-runs the whole triple reviewer fan-out;
    # partial sub_stage state and partial review files from a prior
    # half-finished run are overwritten.
    if start_idx <= VALID_RESUME_STAGES.index("review-code"):
        set_stage("review-code")
        print(
            f"==> [3/4] reviewing code in parallel "
            f"(models: {args.holistic}, {args.detail}, {args.scope})",
            flush=True,
        )
        review_code_a, review_code_b, review_code_c = run_review_code_stage(
            session_dir=session_dir,
            plan_text=plan_text,
            holistic=args.holistic,
            detail=args.detail,
            scope=args.scope,
            is_git=is_git,
        )
        print(f"    holistic code review ({args.holistic}): {review_code_a}", flush=True)
        print(f"    detail code review   ({args.detail}): {review_code_b}", flush=True)
        print(f"    scope code review    ({args.scope}): {review_code_c}", flush=True)

    # ----- address-code -----
    if start_idx <= VALID_RESUME_STAGES.index("address-code"):
        set_stage("address-code")
        print(f"==> [4/4] addressing code reviews (model: {args.address})", flush=True)
        run_address_code_stage(
            session_dir=session_dir,
            address_model=args.address,
            is_git=is_git,
        )

    print("", flush=True)
    print(f"done. session artifacts in: {session_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
