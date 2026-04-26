# Pipeline package design

Phase 0 deliverable. This document is the binding specification for Phases
1–5 of the gremlin-pipeline migration: the work is to extract the shared
plan / implement / review / address logic out of the three orchestrators
(`skills/localgremlin/`, `skills/ghgremlin/`, `skills/bossgremlin/`) into
a top-level Python package, with one `ClaudeClient` seam so the stages
become unit-testable without spawning real `claude -p` subprocesses.

No code changes in Phase 0. Phases 1–5 land the moves incrementally.

## Why a package

Today the three orchestrators duplicate the same operational concerns
(stage sequencing, resume preconditions, child-process tracking, signal
handling, stage/bail bookkeeping, state-dir layout, raw-jsonl logging) in
two languages. `localgremlin.py`/`_core.py` and `ghgremlin.sh` reimplement
the same trace-printing logic in Python and `jq` respectively
(`_emit_event` vs the inline `progress_tee` jq filter). `bossgremlin.py`
reimplements stage bookkeeping a third time (`set_stage` shells out to
`set-stage.sh`). Every change to a shared concern requires editing all
three.

The pipeline package consolidates the shared behavior behind a single
import surface so each orchestrator becomes a thin wrapper.

## Where it lives

`pipeline/` at the repo root, parallel to `skills/`, `agents/`,
`commands/`. Resolved from the open question in the upstream spec: the
package is source code that the skills _depend on_, not a skill itself,
so it doesn't belong inside `skills/`.

Constraint imposed by the sync mechanism: at runtime, gremlins run from
worktrees of arbitrary repos and import via `python -m pipeline.cli`,
which needs `pipeline/` on `sys.path`. The agreed sticking point is
`PYTHONPATH=$HOME/.claude` — already implicitly present today because
`launch.sh` resolves stage scripts under `$HOME/.claude/skills/<kind>/`.
For that to find `pipeline/`, the package must be mirrored to
`~/.claude/pipeline/`. **Phase 5** extends `scripts/sync.sh` `DIR_PAIRS`
with `pipeline:$CLAUDE_DIR/pipeline` so `pipeline/` syncs alongside
`skills/`, `agents/`, `commands/`.

Until Phase 5 lands, the orchestrators continue to dispatch the way they
do today; phases 1–4 either ship in a single-PR pair with the sync
update (preferred) or stage the dispatch flip behind a feature check.

## Module layout

```
pipeline/
  DESIGN.md                       # this file (Phase 0)
  pyproject.toml                  # project metadata, pytest config (Phase 1)
  __init__.py
  cli.py                          # `python -m pipeline.cli {local,gh,boss}` dispatch
  runner.py                       # generic stage runner: sequencing, resume, signals, child tracking
  state.py                        # session-dir resolution, set_stage / emit_bail wrappers
  git.py                          # in_git_repo / git_head / dirty / branch & worktree helpers
  clients/
    __init__.py
    claude.py                     # ClaudeClient protocol + real subprocess implementation
    fake.py                       # recording test double (replays canned stream-json)
  stages/
    __init__.py
    plan.py                       # local plan stage (writes session_dir/plan.md via claude -p)
    implement.py                  # local + gh implement stage (claude -p; gh extracts session_id)
    review_code.py                # triple-lens fan-out (currently _core.run_review_code_stage)
    address_code.py               # currently _core.run_address_code_stage
    commit_pr.py                  # ghgremlin commit-pr stage incl. impl-handoff branch lifecycle
    ghreview.py                   # ghgremlin /ghreview + scope-reviewer parallel stage
    ghaddress.py                  # ghgremlin /ghaddress stage
    wait_copilot.py               # ghgremlin Copilot-review polling stage
  orchestrators/
    __init__.py
    local.py                      # ports skills/localgremlin/localgremlin.py main()
    gh.py                         # ports skills/ghgremlin/ghgremlin.sh
    boss.py                       # ports skills/bossgremlin/bossgremlin.py main()
  prompts/
    plan.md                       # currently embedded in localgremlin.py main()
    implement_local.md            # currently embedded in localgremlin.py main()
    implement_gh.md               # currently embedded in ghgremlin.sh stage 2a
    address_code.md               # currently embedded in _core.run_address_code_stage
    review_code.md                # currently inline in _core.run_review (header + structure)
    commit_pr_handoff.md          # currently inline in ghgremlin.sh stage 2b (handoff path)
    commit_pr_fresh.md            # currently inline in ghgremlin.sh stage 2b (fresh path)
    scope_review_pr.md            # currently inline in ghgremlin.sh stage 4 (scope reviewer)
    lenses/
      holistic.md                 # moves from skills/localgremlin/lens-holistic-code.md
      detail.md                   # moves from skills/localgremlin/lens-detail-code.md
      scope.md                    # moves from skills/localgremlin/lens-scope-code.md
  tests/
    __init__.py
    test_runner.py                # stage sequencing, resume preconditions, signal handlers
    test_state.py                 # set_stage / emit_bail no-op-without-GR_ID contract
    test_git.py                   # impl-handoff branch lifecycle
    test_stages_plan.py           # plan stage prompt + post-stage validation
    test_stages_review_code.py    # triple-lens fan-out, sub_stage progression
    test_stages_address_code.py   # glob discovery, model-name extraction
    test_stages_commit_pr.py      # ghgremlin handoff/fresh branching
    fixtures/
      stream_plan.jsonl           # canned stream-json for the plan stage
      stream_implement.jsonl      # canned stream-json for the implement stage
      ...
```

