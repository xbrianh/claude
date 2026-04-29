# `gremlins/`

Shared plan / implement / review / address machinery extracted from
`skills/{localgremlin,ghgremlin,bossgremlin}/`. The skill scripts under
`skills/` are thin shims that exec into `python -m gremlins.cli`; this
package owns the actual orchestration. [`DESIGN.md`](DESIGN.md) is the
binding migration spec — load it for marker protocol, ghgremlin
impl-handoff branch lifecycle, launcher contract, and per-phase rollout
history.

## Module layout

- `cli.py` — `python -m gremlins.cli {local,review,address,gh,boss,fleet,handoff}` dispatch.
- `runner.py` — `run_stages` sequencer (with `resume_from`) + SIGINT/SIGTERM handlers that reap `claude -p` children.
- `state.py` — session-dir resolution, `set_stage` / `emit_bail` / `patch_state` / `check_bail`.
- `git.py` — `in_git_repo`, `git_head`, branch / worktree helpers.
- `gh_utils.py` — `gh` CLI wrappers and stream-json URL extractors used by the gh orchestrator.
- `fleet/` — fleet manager package: status listing + `stop` / `rescue` / `land` / `close` / `rm` / `log` subcommands. Implements the logic behind shim entrypoint `skills/gremlins/gremlins.py`. See [`fleet/CLAUDE.md`](fleet/CLAUDE.md) for the per-module breakdown.
- `handoff.py` — chain-step decision agent (next-plan / chain-done / bail). Implements the logic behind shim entrypoint `skills/handoff/handoff.py`.
- `clients/claude.py` — `ClaudeClient` Protocol + `SubprocessClaudeClient` (production).
- `clients/fake.py` — `FakeClaudeClient` recording test double; replays canned stream-json from fixtures keyed by `label`.
- `stages/` — per-stage bodies: `plan`, `implement`, `review_code`, `address_code`, `commit_pr`, `ghreview`, `ghaddress`, `wait_copilot`. (The `request-copilot` stage body is inlined as a closure in `orchestrators/gh.py`.)
- `orchestrators/local.py` — `local_main`, `review_main`, `address_main`. Implements the logic behind shim entrypoint `skills/localgremlin/localgremlin.py`.
- `orchestrators/gh.py` — `gh_main`. Implements the logic behind shim entrypoint `skills/ghgremlin/ghgremlin.sh`.
- `orchestrators/boss.py` — `boss_main`. Implements the logic behind shim entrypoint `skills/bossgremlin/bossgremlin.py`. Subprocesses out to `python -m gremlins.cli handoff` and `python -m gremlins.cli fleet {stop,land,rescue}` between child gremlins.
- `prompts/` — externalized prompt templates (plan, implement, review lenses, etc).
- `tests/` — pytest suite; `claude` is always faked, but some tests still run local `git` subprocesses and typically stub `gh` at the `subprocess.run` level.

## Entry points

| Subcommand | Module | Replaces |
|---|---|---|
| `local` | `orchestrators.local.local_main` | `skills/localgremlin/localgremlin.py` |
| `review` | `orchestrators.local.review_main` | `localreview.py` |
| `address` | `orchestrators.local.address_main` | `localaddress.py` |
| `gh` | `orchestrators.gh.gh_main` | `skills/ghgremlin/ghgremlin.sh` |
| `boss` | `orchestrators.boss.boss_main` | `skills/bossgremlin/bossgremlin.py` |
| `fleet` | `fleet.main` | `skills/gremlins/gremlins.py` |
| `handoff` | `handoff.main` | `skills/handoff/handoff.py` |

## Testability seam: `ClaudeClient`

Every stage that invokes `claude` takes an injected `client: ClaudeClient`
(Protocol in `clients/claude.py`). Production code passes
`SubprocessClaudeClient()` to those stages; tests pass
`FakeClaudeClient(fixtures={label: <jsonl-or-list>})`. The fake records each
`run(...)` call into `self.calls` for assertion. **Never have a stage
spawn `claude -p` directly** — go through the injected client so tests can
intercept it.

`FakeClaudeClient` looks fixtures up by `label`. Stages that re-enter the
same logical step within one process (e.g. resumed implement) must use
distinct labels per phase.

## Byte-stable strings — DO NOT change

These values are persisted to `state.json` files and read by other
writers (`session-summary.sh` hook, `liveness.sh` sourced from
`session-summary.sh`, the fleet manager that inlines an equivalent
classifier in [`fleet/state.py`](fleet/state.py), the launcher, the rescue
protocol). Renaming any of them silently breaks cross-process
consumers. Source of truth: bail-class constants live in
[`state.py`](state.py); local / gh stage-name vocab is defined and
validated in the orchestrators; marker-protocol bail reasons live in
[`DESIGN.md`](DESIGN.md) (§Marker-protocol bail reasons) and the
`skills/_bg/` scripts.

- **Bail classes** (`state.json.bail_class`): `reviewer_requested_changes`, `security`, `secrets`, `other`.
- **Local stage names**: `plan`, `implement`, `review-code`, `address-code`.
- **Gh stage names**: `plan`, `implement`, `commit-pr`, `request-copilot`, `ghreview`, `wait-copilot`, `ghaddress`.
- **Marker-protocol bail reasons**: `diagnosis_no_marker`, `diagnosis_bad_marker`, `diagnosis_claude_error`, `diagnosis_timeout`, `excluded_class:<class>`, `attempts_exhausted`, `relaunch_launcher_missing`, `relaunch_failed`.

## Bash-script carve-outs

`state.set_stage` and `state.emit_bail` shell out to
`~/.claude/skills/_bg/set-stage.sh` and `set-bail.sh` rather than
patching `state.json` in Python. Those scripts are **also** invoked by
non-gremlins writers (`session-summary.sh` hook, which sources
`liveness.sh` for its at-startup gremlin summary), so the bash scripts
are the single source of truth for the on-disk format. Don't reimplement
that bookkeeping in pure Python — forks would drift.

Both helpers no-op without `GR_ID` and never raise: stage / bail
bookkeeping must not crash a running gremlin.

## Tests

```
cd gremlins && PYTHONPATH=. python -m pytest
```

Equivalently, from `gremlins/tests/`: `PYTHONPATH=$(pwd)/.. python -m pytest`.

## Sync invariant

`gremlins/` is mirrored to `~/.claude/gremlins/` via
[`scripts/sync.sh`](../scripts/sync.sh) (already in `DIR_PAIRS`). Edit
files in the repo, then run `scripts/sync.sh push` to propagate. Never
hand-edit `~/.claude/gremlins/` — the next `pull` will overwrite it, or
the next `push` will silently revert your changes.
