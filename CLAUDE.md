# This repo

Personal Claude Code config, bidirectionally mirrored with `~/.claude/` via [`scripts/sync.sh`](scripts/sync.sh). See [`README.md`](README.md) for the full overview.

## Source of truth

`scripts/sync.sh` is authoritative for what is tracked. Do not hand-edit files under `~/.claude/` without running `scripts/sync.sh pull` afterward, and do not edit repo files without running `scripts/sync.sh push` to propagate.

## Tracked paths

Only the paths in `FILE_PAIRS` and `DIR_PAIRS` at the top of `scripts/sync.sh` are synced. Anything outside those paths is untracked by the sync. The list below is kept in sync manually with `scripts/sync.sh` — update both together.

- `FILE_PAIRS`: `home/CLAUDE.md` → `~/.claude/CLAUDE.md`, `settings.json` → `~/.claude/settings.json`.
- `DIR_PAIRS`: `skills/`, `agents/`, `commands/` ↔ `~/.claude/{skills,agents,commands}/`. A tracked directory that doesn't yet exist on the source side is skipped (not created) — e.g. `commands/` is tracked but currently empty and won't be mirrored until it has contents.

## Directory mirroring caveat

`DIR_PAIRS` sync with `rsync --delete` — deletions on either side are real. Pending deletions count toward `DELETE_THRESHOLD` (5); non-dry-run `push`/`pull` refuse above that threshold unless `--force` is used, while `--dry-run` still previews the deletions.

## Adding a new skill / agent / command

Create the file under the corresponding repo directory (`skills/<name>/SKILL.md`, `agents/<name>.md`, `commands/<name>.md`), then run `scripts/sync.sh push`.

## The `gh*` gremlin

The `ghgremlin` skill invokes [`skills/ghgremlin/ghgremlin.sh`](skills/ghgremlin/ghgremlin.sh) to run an end-to-end GitHub-issue-driven workflow: `/ghplan`, an implementation + PR creation stage, `/ghreview` (with a scope reviewer running in parallel), and `/ghaddress`.

Both `/ghgremlin` and `/localgremlin` accept `--design` as a first flag to invoke `/design` before the gremlin, running an interactive spec conversation and handing off automatically when the user is ready.

## Additional skills

- [`/gremlins`](skills/gremlins/SKILL.md) — on-demand status of all background gremlins on this machine. Key subcommands: `stop <id>` (SIGTERM), `rescue <id>` (diagnose and resume inline), `rm <id>` (delete state directory, worktree directory, and branch), `land <id>` (squash-land a local gremlin or merge a gh PR and clean up). Use `--here` to filter to the current repo.

## Not tracked

`settings.local.json` and `.claude/` are intentionally ignored — see [`.gitignore`](.gitignore).