Three orchestrator entry points map onto the same `cli.py`:

```
$ python -m pipeline.cli local <args>   # was: skills/localgremlin/localgremlin.py
$ python -m pipeline.cli gh    <args>   # was: skills/ghgremlin/ghgremlin.sh
$ python -m pipeline.cli boss  <args>   # was: skills/bossgremlin/bossgremlin.py
```

Each subcommand instantiates the real `ClaudeClient`, builds the stage
list, and hands it to `runner.run`. Tests instantiate `FakeClaudeClient`
and assert on the recorded calls without any subprocess spawn.

## ClaudeClient interface

Derived from actual usage in `_core.run_claude` (`skills/localgremlin/_core.py:263`)
and the four `claude -p` invocation sites in `ghgremlin.sh`
(plan stage at `skills/ghgremlin/ghgremlin.sh:381-384`, title-generation
at `:209-212`, implement at `:434-444`, commit-pr at `:578-579`,
ghreview/scope at `:609-632`, ghaddress at `:664`).

Patterns the interface must support:

1. **Stream-json output, raw-jsonl tee, human trace to stderr.** The
   default `_core.run_claude` shape: spawn with
   `--permission-mode bypassPermissions --output-format stream-json --verbose`,
   read stdout line-by-line, write raw lines to a session-dir
   `.jsonl` file, emit a one-line-per-event human trace to stderr,
   raise on non-zero exit. Used by every stage today except the gh
   title-generation step.
2. **Text output.** ghgremlin's title-generation step
   (`ghgremlin.sh:209-212`) uses `--output-format text` and parses the
   single-line title out of stdout. The interface needs to surface the
   captured text body to the caller.
3. **Session resumption.** ghgremlin's commit-pr stage uses
   `--resume <session_id>` (`ghgremlin.sh:578-579`) so the same `claude`
   session that did the implementation also opens the PR. The session_id
   is extracted from the stream-json `system.init` event of the implement
   stage (`extract_session_id` at `ghgremlin.sh:358-364`).
4. **Optional `--model` flag.** Localgremlin always passes a model
   (its CLI defaults to `sonnet`); ghgremlin only passes one if
   `--model <name>` was supplied at launch (`ghgremlin.sh:288-289`).
5. **Post-hoc URL extraction from stream-json events.** ghgremlin's plan
   and commit-pr stages capture the full stream-json output to a string
   variable, then run a jq pipeline (`extract_gh_url` at
   `ghgremlin.sh:305-352`) over the events to find a `gh issue create` /
   `gh pr create` Bash tool_result and pull a URL out. The interface
   needs to either (a) collect events into a returned object so callers
   can post-process them, or (b) accept an event callback so callers
   register an extraction filter inline.
6. **Parallel invocation.** The triple-reviewer fan-out
   (`_core.run_triple_review`) and the ghreview/scope split
   (`ghgremlin.sh:609-636`) both spawn multiple `claude -p` subprocesses
   concurrently. The real client owns child-process tracking
   (the module-level `_children` list in `_core.py:51-88`) so `_reap_all`
   on SIGINT/SIGTERM kills every live child; the fake doesn't need to
   track because it never spawns.

