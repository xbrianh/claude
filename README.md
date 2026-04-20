# Claude Code config

Personal [Claude Code](https://claude.com/claude-code) configuration — global instructions, settings, skills, agents, and the `ghimplement` workflow. The repo is bidirectionally mirrored with `~/.claude/` via [`scripts/sync.sh`](scripts/sync.sh).

## Layout

```
CLAUDE.md             # repo-level doc for Claude, loaded when cwd is this repo (not synced)
home/CLAUDE.md        # global preferences, synced to ~/.claude/CLAUDE.md
settings.json         # harness settings: hooks, permissions, plugins
skills/
  _bg/                # shared background-workflow launcher + SessionStart hook
  ghplan/             # /ghplan: draft a plan, post it as a GitHub issue
  ghimplement/        # /ghimplement: run the full pipeline in the background via skills/_bg/launch.sh
  ghreview/           # /ghreview: review a PR and post inline comments
  ghaddress/          # /ghaddress: address review comments on a PR
  localimplement/     # /localimplement: full local pipeline in the background via skills/_bg/launch.sh
agents/
  pragmatic-developer.md
commands/             # slash commands (created on first sync; no files tracked yet)
scripts/sync.sh       # bidirectional sync with ~/.claude
```

Intentionally untracked: `settings.local.json` and `.claude/` — see [`.gitignore`](.gitignore).

## Sync workflow

[`scripts/sync.sh`](scripts/sync.sh) is the source of truth. It tracks exactly the paths in `FILE_PAIRS` and `DIR_PAIRS` at the top of the script.

```
scripts/sync.sh pull     # ~/.claude → repo
scripts/sync.sh push     # repo → ~/.claude
scripts/sync.sh diff     # show differences (alias: status)
```

Flags: `-n`/`--dry-run` to preview, `-f`/`--force` to allow more than `DELETE_THRESHOLD` (5) directory-pair deletions, `-y`/`--yes` to skip the confirmation prompt.

Directory pairs (`skills/`, `agents/`, `commands/`) sync with `rsync --delete`, so `push` and `pull` mirror — extras on the destination side are removed. Only those directory-pair deletions count toward `DELETE_THRESHOLD`: the guardrail refuses a non-dry-run sync that would delete more than 5 files this way unless `--force` is passed. With `--dry-run`, the script still previews the deletions but does not refuse the run.

## Skills

The four `gh*` skills compose into a GitHub-issue-driven workflow:

- [`/ghplan`](skills/ghplan/SKILL.md) — draft a plan and post it as a new GitHub issue.
- [`/ghimplement`](skills/ghimplement/SKILL.md) — run the full pipeline end-to-end via [`skills/ghimplement/ghimplement.sh`](skills/ghimplement/ghimplement.sh).
- [`/ghreview`](skills/ghreview/SKILL.md) — review a PR and post inline comments.
- [`/ghaddress`](skills/ghaddress/SKILL.md) — address review comments on a PR and reply to each thread.
- [`/localimplement`](skills/localimplement/SKILL.md) — local (no-GitHub) counterpart to `/ghimplement`: runs plan → review-plan ×2 → address-plan → implement → review-code ×2 → address-code locally via [`skills/localimplement/localimplement.sh`](skills/localimplement/localimplement.sh), with all artifacts written to `.claude-workflow/<timestamp>/`.

`skills/ghimplement/ghimplement.sh` chains them: `/ghplan` → implement → `/ghreview` (Copilot + Claude) → `/ghaddress`, producing a merged-ready PR from a single instruction.

### Background execution

Both `/ghimplement` and `/localimplement` run **in the background**. Their SKILL.md wrappers hand off to [`skills/_bg/launch.sh`](skills/_bg/launch.sh), which:

- Creates an isolated worktree (via `git worktree add --detach` for git projects, `cp -a` otherwise) so concurrent invocations don't collide.
- Spawns the pipeline detached (subshell + `nohup`), so it survives `Ctrl-C`, shell exit, and Claude Code quitting.
- Records per-workflow state under `~/.claude/workflows/<id>/` (`state.json`, combined `log`, `finished` / `acknowledged` markers). This directory is intentionally **not** synced — it's runtime state, not config.
- Returns within ~1s with the workflow id, workdir, log path, and state-file path.

A pair of hooks (`SessionStart` + `UserPromptSubmit`, wired in [`settings.json`](settings.json)) invokes [`skills/_bg/session-summary.sh`](skills/_bg/session-summary.sh), which reports running and newly-finished workflows for the current project so you're notified the next time you open Claude Code in that tree. Acknowledged state dirs older than 14 days are pruned on the next hook firing.

## Getting started

1. Clone this repo.
2. Review [`settings.json`](settings.json) and [`home/CLAUDE.md`](home/CLAUDE.md) — these land in `~/.claude/` on `push`.
3. Run `scripts/sync.sh diff` to see what would change against your current `~/.claude/`.
4. **If you have existing content in `~/.claude/skills/`, `~/.claude/agents/`, or `~/.claude/commands/`, run `scripts/sync.sh push --dry-run` first.** `push` mirrors these directories with `rsync --delete`, so any files not present in this repo will be removed from `~/.claude/`. The `DELETE_THRESHOLD` guardrail catches large deletions, but smaller losses still slip through.
5. Run `scripts/sync.sh push` to install.
