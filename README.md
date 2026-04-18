# Claude Code config

Personal [Claude Code](https://claude.com/claude-code) configuration — global instructions, settings, skills, agents, and the `ghimplement` workflow. The repo is bidirectionally mirrored with `~/.claude/` (plus `~/bin/ghimplement.sh`) via [`scripts/sync.sh`](scripts/sync.sh).

## Layout

```
CLAUDE.md             # global instructions (see CLAUDE.md)
settings.json         # harness settings: hooks, permissions, plugins
bin/ghimplement.sh    # chained plan → implement → review → address pipeline
skills/
  ghplan/             # /ghplan: draft a plan, post it as a GitHub issue
  ghimplement/        # /ghimplement: run the full pipeline via bin/ghimplement.sh
  ghreview/           # /ghreview: review a PR and post inline comments
  ghaddress/          # /ghaddress: address review comments on a PR
agents/
  pragmatic-developer.md
commands/             # slash commands (currently empty)
scripts/sync.sh       # bidirectional sync with ~/.claude and ~/bin
```

Intentionally untracked: `settings.local.json` and `.claude/` — see [`.gitignore`](.gitignore).

## Sync workflow

[`scripts/sync.sh`](scripts/sync.sh) is the source of truth. It tracks exactly the paths in `FILE_PAIRS` and `DIR_PAIRS` at the top of the script.

```
scripts/sync.sh pull     # ~/.claude → repo
scripts/sync.sh push     # repo → ~/.claude
scripts/sync.sh diff     # show differences (alias: status)
```

Flags: `--dry-run` to preview, `--force` to allow deleting more than `DELETE_THRESHOLD` (5) files, `--yes` to skip the confirmation prompt.

Directory pairs (`skills/`, `agents/`, `commands/`) sync with `rsync --delete`, so `push` and `pull` mirror — extras on the destination side are removed. The `DELETE_THRESHOLD` guardrail refuses any sync that would delete more than 5 files unless `--force` is passed.

## Skills

The four `gh*` skills compose into a GitHub-issue-driven workflow:

- [`/ghplan`](skills/ghplan/SKILL.md) — draft a plan and post it as a new GitHub issue.
- [`/ghimplement`](skills/ghimplement/SKILL.md) — run the full pipeline end-to-end via [`bin/ghimplement.sh`](bin/ghimplement.sh).
- [`/ghreview`](skills/ghreview/SKILL.md) — review a PR and post inline comments.
- [`/ghaddress`](skills/ghaddress/SKILL.md) — address review comments on a PR and reply to each thread.

`bin/ghimplement.sh` chains them: `/ghplan` → implement → `/ghreview` (Copilot + Claude) → `/ghaddress`, producing a merged-ready PR from a single instruction.

## Getting started

1. Clone this repo.
2. Review [`settings.json`](settings.json) and [`CLAUDE.md`](CLAUDE.md) — these land in `~/.claude/` on `push`.
3. Run `scripts/sync.sh diff` to see what would change against your current `~/.claude/`.
4. Run `scripts/sync.sh push` to install.
