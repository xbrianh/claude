# Pipeline extraction — Phase 0 design doc

This is the inventory + module-layout proposal that gates Phase 1 of the
pipeline extraction. No code moves in this PR; the goal is to lock the shape
of `pipeline/` before any orchestrator is rewired through it.

The three orchestrators in scope:

- `skills/localgremlin/localgremlin.py` (~370 lines) + `skills/localgremlin/_core.py` (~710 lines)
- `skills/ghgremlin/ghgremlin.sh` (~560 lines of Bash)
- `skills/bossgremlin/bossgremlin.py` (~630 lines)

Plus the supporting files: `skills/_bg/{launch,finish,liveness,session-summary,set-stage,set-bail}.sh`, `skills/handoff/handoff.py`, and `skills/localgremlin/{localreview,localaddress}.py` (entry points around `_core.py`).

## 1. Inventory of shared concepts

Every entry below names the location(s) of the current implementation so the
Phase 1 PR can move (not rewrite) the logic.

### 1.1 `claude -p` invocation wrapper

The single most-duplicated primitive. All three orchestrators shell out to
`claude -p` with the same default flags but tee/parse the output differently.

- `skills/localgremlin/_core.py:run_claude` — Python `Popen`, tracks the
  child for signal-based reaping, streams stdout through `log_stream`.
- `skills/localgremlin/_core.py:CLAUDE_FLAGS` — `["--permission-mode", "bypassPermissions", "--output-format", "stream-json", "--verbose"]`.
- `skills/ghgremlin/ghgremlin.sh:CLAUDE_FLAGS` — same flags, in Bash.
- `skills/ghgremlin/ghgremlin.sh:progress_tee` — `tee >(jq ...)` that
  prints a human-readable progress trace to stderr.
- `skills/handoff/handoff.py:CLAUDE_FLAGS` — uses `--output-format text`
  and a single blocking `subprocess.run(..., timeout=...)` (no streaming,
  no child tracking).

### 1.2 Stream-JSON logger

Two parallel implementations of "decode each stream-json event into a
human-readable trace line":

- `skills/localgremlin/_core.py:_emit_event` + `log_stream` — Python.
  Handles `system/init`, `assistant/{text,thinking,tool_use}`,
  `user/tool_result`, `result`. Truncates each line to 200 chars.
- `skills/ghgremlin/ghgremlin.sh:progress_tee` — same event shapes, jq
  expression. Same 200-char truncation.

These need to converge on one Python implementation that the bash side
shells out to (or that the bash side is replaced by, depending on the
launcher decision below).

### 1.3 Stage runner / `--resume-from`

All three gremlins implement a "stage list with resume index" pattern, but
each one re-derives it.

- `skills/localgremlin/localgremlin.py:VALID_RESUME_STAGES` — Python list
  + `start_idx = VALID_RESUME_STAGES.index(args.resume_from)`. Each stage
  guarded by `if start_idx <= VALID_RESUME_STAGES.index("<stage>")`.
- `skills/ghgremlin/ghgremlin.sh:STAGES` — Bash array + `_stage_idx` /
  `run_stage` helpers. Uses `if run_stage <stage>; then ... fi` guards.
- `skills/bossgremlin/bossgremlin.py` — no list; the boss is a state
  machine driven by `boss_state.json` (`current_child_id`,
  `handoff_count`). The `--resume-from` flag is accepted from `launch.sh`
  but ignored: the boss resumes from `boss_state.json`.

### 1.4 State directory + artifact layout

Both managed by `_bg/launch.sh`, then read/written by every gremlin.