The proposed signature:

```python
from typing import Protocol, Callable, Sequence
import pathlib

class CompletedRun:
    """Return value from ClaudeClient.run.

    Fields:
      exit_code     — int, always populated; non-zero raises before this
                      is constructed (kept for completeness/future use).
      session_id    — str | None; extracted from the stream-json
                      `system.init` event when present, else None
                      (e.g. text-mode runs and runs that crashed before
                      emitting init).
      text_result   — str | None; for output_format='text', the captured
                      stdout. None for stream-json runs.
      events        — list[dict] | None; populated only when
                      capture_events=True. Each entry is one parsed
                      stream-json event (system.init, assistant,
                      user.tool_result, result, …).
    """

class ClaudeClient(Protocol):
    def run(
        self,
        prompt: str,
        *,
        label: str,
        model: str | None = None,
        raw_path: pathlib.Path | None = None,
        output_format: str = "stream-json",  # or "text"
        resume_session: str | None = None,
        extra_flags: Sequence[str] = (),
        capture_events: bool = False,
        on_event: Callable[[dict], None] | None = None,
    ) -> CompletedRun:
        """Spawn `claude -p` with the configured flags. Tee raw stream-json
        to `raw_path` if given. Emit a human-readable per-event trace to
        stderr (the contract `_emit_event`/`progress_tee` provide today).
        Always extract `session_id` from `system.init` if present.

        Raise RuntimeError on non-zero exit. Return CompletedRun with the
        outcome.

        Defaults match `_core.run_claude`: `--permission-mode
        bypassPermissions --output-format <output_format> --verbose`,
        plus `--model <model>` when supplied, plus `--resume
        <resume_session>` when supplied, plus extra_flags appended last.

        `label` is the per-stage identifier used to prefix human-trace
        lines (e.g. `[plan]`) and as a stable key in the raw_path
        filename when the stage spawns multiple parallel `claude -p`
        invocations (today: `stream-review-code-<lens>-<model>.jsonl`).
        """
```

The real implementation (`pipeline/clients/claude.py`):
- Uses `subprocess.Popen` with default bufsize so `readline()` reads in
  8 KiB chunks (preserves the throughput improvement noted in
  `_core.py:267-272`).
- Owns the module-level `_children` list and `_reap_all` for SIGINT/
  SIGTERM-driven cleanup. `runner.install_signal_handlers()` calls into
  the client to register the handler.
- Streams events through one logger (porting `_core._emit_event`) so the
  human trace shape stays identical for both local and gh chains —
  today the bash `progress_tee` jq filter and the Python `_emit_event`
  printer differ subtly (e.g. line prefixes, the trailing-newline
  handling on `gsub("\n"; " ")`); the migration unifies them.
- Optionally accumulates parsed events into a list when
  `capture_events=True`, so URL-extraction stages don't have to re-parse
  the raw-jsonl file from disk after the fact.

The fake (`pipeline/clients/fake.py`):
- Records every `run(...)` call as a `RecordedCall` (prompt, model,
  flags, label, raw_path, resume_session) into a list the test asserts
  on.
- Replays canned events from a fixture file in `tests/fixtures/` based
  on the call's `label` (e.g. `label="plan"` → replay
  `fixtures/stream_plan.jsonl`).
