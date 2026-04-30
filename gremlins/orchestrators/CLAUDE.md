# `gremlins/orchestrators/`

Per-pipeline orchestrator entry points. Each module owns one CLI subcommand
(see `../cli.py` dispatch) and wires the appropriate stages from
`../stages/` into a sequence.

## Modules

- `local.py` — `local_main` (full local chain), `review_main` (review-code
  only), `address_main` (address-code only). Subcommands: `local`, `review`,
  `address`.
- `gh.py` — `gh_main`. Subcommand: `gh`. Drives the gh pipeline:
  `plan → implement → commit-pr → request-copilot → ghreview →
  wait-copilot → ghaddress`. The `request-copilot` stage body is inlined
  here as a closure rather than living in `../stages/`.
- `boss.py` — `boss_main`. Subcommand: `boss`. Not a stage sequencer —
  drives a chain of child gremlins, subprocessing out to
  `python -m gremlins.cli {handoff,fleet}` between each one. State lives in
  `boss_state.json` (schema preserved byte-for-byte from the legacy
  `bossgremlin.py`).

## Conventions

- Each `*_main(argv)` returns an int exit code; the CLI dispatch in
  `../cli.py` calls them with `sys.argv[2:]`.
- `local.py` and `gh.py` build a real `SubprocessClaudeClient()` by default
  and pass it into stages via the `client: ClaudeClient` seam (see parent
  CLAUDE.md). Tests inject a `FakeClaudeClient`. Never have an orchestrator
  spawn `claude -p` directly.
- Stage bodies live in `../stages/`. Orchestrators wire them up (resume
  semantics, signal handlers, session-dir resolution) — keep stage logic
  out of these files.
- Stage-name vocabulary per orchestrator is byte-stable (see parent
  CLAUDE.md §"Byte-stable strings"). `VALID_RESUME_STAGES` /
  `VALID_STAGES` constants are the source of truth.
- `AGENT_FILE` in `local.py` and `gh.py` resolves
  `agents/pragmatic-developer.md` via three `parent`s up from `__file__`.
  This works because the package lives at `~/.claude/gremlins/` and the
  agents dir is its sibling. If you move this directory deeper, update the
  parent count.

## Boss-specific notes

- `boss.py` subprocesses out via `_gremlins_cli_cmd` / `_gremlins_cli_env`.
  The env helper sets `PYTHONSAFEPATH=1` and prepends the package's parent
  to `PYTHONPATH` so `python -m gremlins.cli` resolves to
  `~/.claude/gremlins/` regardless of cwd (worktree-shadow protection).
- The `--resume-from` flag forwarded by `launch.sh --resume` is *ignored*:
  boss resumes from `boss_state.json`, not the runner's stage vocabulary.
- `SIGTERM` is trapped to set `_stop_requested` and forward to the current
  child process; the chain checks `check_stop()` between operations.