- Root: `${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins/<GR_ID>/`.
- `state.json` — schema fields: `id`, `kind`, `project_root`, `workdir`,
  `setup_kind`, `branch`, `status` (running|done|stopped),
  `started_at`, `ended_at`, `instructions` (200-char display summary),
  `description`, `description_explicit`, `parent_id`, `pipeline_args` (JSON
  array, used by `--resume`), `stage`, `sub_stage`, `stage_updated_at`,
  `pid`, `exit_code`, `bail_class`, `bail_detail`, `bail_reason`,
  `rescue_count`, `rescued_at`, `resumed_from_stage`, `issue_url` /
  `issue_num` / `pr_url` (gh-only), `model`.
- `instructions.txt` — sidecar (full untruncated instructions, for resume).
- `pid` — backgrounded gremlin's pid (also written into `state.json.pid`).
- `log` — combined stdout+stderr.
- `finished` / `closed` / `summarized` — terminal markers consumed by
  `liveness.sh` and `session-summary.sh`.
- `artifacts/` — `plan.md`, `spec.md`, `review-code-{holistic,detail,scope}-<model>.md`,
  `stream-{plan,implement,review-code-*,address}.jsonl`,
  `ghplan-out.jsonl`. Bossgremlin adds `boss_state.json`,
  `handoff-NNN.md`, `handoff-NNN-child.md`, `handoff-NNN.state.json`.
- Direct invocation (no `GR_ID`): `$STATE_ROOT/direct/<ts>-<rand>/artifacts/`
  — see `skills/localgremlin/_core.py:resolve_session_dir` and the
  matching prune at the bottom of `session-summary.sh`.

### 1.5 `set_stage` + `emit_bail` bookkeeping

- Helpers: `skills/_bg/set-stage.sh`, `skills/_bg/set-bail.sh`. Atomic
  `jq` writes; silent no-op on any failure (stage bookkeeping must never
  break the running gremlin).
- Python wrappers: `skills/localgremlin/_core.py:set_stage` and
  `emit_bail` — fork the helper script via `subprocess.run`, swallow
  errors, no-op when `GR_ID` is not set.
- Bash wrapper: `skills/ghgremlin/ghgremlin.sh:set_stage` (inline 5-line
  function), plus `patch_state` (generic jq filter applier) and
  `check_bail` (read `.bail_class` after each stage and die if set).
- `bail_class` vocabulary (set-bail.sh header):
  `reviewer_requested_changes`, `security`, `secrets`, `other`. Headless
  rescue refuses the first three.
- Sub-stage shape: review-code emits a `{holistic, detail, scope}` dict of
  `"running (model)"` → `"done (model)"`.

### 1.6 Plan / spec loading and snapshotting

Three distinct sources, one canonical destination (`session_dir/plan.md`).

- `localgremlin --plan <path>`: copy file → `session_dir/plan.md` once at
  fresh launch (`localgremlin.py:160-167`). Resume reads the snapshot.
- `ghgremlin --plan <local-file>`: copy to `plan.md` and post the file as
  a new GitHub issue, then record `issue_url`/`issue_num` in `state.json`
  (`ghgremlin.sh:202-225`).
- `ghgremlin --plan <issue-ref>`: parse `42` / `#42` /
  `owner/repo#42` / `https://github.com/owner/repo/issues/42`, fetch via
  `gh issue view`, snapshot body to `plan.md` (`ghgremlin.sh:226-265`).
- `ghgremlin` (no `--plan`): run `/ghplan` and extract the issue URL via
  `extract_gh_url` from the stream-json (`ghgremlin.sh:366-407`).
- `bossgremlin --plan <spec-path>`: north-star spec stored as
  `boss_state.spec_path`; rolling plans (`handoff-NNN.md`) written by
  the handoff agent.
- First positional file → copied to `STATE_DIR/artifacts/spec.md` by
  `launch.sh:434-475` (used by `/design <spec>` → gremlin handoff).
- Plan loading is duplicated in two places for the `pragmatic-developer.md`
  `## Core Principles` section: `localgremlin.py:171-186` (Python loop)
  and `ghgremlin.sh:411-416` (`awk` script).

### 1.7 Resume-precondition checks

