# `pipeline/` design doc

Status: design-only (Phase 0). No code under `pipeline/` other than this file
exists yet. This document is the shared contract for Phases 1, 3, and 4 to
implement against.

## Why this exists

Today the three gremlin orchestrators (`localgremlin`, `ghgremlin`,
`bossgremlin`) live as separate scripts under `skills/<kind>/`. Two are
Python (`localgremlin.py` + `_core.py`, plus the foreground `localreview` /
`localaddress` CLIs), and one is bash (`ghgremlin.sh`). They share a
non-trivial amount of conceptual surface area — stage sequencing, resume,
`claude -p` invocation, stream-json parsing, state-dir bookkeeping, plan
loading, child-process tracking, signal handling — but the shared bits are
either duplicated across the two languages or leak through the launcher
(`skills/_bg/launch.sh`).

The `pipeline/` package consolidates the shared mechanism into one Python
package with a single CLI entry point (`python -m pipeline.cli {local,gh,boss}`),
behind a swappable `ClaudeClient` interface so the orchestrators are testable
without spawning real `claude -p` subprocesses.

## Inventory of shared concepts

The concepts below are present (in some form) in all three gremlins today
and need to be reified by the new package:

### Stage runner

- Each gremlin defines an ordered list of stages and runs them
  sequentially. Today this is:
  - localgremlin: `plan → implement → review-code → address-code` (4 stages,
    `VALID_RESUME_STAGES` in `localgremlin.py:82`).
  - ghgremlin: `plan → implement → commit-pr → request-copilot → ghreview →
    wait-copilot → ghaddress` (7 stages, `STAGES` in `ghgremlin.sh:98`).
  - bossgremlin: chains child gremlins serially, deciding next-step via
    `/handoff` between each.
- `--resume-from <stage>` skips any stage whose index in the list is below
  the target. Each gremlin re-validates its own preconditions before
  declaring the resume safe (see "Resume-precondition checks" below).
- Stage transitions emit a `set_stage` call so `/gremlins` and the
  session-summary hook can see progress. `review-code` additionally emits
  a sub-stage dict (`{holistic, detail, scope}` flipping running→done as
  each reviewer thread finishes). See `_core.py:417`.
- Child-process tracking: long-running stages (notably `review-code`'s
  triple-reviewer fan-out) spawn multiple `claude -p` subprocesses in
  parallel. On SIGINT/SIGTERM we must terminate every live child. Today
  that's `_core._reap_all` (`_core.py:68`) backed by a module-level
  `_children: List[Popen]` under an RLock; bash uses `trap 'kill -- -$$'`.

### `claude -p` wrapper + stream-json logger

- All three gremlins spawn `claude -p <prompt>` with
  `--permission-mode bypassPermissions --output-format stream-json
  --verbose`, optionally `--model <model>`, and parse the resulting
  newline-delimited JSON.
- Python: `_core.run_claude` (`_core.py:263`) tees stdout to a `.jsonl` raw
  trace under the session dir and emits a human-readable progress trace to
  stderr via `log_stream` / `_emit_event` (`_core.py:179`).
- Bash: `progress_tee` in `ghgremlin.sh:19` does the same with `jq`.
- ghgremlin additionally extracts `session_id` from the stream-json `init`
  event (`extract_session_id`, `ghgremlin.sh:358`) so the `commit-pr` stage
  can `--resume <session-id>` the same agent session that did the
  implementation. It also extracts URLs from gh-tool tool_results
  (`extract_gh_url`, `ghgremlin.sh:305`) — preferring the most recent
  `gh issue create` / `gh pr create` call's tool_result over a final-text
  scrape, which is fragile.
- Trim semantics: progress lines are truncated to 200 chars; malformed
  JSON lines from a crashing `claude -p` are skipped silently (the bash
  side does this with `jq … || true`; the Python side does it with a
  try/except around `json.loads` in `log_stream`).

### State dir + artifact layout

- Real gremlins (under `_bg/launch.sh`) get
  `${XDG_STATE_HOME:-$HOME/.local/state}/claude-gremlins/<GR_ID>/` with:
  - `state.json` — the canonical bookkeeping doc (id, kind, workdir,
    branch, status, stage, sub_stage, pid, exit_code, bail_class,
    pipeline_args, parent_id, etc.).
  - `artifacts/` — plan.md, review-code-*.md, stream-*.jsonl traces,
    spec.md (boss only), ghplan-out.jsonl (gh only).
  - `instructions.txt` — full untruncated instructions sidecar (state.json
    only stores a 200-char display summary).
  - `log` — appended stdout/stderr from the gremlin process itself.
  - `pid` — captured PID for liveness / signal delivery.
  - `finished` / `summarized` — terminal markers cleared on resume.
