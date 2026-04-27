# This repo

Personal Claude Code config, bidirectionally mirrored with `~/.claude/` via [`scripts/sync.sh`](scripts/sync.sh). See [`README.md`](README.md) for the full overview.

## Source of truth

`scripts/sync.sh` is authoritative for what is tracked. Do not hand-edit files under `~/.claude/` without running `scripts/sync.sh pull` afterward, and do not edit repo files without running `scripts/sync.sh push` to propagate.

## Tracked paths

Only the paths in `FILE_PAIRS` and `DIR_PAIRS` at the top of `scripts/sync.sh` are synced. Anything outside those paths is untracked by the sync. The list below is kept in sync manually with `scripts/sync.sh` — update both together.

- `FILE_PAIRS`: `home/CLAUDE.md` → `~/.claude/CLAUDE.md`, `settings.json` → `~/.claude/settings.json`.
- `DIR_PAIRS`: `skills/`, `agents/`, `commands/`, `pipeline/` ↔ `~/.claude/{skills,agents,commands,pipeline}/`. A tracked directory that doesn't yet exist on the source side is skipped (not created) — e.g. `commands/` is tracked but currently empty and won't be mirrored until it has contents.

## Directory mirroring caveat

`DIR_PAIRS` sync with `rsync --delete` — deletions on either side are real. Pending deletions count toward `DELETE_THRESHOLD` (5); non-dry-run `push`/`pull` refuse above that threshold unless `--force` is used, while `--dry-run` still previews the deletions.

## Adding a new skill / agent / command

Create the file under the corresponding repo directory (`skills/<name>/SKILL.md`, `agents/<name>.md`, `commands/<name>.md`), then run `scripts/sync.sh push`.

## The `gh*` gremlin

The `ghgremlin` skill invokes [`skills/ghgremlin/ghgremlin.sh`](skills/ghgremlin/ghgremlin.sh) to run an end-to-end GitHub-issue-driven workflow: `/ghplan`, an implementation + PR creation stage, `/ghreview` (with a scope reviewer running in parallel), and `/ghaddress`. `--plan <path|issue-ref>` skips the `/ghplan` stage and uses the supplied file or existing issue as the plan instead.

## The local gremlin

The `localgremlin` skill runs plan → implement → a single detail reviewer → address-code locally via the [`pipeline`](pipeline/) package (`python -m pipeline.cli local`). All artifacts land in `~/.local/state/claude-gremlins/<id>/artifacts/` off the product branch. `--plan <path>` skips the plan stage and uses the supplied file as the plan.

The orchestrator main and stage bodies live under [`pipeline/orchestrators/local.py`](pipeline/orchestrators/local.py) and [`pipeline/stages/`](pipeline/stages/); the standalone `/localreview` and `/localaddress` skills dispatch to `pipeline.cli review` / `pipeline.cli address` so all three callers execute the same code. The same applies to `/gremlins` (→ `pipeline.cli fleet`) and `/handoff` (→ `pipeline.cli handoff`) — fleet management and chain-step decisions live in [`pipeline/fleet.py`](pipeline/fleet.py) and [`pipeline/handoff.py`](pipeline/handoff.py). The skill scripts under `skills/localgremlin/`, `skills/gremlins/`, and `skills/handoff/` are thin shims that exec into `pipeline.cli`.

## Additional skills

- [`/gremlins`](skills/gremlins/SKILL.md) — on-demand status of all background gremlins on this machine. Key subcommands: `stop <id>` (SIGTERM), `rescue <id>` (diagnosis step: inline `claude -p` diagnoses and fixes the failure in the foreground; relaunch step: `launch.sh --resume` relaunches the pipeline at the failed stage in the background under the original gremlin id, with a `(rescue)` liveness marker), `rm <id>` (delete state directory, worktree directory, and branch), `close <id>` (mark finished gremlin closed), `log <id>` (`tail -F` the gremlin's log — convenience wrapper for watching a long-running stage in real time), `land <id>` (squash-land a local gremlin or merge a gh PR and clean up). Use `--here` to filter to the current repo.
- [`/localreview`](skills/localreview/SKILL.md) — foreground: run the detail code review over local changes, writing `review-code-detail-*.md` to `--dir` (cwd by default). No planning, no implementation, no background gremlin.
- [`/localaddress`](skills/localaddress/SKILL.md) — foreground: read the `review-code-detail-*.md` file from `--dir` and address actionable findings. In a git repo, creates a single `Address review feedback` commit (no push).
- [`/handoff`](skills/handoff/SKILL.md) — foreground: reads the current plan and landed diff, decides `next-plan` / `chain-done` / `bail`, and writes an updated plan plus a child plan for the next gremlin. Accepts `--plan <path> [--out <path>] [--base <ref>] [--model <model>] [--timeout <secs>]`.
- [`/bossgremlin`](skills/bossgremlin/SKILL.md) — background chained serial workflow driven by a top-level spec; invokes `/handoff` between child gremlins and lands each before proceeding. Requires `--plan <spec-path> --chain-kind local|gh`. Monitor with `/gremlins` (`KIND=boss`); rescue a stalled chain with `rescue <boss-id>`.

## Not tracked

`settings.local.json` and `.claude/` are intentionally ignored — see [`.gitignore`](.gitignore).