Each pipeline encodes "what must exist on disk to resume from stage X" by
hand:

- `localgremlin.py:189-247` — implement requires `plan.md`; review-code
  requires plan + impl evidence (git: dirty tree or any HEAD history;
  non-git: any non-metadata file in the worktree); address-code requires
  plan + impl + all three review files.
- `ghgremlin.sh:97-111` — only enforces stage-name validity and special-
  cases `commit-pr` to rewind to `implement` (because `IMPL_SESSION` is
  not persisted). Per-stage rehydration is inline at each stage's
  `else` branch (e.g. `ghgremlin.sh:397-407` for resuming past plan).
- `bossgremlin.py` — preconditions are implicit: existence of
  `boss_state.json` means resume; otherwise fresh.

### 1.8 Handoff invocation contract

Single producer (boss) → single consumer (handoff.py). Stable wire format:

- CLI: `handoff.py --plan <current> [--spec <spec>] --out <next-plan> --base <chain-base-ref> --model <model> --timeout <secs> [--rev <ref>]`.
- Outputs: rolling plan file at `--out`, signal file at `<out>.state.json`,
  child plan at `<out-stem>-child<out-suffix>` (only on `next-plan`).
- Signal JSON: `{"exit_state": "next-plan|chain-done|bail", "child_plan": "<path>|null", "reason": "<bail reason>|null"}`.
- Exit codes: 0 = success (read signal file to distinguish outcomes),
  1 = infrastructure failure.
- Caller side: `skills/bossgremlin/bossgremlin.py:run_handoff` — lives
  alongside the per-handoff `git fetch origin <target_branch>` refresh
  and the `--spec` forwarding gate (only forward once `current_plan` has
  diverged from `spec_path`).

### 1.9 Branch + worktree management

- `skills/_bg/launch.sh:482-500` — three setup kinds:
  - `worktree-branch` (localgremlin in git): `git worktree add -b bg/localgremlin/<GR_ID> <wd> HEAD`.
  - `worktree` (ghgremlin / bossgremlin in git): `git worktree add --detach <wd> HEAD`.
  - `copy` (any kind, non-git): `cp -a <project_root>/. <wd>/`.
- `skills/_bg/finish.sh:57-63` — on success only, `git worktree remove --force <wd> && git worktree prune`. Bossgremlin is exempt (its detached-HEAD squash chain must survive until `land`).
- `skills/handoff/handoff.py:collect_git_context` — `git log` /
  `git diff` between `merge-base(--rev or HEAD, --base)` and the inspect
  ref. The `--rev` knob lets the boss inspect `origin/<target_branch>`
  from its own frozen worktree.
- Branch naming convention: `bg/localgremlin/<GR_ID>` for local;
  `issue-N-<slug>` (chosen by the implement agent itself) for gh.

### 1.10 Land semantics

Lives in `skills/gremlins/gremlins.py` (out of scope for this read), but
the contract that the orchestrators depend on:

- `gremlins land <local-id>` — squash-merge the worktree branch onto the
  user's checked-out branch.
- `gremlins land <gh-id>` — `gh pr merge`.
- `gremlins land <boss-id>` — fast-forward / squash the boss's
  accumulated detached-HEAD chain.