- Direct invocations (no `GR_ID`) drop everything under
  `$STATE_ROOT/direct/<ts>-<rand>/artifacts/` so they're visually separated
  and can be pruned on a simple age-based heuristic
  (`resolve_session_dir`, `_core.py:452`).

### `set_stage` / `emit_bail` bookkeeping

- `set_stage(stage, sub_stage=None)`: shells out to
  `~/.claude/skills/_bg/set-stage.sh` to atomically patch
  `state.json.stage` (and `.sub_stage`). No-op without `GR_ID` or when the
  helper is missing/non-executable. Never raises — bookkeeping must not
  break the running gremlin.
- `emit_bail(bail_class, bail_detail)`: shells out to
  `~/.claude/skills/_bg/set-bail.sh`. Recorded so `/gremlins rescue
  --headless` can decide whether automated recovery is allowed. Bail
  classes seen in the wild: `secrets`, `security`, `reviewer_requested_changes`
  (refused outright), and `other` (the catch-all that allows rescue).
- bash `check_bail` (`ghgremlin.sh:157`) reads `bail_class` after each
  stage and dies the pipeline if non-empty.

### Plan / spec loading and snapshotting

- `localgremlin --plan <path>`: source path is copied into
  `session_dir/plan.md` on fresh launch; on resume the snapshot is
  authoritative (rescue-determinism — the source may have been edited or
  deleted in the meantime). See `localgremlin.py:159`.
- `ghgremlin --plan <path|issue-ref>`: a file path is posted as a new
  GitHub issue (auto-titled by a one-shot `claude -p` call) and the
  contents copied to `artifacts/plan.md`; an issue ref
  (`42` / `#42` / `owner/repo#42` / a full `https://github.com/.../issues/N`
  URL) is fetched via `gh issue view`. Cross-repo issue refs deliberately
  skip the `Closes #N` link to avoid auto-closing an issue in another
  repo. See `ghgremlin.sh:193`.
- `bossgremlin`: `--plan <spec-path>` is required; the spec is copied to
  `state-dir/artifacts/spec.md` by launch.sh up front
  (`launch.sh:472`) and is the north star handed to every child via
  `/handoff`.

### Resume-precondition checks

Each orchestrator validates the in-tree state before agreeing to resume at
a non-zero stage:

- localgremlin (`localgremlin.py:191`):
  - `implement`+: `plan.md` exists and is non-empty.
  - `review-code`+: in git, either dirty tree or any commit reachable from
    HEAD; outside git, the worktree contains at least one non-metadata
    file.
  - `address-code`: all three review-code-*.md exist and are non-empty.
- ghgremlin (`ghgremlin.sh`):
  - `commit-pr` is structurally unresumable (it needs `IMPL_SESSION` from
    the same in-process run), so a request to resume there silently
    rewinds to `implement` (`ghgremlin.sh:107`).
  - Stages from `implement` onward without `--plan` need `issue_url` in
    state.json; stages from `wait-copilot`+ need `pr_url`.
- bossgremlin: resume strategy is "rescue the failing child gremlin in
  place, then the boss continues" rather than fast-forwarding the boss
  itself.

### Handoff invocation contract