- Writes the canned events to `raw_path` if given, so any post-stage
  code that reads that file (the implement stage's empty-output check,
  the address stage's review-file glob) sees realistic on-disk shape.
- Returns a CompletedRun with the canned `session_id` and (when
  `capture_events=True`) the canned event list.

## launch.sh --resume contract

Today, both the fresh-launch path and the `--resume` path in
`skills/_bg/launch.sh` resolve a per-kind script:

```bash
PIPELINE=""
for ext in py sh; do
    candidate="$HOME/.claude/skills/$KIND/$KIND.$ext"
    if [[ -x "$candidate" ]]; then PIPELINE="$candidate"; break; fi
done
```

(fresh-launch: `_bg/launch.sh:281-286`; resume: `_bg/launch.sh:140-145`)

After Phase 1 (local), Phase 3 (gh), Phase 4 (boss), each kind's
dispatch line replaces the per-kind script with a `pipeline.cli` module
invocation. The exact change applies to **both dispatch paths**
(fresh-launch and resume), and the spawned-bash command shape preserves
argv forwarding identically:

**Before** (fresh, `_bg/launch.sh:597-598`):
```bash
nohup bash -c '"$PIPELINE" "$@"; EC=$?; "$HOME/.claude/skills/_bg/finish.sh" "$GR_ID" "$EC"' \
    -- "$@" </dev/null >"$STATE_DIR/log" 2>&1 &
```

**After**:
```bash
PYTHONPATH="$HOME/.claude${PYTHONPATH:+:$PYTHONPATH}" \
nohup bash -c 'python3 -m pipeline.cli "$@"; EC=$?; "$HOME/.claude/skills/_bg/finish.sh" "$GR_ID" "$EC"' \
    -- "$KIND_SUBCOMMAND" "$@" </dev/null >"$STATE_DIR/log" 2>&1 &
```

where `$KIND_SUBCOMMAND` is `local`, `gh`, or `boss` (mapped from
`$KIND` once at the top of launch.sh, since `localgremlin` →
`pipeline.cli local`, etc).

The same change applies to the `--resume` branches at
`_bg/launch.sh:230-240`, including the `_has_plan` flag-detection
heuristic — that branch already conditionally appends or omits the
trailing `$INSTRUCTIONS` positional, which the migrated code keeps
unchanged.

**Argv forwarding.** Pipeline-level flags persisted in
`state.json.pipeline_args` (e.g. localgremlin's `-a -b -c -i -p -x` model
selectors, ghgremlin's `-r <ref> --plan <path>`, bossgremlin's
`--chain-kind --plan --model`) are forwarded **unchanged** as positional
arguments after `$KIND_SUBCOMMAND`. `pipeline.cli` is responsible for
parsing them with the same argparse contract each orchestrator has
today; tests should verify that a `pipeline_args` array recorded by an
older launcher still parses correctly through `pipeline.cli` (the
schema is byte-stable).

**Resume invariants** (`--resume-from <stage>` is appended after
`pipeline_args` and before instructions, exactly as today). Stage names
must remain byte-identical: `plan`, `implement`, `review-code`,
`address-code` for local; `plan`, `implement`, `commit-pr`,
`request-copilot`, `ghreview`, `wait-copilot`, `ghaddress` for gh.
Boss has no `--resume-from` semantics on its own argv (it ignores the
flag and resumes from `boss_state.json` instead, per
`bossgremlin.py:469-471`); `pipeline.cli boss` must accept and ignore
the flag for launcher-compatibility.

**Per-phase rollout.** Each of phases 1, 3, 4 changes the dispatch for
exactly one kind and leaves the others unchanged. The `for ext in py
sh` loop becomes a per-kind case statement that points the migrated
kinds at `pipeline.cli` and the not-yet-migrated kinds at the old
script paths. After Phase 4 the loop is gone and all three kinds use
`pipeline.cli`.

## Rescue marker-protocol surface

The marker protocol is the contract between three components: the
upstream stage that bails, the headless rescue agent, and the boss
orchestrator that watches its children. Every name below is
**byte-stable across the migration** — these strings appear in
`state.json` files written by the old code that the new code must
continue to read.

### state.json fields written by the gremlin pipeline

| Field             | Writer                              | Consumers                                         |
|-------------------|-------------------------------------|---------------------------------------------------|
| `bail_class`      | upstream stage via `_bg/set-bail.sh` (or `_core.emit_bail`) | `gremlins.py` headless rescue, `liveness.sh`     |
| `bail_detail`     | upstream stage via `_bg/set-bail.sh` (or `_core.emit_bail`) | `gremlins.py` (preserved through marker)         |
| `bail_reason`     | `gremlins.py:_write_bail` (rescue path) | `liveness.sh`, `bossgremlin.py.get_child_bail_reason` |
| `stage`           | `_bg/set-stage.sh` (via `set_stage` in each pipeline) | `/gremlins`, `liveness.sh`, `session-summary.sh` |
| `sub_stage`       | `_bg/set-stage.sh` (review-code only) | `/gremlins`                                       |
| `stage_updated_at`| `_bg/set-stage.sh`                   | `liveness.sh` (stall heuristic)                   |

Note: `bail_class` and `bail_reason` are **distinct fields** —
upstream stages write `bail_class` via `set-bail.sh`; the headless
rescue agent reads `bail_class` to decide whether to attempt recovery
(`gremlins.py:54` defines `EXCLUDED_BAIL_CLASSES`), then writes
`bail_reason` via `_write_bail` to record the rescue outcome. Boss
prefers `bail_reason` over `bail_class`
(`bossgremlin.py:get_child_bail_reason` at `:410-418`), and
`liveness.sh` does likewise (`liveness.sh:50-57`). The migration must
preserve both writes and the precedence between them.

### Bail-class vocabulary (upstream stages → `state.json.bail_class`)

Source: `skills/_bg/set-bail.sh:10-17`. These four classes are the
entire vocabulary upstream stages write today:

- `reviewer_requested_changes` — code review flagged blocker findings.
  Written by /ghreview when the review concludes with blocker-severity
  findings, and by /ghaddress when it cannot proceed.
- `security` — review flagged security concern(s).
- `secrets` — change touches secrets/credentials.
- `other` — generic; pair with a useful `bail_detail`. Written by
  `_core.emit_bail` from the review-code and address-code stage
  wrappers (`_core.py:599`, `_core.py:708`) on infrastructure failure.

The first three are in `EXCLUDED_BAIL_CLASSES` (`gremlins.py:54`);
headless rescue refuses to run for them and writes a
`bail_reason="excluded_class:<class>"` instead.

### Rescue verdict vocabulary (Phase A agent marker file)

The headless rescue agent writes a JSON marker file that the rescue
wrapper reads. The `status` field is one of four values
(`gremlins.py:638`, `:672`):

- `fixed` — agent edited `state.json` or pipeline source so the bug is
  no longer present; rescue should proceed to Phase B (relaunch).
- `transient` — failure was a flake (network/tool timeout); rescue
  should proceed to Phase B without code changes.
- `structural` — agent identified a real bug in pipeline source or a
  sibling artifact (e.g. a child plan) that requires a human edit.
  Rescue refuses to relaunch; logs the agent's summary as
  `bail_reason="structural"`, `bail_detail=<summary>`.
- `unsalvageable` — agent declared the run unrecoverable. Same shape
  as `structural` but indicates "give up" rather than "fix and retry".

The agent's `summary` field is copied into `bail_detail` for
`structural` and `unsalvageable` outcomes so the boss can show it in
its log (`bossgremlin.py:_summarize_for_log` at `:432-447`).

### Marker-protocol bail reasons (Phase A failure modes)

When the Phase A agent does not produce a usable marker, the rescue
wrapper writes its own `bail_reason` describing why
(`gremlins.py:896-908`, `:953-988`). These reasons are byte-stable;
operators may grep state.json for them, and the boss treats them as
"rescue refused":

- `phase_a_no_marker` — agent finished without writing the marker file.
- `phase_a_bad_marker` — marker file exists but is malformed
  (unparseable JSON, or `status` not in the verdict vocabulary).
- `phase_a_claude_error` — `claude -p` itself returned non-zero.
- `phase_a_timeout` — agent did not finish within the configured
  timeout.

Plus two reasons written when rescue refuses upstream-classified bails
(`gremlins.py:785-794`):

- `excluded_class:<bail_class>` — upstream wrote one of the three
  excluded classes (`reviewer_requested_changes`, `security`,
  `secrets`); rescue won't touch it.
- `attempts_exhausted` — `rescue_count` exceeded the configured cap.

Plus two written for Phase B failures
(`gremlins.py:1010`, `:1024`, `:1031`):

- `phase_b_launcher_missing` — `_bg/launch.sh --resume` is not present
  or not executable.
- `phase_b_relaunch_failed` — `launch.sh --resume` returned non-zero.

The migration must preserve every name above. `pipeline/state.py` may
introduce typed enums for ergonomics inside the package, but the
`set-bail.sh`-equivalent string written into `state.json` must be the
exact byte sequence; tests should pin a round-trip from each enum
member to its on-disk string.

## ghgremlin impl-handoff branch lifecycle

The most intricate piece of bash logic in `ghgremlin.sh`: the
`ghgremlin-impl-handoff-$$` per-run branch. Lives in stage 2a (implement,
`ghgremlin.sh:418-532`) and stage 2b (commit-pr,
`ghgremlin.sh:534-595`). Phase 3 ports the whole lifecycle into
`pipeline/git.py` + `pipeline/stages/implement.py` +
`pipeline/stages/commit_pr.py`.

### Why the branch exists

The implement stage runs `claude -p` in a worktree that launch.sh
created at `origin/<default-branch>` with detached HEAD. The implement
prompt explicitly invites the agent to commit checkpoints during a
multi-step plan ("You may commit checkpoints as you go (recommended for
multi-step plans — a later stage handles branching and PR creation
regardless), but do not push to any remote", `ghgremlin.sh:443`). When
the agent does commit, those commits are reachable only through the
detached HEAD ref. Without a branch, the next `claude -p` invocation
(commit-pr stage) cannot easily reuse them — and `git worktree remove`
in `finish.sh` on success would make them unreachable.

The hand-off branch creates a stable named ref that survives the
implement-stage exit and feeds into commit-pr's renaming logic.

### Lifecycle

**Pre-implement state capture** (`ghgremlin.sh:422-423`):
```
PRE_HEAD=$(git rev-parse HEAD)
PRE_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || true)
```
PRE_BRANCH is empty under launch.sh's detached worktree. It's only
populated for direct (non-launch.sh) invocations from a named branch.

**Post-implement classification** (`ghgremlin.sh:456-467`):
```
POST_HEAD=$(git rev-parse HEAD)
IMPL_HEAD_ADVANCED=0
if [[ "$POST_HEAD" != "$PRE_HEAD" ]]; then
    if git merge-base --is-ancestor "$PRE_HEAD" "$POST_HEAD"; then
        IMPL_HEAD_ADVANCED=1
    else
        die "implementation changed HEAD … without advancing"
    fi
fi
if [[ "$IMPL_HEAD_ADVANCED" == "0" && -z "$(git status --porcelain)" ]]; then
    die "implementation step produced no changes"
fi
```
Three outcomes:
1. **HEAD advanced fast-forward**: `IMPL_HEAD_ADVANCED=1`. Implement
   committed; we own the diff.
2. **HEAD unchanged but worktree dirty**: `IMPL_HEAD_ADVANCED=0`,
   `_commit_count=0`. Implement edited files but didn't commit.
3. **HEAD unchanged and clean**: `die`. Empty implementation, refuse to
   open empty PR. Same invariant as `localgremlin.py:321-332`.
4. **HEAD changed but not fast-forward**: `die`. Refuse to treat
   divergent commits as the PR's contents.

**Hand-off branch creation** (only path 1, `ghgremlin.sh:488-531`):
```
HANDOFF_BRANCH="ghgremlin-impl-handoff-$$"
git show-ref --verify --quiet "refs/heads/$HANDOFF_BRANCH" \
    && die "hand-off branch $HANDOFF_BRANCH already exists"
git switch -c "$HANDOFF_BRANCH"
[[ -n "$PRE_BRANCH" ]] && git branch -f "$PRE_BRANCH" "$PRE_HEAD"
```
The `$$`-suffix scopes the branch name to this PID so concurrent
gremlins in the same repo don't collide. The PRE_BRANCH reset is a
no-op under launch.sh (PRE_BRANCH is empty); under direct invocation
from a feature branch it's a destructive ref rewrite, intentional and
documented in the source comment at `ghgremlin.sh:476-487`.

**Stale-branch sweep** (still inside `ghgremlin.sh:504-528`):
```
while IFS= read -r _stale; do
    [[ -z "$_stale" || "$_stale" == "$HANDOFF_BRANCH" ]] && continue
    if git merge-base --is-ancestor "$_stale" HEAD 2>/dev/null; then
        git branch -d "$_stale" >/dev/null || true
    else
        echo "warning: leaving divergent hand-off branch $_stale" >&2
    fi
done < <(git for-each-ref --format='%(refname:short)' \
             'refs/heads/ghgremlin-impl-handoff-*')
```
A previous run may have died after `git switch -c` but before
commit-pr's `git branch -m` rename, leaving an orphan branch with a
different `$$` suffix. We're now off any such branch (we just switched
to the new HANDOFF_BRANCH), so deleting already-merged ones with
`git branch -d` is safe — `-d` refuses divergent branches and refuses
worktree-checked-out branches. Divergent ones are left in place with
a warning so an operator can recover unique commits via reflog.