`bossgremlin.py:land_child` invokes `gremlins.py land <child-id>` between
each child and the next handoff. A failed land halts the chain (a rescue
of the *pipeline* can't fix a merge conflict or branch protection).

### 1.11 Other shared bits

These are smaller but worth one home:

- `MODEL_RE` / `GR_ID_RE` validation (`_core.py:31-32`).
- `slugify()` for ID + branch generation (`launch.sh:25-42`).
- `extract_gh_url` for pulling URLs out of stream-json by matching
  `gh issue create` / `gh pr create` tool calls (`ghgremlin.sh:305-352`) —
  re-usable for any future tool-result URL extraction.
- `extract_session_id` for `claude -p --resume <session_id>` chaining
  across stages within the same gremlin invocation
  (`ghgremlin.sh:358-364`).
- Signal-handler pattern + child-process registry: `_track`, `_untrack`,
  `_reap_all`, `install_signal_handlers` in `_core.py:51-98`. The bash
  side relies on `nohup` + subshell; the equivalent for child-reaping is
  implicit (process-group SIGTERM via `kill -- -$$`).
- Liveness classifier: `skills/_bg/liveness.sh:liveness_of_state_file` —
  emits `running` / `dead:<reason>` / `stalled:<reason>`. Sourced by
  both `session-summary.sh` and `gremlins.py`. Inline fallback in
  `session-summary.sh:40-59` for partial-sync windows.

## 2. Proposed `pipeline/` module layout

The strawman from the rolling plan, refined against the inventory above. The
intent is to give each orchestrator (Python or Bash → ported) a single
import surface and to keep the data structures (state.json, session dir
layout) accessible without duplicating helpers.

```
pipeline/
  __init__.py
  client.py          # ClaudeClient interface, RealClaudeClient, FakeClaudeClient
  stream.py          # log_stream, _emit_event, raw stream-json → human trace
  state.py           # state.json read/write/patch; set_stage / emit_bail re-impls
  session.py         # session_dir resolution, artifact paths, instructions sidecar
  snapshot.py        # plan.md / spec.md snapshotting, source-vs-snapshot rules
  stages.py          # Stage, Sequencer with --resume-from logic
  resume.py          # precondition predicates (plan exists, impl evidence, ...)
  signals.py         # install_signal_handlers, child registry, _reap_all
  reviewers.py       # ReviewWorker, run_triple_review fan-out
  worktree.py        # add/remove worktree, branch naming, copy fallback
  liveness.py        # state-file → running/dead/stalled classification
  finish.py          # finished marker + state.json terminal patch
  ghutil.py          # extract_gh_url, extract_session_id, gh issue/pr helpers
  git.py             # in_git_repo, git_head, collect_git_context
  prompts/
    __init__.py
    pragmatic.py     # parse `## Core Principles` section once
    plan.py          # build_plan_prompt
    impl.py          # build_impl_prompt
    review.py        # build_review_prompt, build_address_prompt
    handoff.py       # build_handoff_prompt
    lenses/          # holistic / detail / scope .md files (see §4.2)
```

Mapping from current code:

| Module | Pulls from |
| --- | --- |
| `client.py` | `_core.py:run_claude`, `_core.py:CLAUDE_FLAGS`, plus a thin Bash shim for `ghgremlin.sh` to keep using during the transition |
| `stream.py` | `_core.py:_emit_event` + `log_stream`; replaces `progress_tee` jq blob |
| `state.py` | `_bg/set-stage.sh` + `_bg/set-bail.sh` (Python), `_core.py:{set_stage, emit_bail}`, `ghgremlin.sh:patch_state` + `check_bail` |
| `session.py` | `_core.py:resolve_session_dir`, parts of `launch.sh:464-475` (artifact dir creation + spec.md copy) |
| `snapshot.py` | `localgremlin.py:160-167`, `ghgremlin.sh:184-281` (plan source resolution branches) |
| `stages.py` | `localgremlin.py:VALID_RESUME_STAGES` + per-stage guards, `ghgremlin.sh:STAGES` + `run_stage` |
| `resume.py` | `localgremlin.py:189-247`, `ghgremlin.sh:397-407, 480-486` |
| `signals.py` | `_core.py:51-98` |
| `reviewers.py` | `_core.py:ReviewWorker`, `run_triple_review`, `run_review_code_stage` |
| `worktree.py` | `launch.sh:482-500`, `finish.sh:57-63` |
| `liveness.py` | `_bg/liveness.sh` (Python re-impl); the bash file stays as a thin shim sourced by `session-summary.sh` |
| `finish.py` | `_bg/finish.sh` (Python re-impl); same shim story |
| `ghutil.py` | `ghgremlin.sh:extract_gh_url`, `extract_session_id`; gh issue / pr / review wrappers |
| `git.py` | `_core.py:{in_git_repo, git_head}`, `handoff.py:collect_git_context` |
| `prompts/pragmatic.py` | `localgremlin.py:171-186`, `ghgremlin.sh:411-416` |
| `prompts/{plan,impl,review,handoff}.py` | inline f-strings in current orchestrators |

What is *not* in `pipeline/`:

- `gremlins.py` (`/gremlins`) stays where it is. It owns commands, not
  pipeline primitives, and only needs to import `pipeline.state`,
  `pipeline.liveness`, and `pipeline.worktree`.
- The skill `SKILL.md` files stay where they are. They're triggers, not
  pipeline code.
- The lens prose files — see §4.2 below.

## 3. `ClaudeClient` interface

The interface every orchestrator should call instead of `subprocess.Popen(["claude", ...])`. This is the seam that makes the pipeline testable: the real client owns child-process tracking and signal-aware reaping; the fake replays canned stream-json from a fixture directory so tests don't pay the cost of a live `claude -p` per stage.

```python
# pipeline/client.py
from __future__ import annotations
import pathlib
from typing import Protocol


DEFAULT_FLAGS: tuple[str, ...] = (
    "--permission-mode", "bypassPermissions",
    "--output-format", "stream-json",
    "--verbose",
)


class ClaudeClient(Protocol):
    """Minimum surface every orchestrator uses to invoke `claude -p`.

    Implementations stream `claude -p` stdout to `raw_path` (raw bytes,
    one stream-json object per line) and emit a human-readable trace
    keyed by `label` to stderr via `pipeline.stream.log_stream`. Raises
    `RuntimeError` on non-zero exit; never returns a non-zero status.
    """

    def run(
        self,
        model: str,
        prompt: str,
        *,
        label: str,
        raw_path: pathlib.Path,
        output_format: str = "stream-json",
        flags: tuple[str, ...] = DEFAULT_FLAGS,
    ) -> None: ...
```

### 3.1 `RealClaudeClient`

- Spawns `claude -p --model <model> <flags...> <prompt>` via `subprocess.Popen`.
- Registers the `Popen` object with `pipeline.signals` on spawn; unregisters
  on `wait()`. The signal handler calls `_reap_all()` on
  SIGINT/SIGTERM, terminating every live child within 2s and SIGKILLing
  stragglers. This is the existing `_core.py:run_claude` behavior verbatim.
- `output_format="text"` skips the streaming path and shells out as a
  blocking `subprocess.run(..., timeout=...)` (the handoff.py shape).
  `raw_path` is still written, but as a single concatenated blob.

### 3.2 `FakeClaudeClient`

- Constructor: `FakeClaudeClient(fixture_dir: pathlib.Path, *, missing="raise")`.
- On `.run(...)`, looks up `<fixture_dir>/<label>.jsonl` (or a name
  derived from `label + model`) and either:
  - replays it line-by-line through `pipeline.stream.log_stream` exactly
    as the real client would, then "exits 0";
  - if the fixture is missing, raises (default) or returns silently
    (`missing="skip"`, useful for asserting "this stage was reached").
- Records every `.run(...)` call into `self.calls: list[CallRecord]` so
  tests can assert the model, prompt, label, and order of stages.
- Optional `outcome_overrides: dict[str, int]` lets a test force a
  specific stage to "exit non-zero" without authoring a failure-shaped
  fixture.

The fake is intentionally narrow: it does not simulate child processes,
file watches, or the launcher. Tests that need the pipeline to actually
write `plan.md` should provide a fixture whose stream-json includes the
appropriate `Write` tool call — exactly mirroring real-`claude` behavior.

## 4. Open questions — answers / deferrals

### 4.1 `pipeline/` placement: repo root vs `scripts/`

**Decision: repo root.**

`scripts/` today holds one-shot tooling: `sync.sh`, `e2e-bossgremlin.sh`,
`e2e/` test scaffolding. Burying a Python package under it would mix
"things you run" with "things other code imports" and force everything
that imports from the pipeline (the three orchestrators, plus eventually
`gremlins.py`) to either tweak `sys.path` or use awkward dotted names
like `scripts.pipeline.client`.

At the repo root, `pipeline/` sits next to `skills/` and `agents/` —
which are the natural peers — and the orchestrators can do
`from pipeline.client import RealClaudeClient` after a
`sys.path.insert(0, repo_root)` in their entry-point shim. Sync.sh's
`DIR_PAIRS` gets one new entry: `pipeline/` ↔ `~/.claude/pipeline/`.

### 4.2 Lens prompt files — `pipeline/prompts/lenses/` or stay with the skill?

**Decision: move to `pipeline/prompts/lenses/`.**

Today the three lens files live under `skills/localgremlin/`, but
`skills/ghgremlin/ghgremlin.sh:504-505` already reaches across the skill
boundary to read `../localgremlin/lens-scope-code.md`. That cross-skill
reach is a smell, and it'll only get worse as more reviewers want
shared prose. Putting the lenses under `pipeline/prompts/lenses/` makes
ownership explicit (they're shared review prompts, not localgremlin's),
removes the cross-skill path, and gives `pipeline.prompts.review` a
canonical place to load them from.

The `_core.py:load_lenses` function already encapsulates the load — the
move is a one-line path change there plus a `git mv` of three files.

### 4.3 `_bg/launch.sh` Python reimplementation — Phase 5 or follow-up?

**Decision: defer to a follow-up (out of scope for the initial
extraction).**

`launch.sh` is 586 lines, but most of that is argv-walking and slug
construction — code that's stable and not currently duplicated across
the three orchestrators. The pieces that *do* need extraction
(state.json schema, GR_ID env var, worktree setup, `finish.sh` hook)
will already be present in `pipeline/{state,session,worktree,finish}.py`
once Phase 1–4 land. Reimplementing the launcher in Python at the same
time would balloon the diff and force a second migration (every
caller of `launch.sh` — `bossgremlin.py:launch_child`, the SKILL.md
wrappers, `gremlins.py rescue`) to switch in lockstep.

The cleaner ordering: extract orchestrator primitives into `pipeline/`
first; let `launch.sh` keep calling `set-stage.sh` / `set-bail.sh` (or
their Python equivalents through a thin bash wrapper) during the
transition; then schedule the launcher port as its own focused PR once
the Python surface is proven. The existing bash launcher is well-tested
and not on fire — we can afford to leave it alone until the rest of the
ground is under us.

## 5. Decisions that need explicit sign-off

In priority order:

1. **`pipeline/` at repo root** (§4.1). Affects `sync.sh` `DIR_PAIRS`
   and every orchestrator's import path. No code-shape implications, but
   it is the first thing Phase 1 does, so it should be locked first.
2. **Lens prompts move to `pipeline/prompts/lenses/`** (§4.2). Removes
   one cross-skill reach in `ghgremlin.sh`; otherwise low-risk.
3. **`launch.sh` stays in Bash for now** (§4.3). Defers ~600 lines of
   migration to a follow-up; the rest of `pipeline/` is unaffected
   either way.
4. **`ClaudeClient` interface as defined in §3**. The seam every
   orchestrator will route through; locking the signature now keeps
   Phase 2+ from re-litigating it.
5. **Module boundaries in §2**. Specifically: `state.py` swallows
   set-stage / set-bail / patch-state; `signals.py` owns the child
   registry; `prompts/` is its own subpackage. Worth a quick "yes that's
   how I'd carve it" before Phase 1 starts moving code into the wrong
   files.
