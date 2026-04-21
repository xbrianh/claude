# Claude Code config

Personal [Claude Code](https://claude.com/claude-code) configuration — global instructions, settings, skills, agents, and the `ghimplement` workflow. The repo is bidirectionally mirrored with `~/.claude/` via [`scripts/sync.sh`](scripts/sync.sh).

## Layout

```
CLAUDE.md             # repo-level doc for Claude, loaded when cwd is this repo (not synced)
home/CLAUDE.md        # global preferences, synced to ~/.claude/CLAUDE.md
settings.json         # harness settings: hooks, permissions, plugins
skills/
  _bg/                # background-workflow scripts: launch.sh, finish.sh, session-summary.sh
  design/             # /design: chat-driven spec writer; hands off to /ghimplement or /localimplement
  ghplan/             # /ghplan: draft a plan, post it as a GitHub issue
  ghimplement/        # /ghimplement: run the full pipeline in the background via skills/_bg/launch.sh
  ghreview/           # /ghreview: review a PR and post inline comments
  ghaddress/          # /ghaddress: address review comments on a PR
  localimplement/     # /localimplement: full local pipeline via skills/_bg/launch.sh; sibling lens-*.md files hold reviewer prompts
  localland/          # /localland: squash-merge a finished /localimplement branch onto the current branch; --gh creates a PR instead
  workflows/          # /workflows: on-demand status of background pipelines; supports stop/rescue/rm subcommands
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

Before any non-dry-run `push`, the script snapshots `~/.claude/` into `/tmp/claude-backup-<timestamp>-<suffix>/` and prints the path. If a push clobbers something, recover by copying files back out of that directory. Retention follows the OS `/tmp` policy — no built-in cleanup.

## Skills

The skills cluster into a GitHub-issue-driven pipeline and a local pipeline (`/localimplement`), with `/design` as an optional first step for either and `/ghreview` / `/ghaddress` usable on their own:

- [`/design`](skills/design/SKILL.md) — runs a WHAT-focused spec conversation; writes the spec to `/tmp/design-<slug>.md` by default; can hand off to `/ghimplement` or `/localimplement`. Pass `--target localimplement` (or `ghimplement`) to pre-select the handoff target — this is what the `--design` flag on `/localimplement` and `/ghimplement` sets automatically.
- [`/ghplan`](skills/ghplan/SKILL.md) — draft a plan and post it as a new GitHub issue.
- [`/ghimplement`](skills/ghimplement/SKILL.md) — run the full pipeline end-to-end via [`skills/ghimplement/ghimplement.sh`](skills/ghimplement/ghimplement.sh).
- [`/ghreview`](skills/ghreview/SKILL.md) — review a PR and post inline comments.
- [`/ghaddress`](skills/ghaddress/SKILL.md) — address review comments on a PR and reply to each thread.
- [`/localimplement`](skills/localimplement/SKILL.md) — local (no-GitHub) counterpart to `/ghimplement`: runs plan → implement → three parallel reviewers (holistic, detail, scope) → address-code locally via [`skills/localimplement/localimplement.py`](skills/localimplement/localimplement.py), with all artifacts written to `~/.local/state/claude-workflows/<id>/artifacts/` (off the product branch). Accepts `--design` to invoke `/design` first.
- [`/localland`](skills/localland/SKILL.md) — squash-merge a finished `/localimplement` workflow branch onto the current branch as a single well-messaged commit, then delete the workflow branch and state directory. `--gh` pushes the result as a new PR against `main` instead.
- [`/workflows`](skills/workflows/SKILL.md) — on-demand status of background pipelines. Subcommands: `stop <id>` (SIGTERM a running pipeline), `rescue <id>` (diagnose and resume a dead/stalled workflow inline), `rm <id>` (delete the state directory, log, worktree directory, and branch). Flags include `--here`, `--ack`, `--ack-all`, `--running`, `--dead`, `--stalled`, `--kind`, `--since`, `--recent`, `--watch`.

`skills/ghimplement/ghimplement.sh` chains them: `/ghplan` → implement → `/ghreview` (Copilot + Claude) → `/ghaddress`, producing a merged-ready PR from a single instruction.

### Background execution

Both `/ghimplement` and `/localimplement` run **in the background**. Both accept `--design` as a first flag to invoke `/design` before the pipeline. Their SKILL.md wrappers hand off to [`skills/_bg/launch.sh`](skills/_bg/launch.sh), which:

- Creates an isolated worktree (via `git worktree add --detach` for git projects, `cp -a` otherwise) so concurrent invocations don't collide.
- Discovers the pipeline script by trying `<kind>.py` before `<kind>.sh` — so [`localimplement.py`](skills/localimplement/localimplement.py) takes precedence over any `.sh` fallback.
- Spawns the pipeline detached (subshell + `nohup`), so it survives `Ctrl-C`, shell exit, and Claude Code quitting.
- Records per-workflow state under `~/.local/state/claude-workflows/<id>/` — or `$XDG_STATE_HOME/claude-workflows/<id>/` if `XDG_STATE_HOME` is set — (`state.json`, combined `log`, `finished` / `acknowledged` markers), deliberately rooted outside `~/.claude/` so Claude Code's sensitive-file guardrail doesn't block subagent writes. [`skills/_bg/finish.sh`](skills/_bg/finish.sh) writes the terminal `status` / `exit_code` and drops the `finished` marker that `session-summary.sh` keys off.
- Returns within ~1s with the workflow id, workdir, log path, and state-file path.

`/localimplement` artifacts under `~/.local/state/claude-workflows/<id>/artifacts/`: `plan.md`, `review-code-holistic-<model>.md`, `review-code-detail-<model>.md`, `review-code-scope-<model>.md`. If launched with `--design`, a `spec.md` is also written there.

A pair of hooks (`SessionStart` + `UserPromptSubmit`, wired in [`settings.json`](settings.json)) invokes [`skills/_bg/session-summary.sh`](skills/_bg/session-summary.sh), which reports running and newly-finished workflows for the current project so you're notified the next time you open Claude Code in that tree. Acknowledged state dirs older than 14 days are pruned on the next hook firing.

## Getting started

1. Clone this repo.
2. Review [`settings.json`](settings.json) and [`home/CLAUDE.md`](home/CLAUDE.md) — these land in `~/.claude/` on `push`.
3. Run `scripts/sync.sh diff` to see what would change against your current `~/.claude/`.
4. **If you have existing content in `~/.claude/skills/`, `~/.claude/agents/`, or `~/.claude/commands/`, run `scripts/sync.sh push --dry-run` first.** `push` mirrors these directories with `rsync --delete`, so any files not present in this repo will be removed from `~/.claude/`. The `DELETE_THRESHOLD` guardrail catches large deletions, but smaller losses still slip through.
5. Run `scripts/sync.sh push` to install.