This sweep is the same logic that `_bg/launch.sh` ran at chain start
in earlier versions; the codebase moved it here per
`gremlins: gate interactive rescue Phase B on marker file (#102)` so
that resume runs (which re-enter implement) also sweep prior failed
runs' refs.

**Hand-off → commit-pr branching** (`ghgremlin.sh:534-595`).
Two prompt shapes by `IMPL_HEAD_ADVANCED`:

- HEAD advanced (`:563-569`): the agent is told the work is already
  committed on `$HANDOFF_BRANCH` (`_commit_count` commits above
  `$PRE_HEAD`); rename with `git branch -m`, push, open the PR. If the
  worktree is also dirty (`_worktree_status`), the agent is told to
  amend the most recent commit or add a new one.
- HEAD did not advance (`:571`): standard "create branch from default,
  commit, push, PR" prompt.

The handoff branch's rename in commit-pr is a `git branch -m
<new-name>` to the issue-derived branch name (`issue-${ISSUE_NUM}-<slug>`
or a slug derived from the plan title for cross-repo issues). After
push, the canonical branch is the renamed one; the temporary
`ghgremlin-impl-handoff-*` ref no longer exists.

### Migration mapping

Phase 3 ports as follows:
- `pipeline/git.py` exposes `record_pre_impl_state`,
  `classify_impl_outcome` (returning a tagged-union enum:
  `EmptyImpl | DirtyOnly | HeadAdvanced(commit_count) | DivergentHead`),
  `create_handoff_branch`, `sweep_stale_handoff_branches`,
  `reset_pre_branch`. Each function takes the project root explicitly
  and shells out to `git`; tests use a temp git repo fixture.
- `pipeline/stages/implement.py` orchestrates the implement-stage flow
  for both local and gh: calls `record_pre_impl_state` before
  `client.run`, classifies with `classify_impl_outcome` after, raises
  on `DivergentHead | EmptyImpl`, creates the hand-off branch and
  sweeps stale ones on `HeadAdvanced`. The local pipeline only uses
  the empty-impl invariant; the gh-specific branch dance is gated on
  a `kind="gh"` flag.
- `pipeline/stages/commit_pr.py` reads the implement stage's
  classification result (passed as a typed object, not via state.json)
  and selects the appropriate prompt template from `pipeline/prompts/`.
  No re-classification — the commit-pr stage trusts the implement
  stage's output, eliminating the implicit "is `_commit_count` still
  accurate?" coupling that today's bash carries via `_action_clause`.

The lifecycle's invariants are preserved verbatim: branch name
template (`ghgremlin-impl-handoff-$$` → `ghgremlin-impl-handoff-<pid>`
formatted in Python), the FF-only check via `git merge-base
--is-ancestor`, the `git branch -d` sweep semantics, and the prompt
distinction between handoff/dirty/handoff/clean/no-handoff cases.
Tests should cover every branch of `classify_impl_outcome` plus the
sweep's divergent/already-merged distinction.

## Out of scope for the migration

- Changing the rescue marker JSON shape (the `{"status": "...",
  "summary": "..."}` schema in `gremlins.py:548-561`). Phase 0 documents
  it; the Phase 1–4 ports preserve byte compatibility.
- Changing `state.json` field names. The migration is a ref-onto-ref
  rename of pipeline implementation; the on-disk vocabulary stays
  stable so older gremlins resume cleanly under the new pipeline.
- Migrating `gremlins.py` itself. The /gremlins skill stays as-is and
  reads state.json the way it does today; only the pipeline code that
  *writes* state.json moves.
- Migrating `_bg/finish.sh`, `_bg/set-stage.sh`, `_bg/set-bail.sh`,
  `_bg/liveness.sh`, `_bg/session-summary.sh`. These remain shell
  scripts; the pipeline `state.py` shells out to them rather than
  reimplementing their logic, because they're also called by
  non-pipeline code paths (`session-summary.sh` is a hook;
  `liveness.sh` is sourced by `gremlins.py`).
- Phase B of headless rescue (`gremlins.py` invokes `launch.sh
  --resume`). Phase 0 documents the launcher contract; the launcher
  itself doesn't move.