bossgremlin calls `~/.claude/skills/handoff/handoff.py --plan <path>
[--out <path>] [--base <ref>] [--model <model>] [--timeout <secs>]`
between children. The handoff agent reads the current plan and the diff
landed on the branch and writes either:
- a `next-plan` document (keep going, here's the next child plan), or
- a `chain-done` marker (we're done), or
- a `bail` marker (something is wrong, stop).

### Branch / worktree management

- localgremlin in a git repo: `launch.sh` creates a named branch
  `bg/localgremlin/<GR_ID>` so commits remain reachable after `finish.sh`.
- ghgremlin: detached worktree, since stage 2b creates and pushes its own
  `issue-N-<slug>` branch.
- Outside a git repo: a copy of the project root via `cp -a`.
- Resume reuses the existing workdir + branch from state.json
  (`launch.sh:124`).

### "Land" semantics

`/gremlins land <id>` lands a finished gremlin onto its target branch:
- localgremlin: squash-merge the `bg/localgremlin/<GR_ID>` branch into the
  parent and clean up the worktree.
- ghgremlin: merge the PR via `gh pr merge` and clean up.

This is `/gremlins`-side logic, not pipeline-side, but `pipeline/` will
expose enough state (branch name, PR URL, exit code) for `/gremlins` to
keep doing its job unchanged.

## Proposed module layout

All paths relative to the repo root.

```
pipeline/
├── DESIGN.md                       # this file
├── pyproject.toml                  # project metadata + pytest config
├── cli.py                          # `python -m pipeline.cli {local,gh,boss}` dispatch
├── runner.py                       # generic stage runner
├── state.py                        # session-dir resolution, set_stage / emit_bail wrappers
├── git.py                          # in_git_repo, git_head, dirty-tree checks, branch/worktree helpers
├── clients/
│   ├── __init__.py
│   ├── claude.py                   # ClaudeClient protocol + real subprocess implementation
│   └── fake.py                     # recording test double
├── stages/
│   ├── __init__.py
│   ├── plan.py                     # local + gh plan resolution (file-source, issue-ref, /ghplan)
│   ├── implement.py
│   ├── review_code.py              # triple-lens parallel reviewer fan-out
│   ├── address_code.py
│   ├── commit_pr.py                # ghgremlin's stage 2b (open PR off the impl session)
│   ├── ghreview.py                 # `/ghreview` + scope reviewer in parallel
│   ├── ghaddress.py                # `/ghaddress`
│   └── wait_copilot.py             # poll gh API for Copilot review
├── orchestrators/
│   ├── __init__.py
│   ├── local.py                    # localgremlin pipeline
│   ├── gh.py                       # ghgremlin pipeline
│   └── boss.py                     # bossgremlin pipeline (chains children + handoff)
├── prompts/                        # stage prompts currently embedded in scripts
│   ├── __init__.py
│   ├── local_plan.md
│   ├── implement.md
│   ├── address_code.md
│   ├── gh_pr.md
│   ├── gh_pr_no_issue.md
│   └── scope_review.md
└── tests/
    ├── __init__.py
    ├── fixtures/
    │   └── stream-json/            # canned `claude -p` output for fake client replay
    ├── test_runner.py
    ├── test_stages_*.py
    └── test_orchestrators_*.py
```

### Module responsibilities

#### `pipeline/cli.py`

Single entry point. Dispatches on the first positional arg:

```
python -m pipeline.cli local   [pipeline-args...] "<instructions>"
python -m pipeline.cli gh      [pipeline-args...] "<instructions>"
python -m pipeline.cli boss    --plan <spec> --chain-kind {local,gh}
```

Each subcommand parses its kind-specific flags, builds the matching
`Orchestrator` instance, and calls `.run()`. The CLI is thin — argument
parsing and config wiring only — so unit tests can construct
orchestrators directly without going through `argparse`.

#### `pipeline/clients/claude.py`

Defines the `ClaudeClient` interface and the real subprocess
implementation. See "ClaudeClient interface" below.

#### `pipeline/clients/fake.py`

Recording test double. Constructed with a fixture directory of canned
stream-json files; each `.run(...)` call:
1. records `(model, prompt, label, raw_path, output_format, flags)` to
   an in-memory call log,
2. picks the next fixture by `label` (or by an explicit replay map
   passed at construction time),
3. writes the fixture to `raw_path` and returns. Non-zero exits are
   simulated by configuring the fake to raise on a given label.

#### `pipeline/runner.py`

Generic stage runner. Owns:
- the stage list (`["plan", "implement", ...]`),
- the resume index resolution (`--resume-from <stage>` → start_idx),
- the per-stage emit-`set_stage`-then-execute loop,
- bail propagation: if a stage records a `bail_class`, the runner halts
  before the next stage starts.

Each stage is a callable taking a typed `StageContext` (session_dir,
plan_text, claude client, is_git, models config, gremlin id) and
returning either `None` or a stage-specific result struct (e.g.
review-code returns the three output paths).

#### `pipeline/state.py`

- `resolve_session_dir() -> Path`: ports `_core.resolve_session_dir`.
- `set_stage(stage, sub_stage=None)`: ports `_core.set_stage`.
- `emit_bail(bail_class, detail)`: ports `_core.emit_bail`.
- `read_state() / patch_state(filter, **args)`: thin jq-equivalent
  wrappers for orchestrators that need to read or atomically patch
  state.json (e.g. ghgremlin persisting `issue_url`, `pr_url`).

#### `pipeline/git.py`

- `in_git_repo() -> bool`
- `git_head() -> str`
- `dirty_tree() -> bool`
- `has_commits() -> bool`
- `worktree_has_files(skip: Iterable[Path]) -> bool` (the non-git
  fallback used by localgremlin's resume-precondition check)

#### `pipeline/stages/`

One module per stage. Each module exports a single `run(ctx) -> Result`
function plus the prompt template (loaded from `pipeline/prompts/`).
Stages depend only on `ctx.client: ClaudeClient`, never on
`subprocess.Popen` directly — this is what lets fake clients drive them.

#### `pipeline/orchestrators/`

Per-kind orchestrators that wire stage modules together via
`Runner`. They own:
- argv parsing (the same `-p/-i/-x/-a/-b/-c` for local, `-r/--model` for
  gh, `--plan/--chain-kind` for boss),
- resume-precondition checks (which delegate to `git.py` helpers),
- stage selection (which stages to include, in what order),
- "land" handoff (boss only).

#### `pipeline/prompts/`

Prompts that today are embedded as Python triple-quoted strings or bash
heredocs become standalone files loaded by stage modules at import or
call time. Embedded substitutions (`{plan_text}`, `{core_principles}`,
etc.) use `str.format` with named keys.

#### `pipeline/pyproject.toml`

```toml
[project]
name = "pipeline"
version = "0.0.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

## ClaudeClient interface

The minimal surface based on current `_core.run_claude` and `ghgremlin.sh`
usage:

```python
from typing import Protocol, Sequence
from pathlib import Path

class ClaudeClient(Protocol):
    def run(
        self,
        model: str,
        prompt: str,
        *,
        label: str,
        raw_path: Path,
        output_format: str = "stream-json",
        flags: Sequence[str] = (),
        resume_session: str | None = None,
    ) -> None:
        """Spawn `claude -p`, stream stdout to raw_path (also tee to stderr
        as a human-readable progress trace), and raise on non-zero exit.

        Implementations MUST track child processes and reap them on
        SIGINT/SIGTERM (parity with _core._reap_all)."""
        ...

    def extract_session_id(self, raw_path: Path) -> str:
        """Read the system/init event from a stream-json trace file and
        return its session_id. Used by ghgremlin's commit-pr stage to
        resume the implement-stage agent session for the PR-opening
        prompt."""
        ...

    def extract_url(
        self,
        raw_path: Path,
        url_pattern: str,
        cmd_pattern: str,
        label: str,
    ) -> str:
        """Scan a stream-json trace for tool_use Bash commands matching
        cmd_pattern, pair them with their tool_result, and return the
        most recent URL matching url_pattern. Falls back to the final
        result text scan."""
        ...
```

### Real implementation (`clients/claude.py`)

- Owns the module-level `_children: list[Popen]` and an `RLock`.
- Installs `SIGINT` / `SIGTERM` handlers on first construction (idempotent
  guard so multiple clients in one process don't stomp the handler chain).
- Streams stdout via the same `log_stream` logic as today (8 KiB buffered
  reader so `readline()` doesn't degrade to one `os.read()` per byte —
  this is a measured perf win on long implement-stage traces, see
  `_core.py:271`).
- Defaults `flags` to `("--permission-mode", "bypassPermissions",
  "--output-format", "stream-json", "--verbose")`. `output_format=` is
  separate (and default `"stream-json"`) so future callers can switch to
  `"text"` for one-shot non-streaming calls (e.g. ghgremlin's issue-title
  generation in `ghgremlin.sh:209`).
- `--model <model>` is appended only when `model` is truthy. Today both
  scripts gate the `--model` flag the same way; bossgremlin and resume
  rehydrate `MODEL` from `state.json`.

### Fake implementation (`clients/fake.py`)

- Constructed with a `replay_dir: Path` containing fixtures named by
  `label` (e.g. `plan.jsonl`, `implement.jsonl`,
  `review-code:sonnet.jsonl`).
- Each `.run(...)` writes the matching fixture to `raw_path` and pushes
  `(model, prompt, label, ...)` onto `self.calls` for assertions.
- Supports `client.fail_on(label, exit_code=1)` to simulate a stage
  failure.
- `.extract_session_id` / `.extract_url` reuse the real-client parsing
  logic (parsing is pure, so we don't need to fake it — only the
  subprocess invocation is faked).

## `launch.sh --resume` contract update

Today `launch.sh` resolves a gremlin to its pipeline binary by checking
`$HOME/.claude/skills/<kind>/<kind>.{py,sh}` (`launch.sh:140` for resume,
`launch.sh:280` for fresh launch). After Phase 1 lands `pipeline/`, the
mapping changes:

### New resolution order

1. If `$HOME/.claude/pipeline/` exists and `python -m pipeline.cli` is
   invocable, prefer:
   ```bash
   PIPELINE=("python" "-m" "pipeline.cli" "$KIND_SHORT")
   ```
   where `$KIND_SHORT` is `local`, `gh`, or `boss` (the `gremlin` suffix
   is dropped — `python -m pipeline.cli local-gremlin` is needlessly
   redundant).
2. Else fall back to `$HOME/.claude/skills/<kind>/<kind>.{py,sh}` (the
   current behavior). This is the migration safety net: a `pipeline/`
   that is broken or only partially synced still lets a user run the old
   path-based gremlins.

### Argv shape

The CLI accepts the same pipeline-level flags the gremlins accept today,
in the same order. Resume continues to work via:

```bash
python -m pipeline.cli <kind> [pipeline-args...] --resume-from <stage> [<instructions>]
```

`launch.sh` needs no awareness of which stages exist for which kind — it
only forwards `--resume-from <stage>` from `state.json.stage` and the
persisted `pipeline_args`. The orchestrators do their own resume-target
validation (this matches today's contract: an invalid `--resume-from`
dies inside the orchestrator with a clear message).

### `PIPELINE` variable shape

Today `PIPELINE` is a single path string passed to `nohup bash -c '"$PIPELINE" "$@"'`.
With the new resolution, `PIPELINE` becomes an array
(`("python" "-m" "pipeline.cli" "$KIND_SHORT")`), expanded inside the
`nohup bash -c` invocation. The single-quoted `bash -c` body needs to be
adjusted to expand the array, e.g.:

```bash
nohup bash -c '"$@"; EC=$?; "$HOME/.claude/skills/_bg/finish.sh" "$GR_ID" "$EC"' \
    -- "${PIPELINE[@]}" "${PIPELINE_ARGS[@]}" --resume-from "$STAGE" </dev/null \
    >>"$STATE_DIR/log" 2>&1 &
```

Both the resume branch and the fresh-launch branch will need this change;
the change is mechanical once `pipeline/` is in place.

### Migration order

- Phase 1: introduces `pipeline/` and `pipeline.cli local`, with
  `localgremlin` migrated. `launch.sh` resolves `localgremlin` to the new
  CLI; `ghgremlin` and `bossgremlin` still resolve to the old paths.
- Phase 3: `ghgremlin` migrates. `launch.sh` resolution now hits the
  pipeline CLI for `ghgremlin` too.
- Phase 4: `bossgremlin` migrates. The skills under
  `skills/{local,gh,boss}gremlin/` shrink to thin SKILL.md + a stub
  shim that just execs `python -m pipeline.cli <kind>` (the shim is
  retained so `launch.sh`'s file-existence check still resolves, and so
  callers that hand-invoke `~/.claude/skills/...` still work).

## Open questions

- `pipeline/` at repo root vs. `scripts/pipeline/`. **Default: repo root.**
  `scripts/sync.sh` would need a new entry under `DIR_PAIRS` either way;
  repo-root keeps the package importable as `pipeline.*` without
  `sys.path` gymnastics, and matches how `agents/`, `commands/`, `skills/`
  already sit at the top level.
- Whether to ship a single `pyproject.toml` at the repo root (covering
  both the existing scripts and `pipeline/`) or keep `pipeline/` as its
  own self-contained project. Default: self-contained, since the existing
  scripts are not a Python package.
- Whether `ClaudeClient.extract_session_id` and `.extract_url` belong on
  the client or as free functions in a `pipeline/streamjson.py` helper.
  They're parsing-only, no I/O, so making them free functions is purer —
  but keeping them on the client matches "everything claude-shaped goes
  through one interface" and gives the fake one place to stub. **Default:
  client methods, as drafted above.**
