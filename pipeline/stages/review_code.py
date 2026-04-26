"""Triple-lens parallel code review stage.

Three reviewer threads each spawn a ``claude -p`` via the injected client,
running concurrently. ``set_stage`` updates the gremlin's sub_stage as each
thread finishes so ``/gremlins`` shows progress in real time.
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time
from typing import Optional, Tuple

from ..clients.claude import ClaudeClient
from ..state import emit_bail, set_stage

LENSES_DIR = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "lenses"


def load_lenses() -> Tuple[str, str, str]:
    """Return (holistic, detail, scope) lens prose. Raises if any is missing
    or empty — the gremlin can't review without all three lenses."""
    files = {
        "holistic": LENSES_DIR / "holistic.md",
        "detail": LENSES_DIR / "detail.md",
        "scope": LENSES_DIR / "scope.md",
    }
    for path in files.values():
        if not path.exists() or path.stat().st_size == 0:
            raise FileNotFoundError(f"missing or empty lens file: {path}")
    # Explicit utf-8 — lens files contain em-dashes and other non-ASCII, so
    # relying on the process default encoding would crash under a non-UTF-8
    # locale (e.g. a minimal container with LANG=C).
    return (
        files["holistic"].read_text(encoding="utf-8"),
        files["detail"].read_text(encoding="utf-8"),
        files["scope"].read_text(encoding="utf-8"),
    )


def run_review(
    *,
    client: ClaudeClient,
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
    client.run(prompt, label=label, model=model, raw_path=raw_path)


class ReviewWorker(threading.Thread):
    """Runs one lens's reviewer in its own thread. Exceptions are captured
    on ``self.error`` so the main thread can decide how to fail."""

    def __init__(
        self,
        *,
        client: ClaudeClient,
        model: str,
        out_file: pathlib.Path,
        focus: str,
        context: str,
        where_field: str,
        label: str,
        raw_path: pathlib.Path,
    ) -> None:
        super().__init__(daemon=True)
        self.client = client
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
                client=self.client,
                model=self.model,
                out_file=self.out_file,
                focus=self.focus,
                context=self.context,
                where_field=self.where_field,
                label=self.label,
                raw_path=self.raw_path,
            )
        except Exception as e:  # noqa: BLE001
            self.error = e
            sys.stderr.write(f"review {self.model} failed: {e}\n")
            sys.stderr.flush()


def run_triple_review(
    *,
    client: ClaudeClient,
    context: str,
    focuses: Tuple[str, str, str],
    out_files: Tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    models: Tuple[str, str, str],
    where_field: str,
    session_dir: pathlib.Path,
) -> None:
    """Spawn three reviewer threads, emit sub_stage updates as each finishes,
    and raise if any worker failed or produced an empty output file."""
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
            client=client, model=model_a, out_file=out_a, focus=focus_a,
            context=context, where_field=where_field,
            label=f"review-code:{lens_keys[0]}:{model_a}",
            raw_path=session_dir / f"stream-review-code-{lens_keys[0]}-{model_a}.jsonl",
        ),
        ReviewWorker(
            client=client, model=model_b, out_file=out_b, focus=focus_b,
            context=context, where_field=where_field,
            label=f"review-code:{lens_keys[1]}:{model_b}",
            raw_path=session_dir / f"stream-review-code-{lens_keys[1]}-{model_b}.jsonl",
        ),
        ReviewWorker(
            client=client, model=model_c, out_file=out_c, focus=focus_c,
            context=context, where_field=where_field,
            label=f"review-code:{lens_keys[2]}:{model_c}",
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
        raise RuntimeError("one or more reviews failed")
    for w, out in zip(workers, out_files):
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"review {w.model} did not produce {out}")


def run_review_code_stage(
    *,
    client: ClaudeClient,
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
    # file, etc.) records bail_class=other before the exception propagates.
    # Headless rescue can attempt the `other` class — but at least the bail
    # field tells callers *something* failed during review-code rather than
    # leaving them to grep the log.
    try:
        run_triple_review(
            client=client,
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
