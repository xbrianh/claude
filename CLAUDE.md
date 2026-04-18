# This repo

Personal Claude Code config, bidirectionally mirrored with `~/.claude/` (plus `~/bin/ghimplement.sh`) via [`scripts/sync.sh`](scripts/sync.sh). See [`README.md`](README.md) for the full overview.

## Source of truth

`scripts/sync.sh` is authoritative for what is tracked. Do not hand-edit files under `~/.claude/` without running `scripts/sync.sh pull` afterward, and do not edit repo files without running `scripts/sync.sh push` to propagate.

## Tracked paths

Only the paths in `FILE_PAIRS` and `DIR_PAIRS` at the top of `scripts/sync.sh` are synced. Anything outside those paths is untracked by the sync.

- `FILE_PAIRS`: `home/CLAUDE.md` → `~/.claude/CLAUDE.md`, `settings.json` → `~/.claude/settings.json`, `bin/ghimplement.sh` → `~/bin/ghimplement.sh`.
- `DIR_PAIRS`: `skills/`, `agents/`, `commands/` ↔ `~/.claude/{skills,agents,commands}/`.

## Directory mirroring caveat

`DIR_PAIRS` sync with `rsync --delete` — deletions on either side are real. Pending deletions count toward `DELETE_THRESHOLD` (5); above that, `push`/`pull` refuse without `--force`.

## Adding a new skill / agent / command

Create the file under the corresponding repo directory (`skills/<name>/SKILL.md`, `agents/<name>.md`, `commands/<name>.md`), then run `scripts/sync.sh push`.

## The `gh*` skill pipeline

[`bin/ghimplement.sh`](bin/ghimplement.sh) chains the four `gh*` skills (`/ghplan` → implement → `/ghreview` → `/ghaddress`) into an end-to-end GitHub-issue-driven workflow.

## Not tracked

`settings.local.json` and `.claude/` are intentionally ignored — see [`.gitignore`](.gitignore).
