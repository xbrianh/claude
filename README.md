# Claude Code config

Personal [Claude Code](https://claude.com/claude-code) configuration — global instructions, settings, skills, agents, and the `ghgremlin` gremlin. The repo is bidirectionally mirrored with `~/.claude/` via [`scripts/sync.sh`](scripts/sync.sh).

## Layout

```
CLAUDE.md             # repo-level doc for Claude, loaded when cwd is this repo (not synced)
home/CLAUDE.md        # global preferences, synced to ~/.claude/CLAUDE.md
settings.json         # harness settings: hooks, permissions, plugins
skills/
  _bg/                # background-gremlin scripts: finish.sh, launch.sh, liveness.sh, session-summary.sh, set-stage.sh
  design/             # /design: chat-driven spec writer; hands off to /ghgremlin or /localgremlin
  ghplan/             # /ghplan: draft a plan, post it as a GitHub issue
  ghgremlin/          # /ghgremlin: run the full gremlin in the background via skills/_bg/launch.sh
  ghreview/           # /ghreview: review a PR and post inline comments
  ghaddress/          # /ghaddress: address review comments on a PR
  localgremlin/       # /localgremlin: full local gremlin via skills/_bg/launch.sh; sibling lens-*.md files hold reviewer prompts; _core.py holds the shared review/address stage bodies
  localreview/        # /localreview: standalone triple-lens code review over local changes (foreground)
  localaddress/       # /localaddress: standalone address-code stage over existing review files (foreground)
  gremlins/           # /gremlins: on-demand status of background gremlins; supports stop/rescue/rm/close/land subcommands
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

The skills cluster into a GitHub-issue-driven gremlin and a local gremlin (`/localgremlin`), with `/design` as an optional first step for either and `/ghreview` / `/ghaddress` usable on their own:

- [`/design`](skills/design/SKILL.md) — runs a WHAT-focused spec conversation; writes the spec to `/tmp/design-<slug>.md` by default; can hand off to `/ghgremlin` or `/localgremlin`. Pass `--target localgremlin` (or `ghgremlin`) to pre-select the handoff target — this is what the `--design` flag on `/localgremlin` and `/ghgremlin` sets automatically.
- [`/ghplan`](skills/ghplan/SKILL.md) — draft a plan and post it as a new GitHub issue.
- [`/ghgremlin`](skills/ghgremlin/SKILL.md) — run the full gremlin run end-to-end via [`skills/ghgremlin/ghgremlin.sh`](skills/ghgremlin/ghgremlin.sh).
- [`/ghreview`](skills/ghreview/SKILL.md) — review a PR and post inline comments.
- [`/ghaddress`](skills/ghaddress/SKILL.md) — address review comments on a PR and reply to each thread.
- [`/localgremlin`](skills/localgremlin/SKILL.md) — local (no-GitHub) counterpart to `/ghgremlin`: runs plan → implement → three parallel reviewers (holistic, detail, scope) → address-code locally via [`skills/localgremlin/localgremlin.py`](skills/localgremlin/localgremlin.py), with all artifacts written to `~/.local/state/claude-gremlins/<id>/artifacts/` (off the product branch). Accepts `--design` to invoke `/design` first. Review-code and address-code stage bodies live in [`skills/localgremlin/_core.py`](skills/localgremlin/_core.py) and are shared with the standalone skills below.
- [`/localreview`](skills/localreview/SKILL.md) — standalone triple-lens code review (holistic, detail, scope) over local changes, foreground. Writes `review-code-*.md` files to `--dir` (defaults to cwd).
- [`/localaddress`](skills/localaddress/SKILL.md) — standalone address-code stage that reads `review-code-*.md` files from `--dir` and applies actionable findings. Foreground. In a git repo, creates one `Address review feedback` commit (no push).
- [`/gremlins`](skills/gremlins/SKILL.md) — on-demand status of background gremlins. Subcommands: `stop <id>`, `rescue <id>`, `rm <id>`, `close <id>`, `land <id>` (squash-land a local gremlin or merge a gh PR). Flags include `--here`, `--running`, `--dead`, `--stalled`, `--kind`, `--since`, `--recent`, `--watch`.

`skills/ghgremlin/ghgremlin.sh` chains them: `/ghplan` → implement → `/ghreview` (Copilot + Claude in parallel with a scope reviewer) → `/ghaddress`, producing a merged-ready PR from a single instruction.

### Background execution

Both `/ghgremlin` and `/localgremlin` run **in the background**. Both accept `--design` as a first flag to invoke `/design` before the gremlin. Their SKILL.md wrappers hand off to [`skills/_bg/launch.sh`](skills/_bg/launch.sh), which:

- Creates an isolated worktree (via `git worktree add --detach` for git projects, `cp -a` otherwise) so concurrent invocations don't collide.
- Discovers the gremlin script by trying `<kind>.py` before `<kind>.sh` — so [`localgremlin.py`](skills/localgremlin/localgremlin.py) takes precedence over any `.sh` fallback.
- Spawns the gremlin detached (subshell + `nohup`), so it survives `Ctrl-C`, shell exit, and Claude Code quitting.
- Records per-gremlin state under `~/.local/state/claude-gremlins/<id>/` — or `$XDG_STATE_HOME/claude-gremlins/<id>/` if `XDG_STATE_HOME` is set — (`state.json`, combined `log`, `finished` / `closed` markers), deliberately rooted outside `~/.claude/` so Claude Code's sensitive-file guardrail doesn't block subagent writes. [`skills/_bg/finish.sh`](skills/_bg/finish.sh) writes the terminal `status` / `exit_code` and drops the `finished` marker that `session-summary.sh` keys off.
- Returns within ~1s with the gremlin id, workdir, log path, and state-file path.

`/localgremlin` artifacts under `~/.local/state/claude-gremlins/<id>/artifacts/`: `plan.md`, `review-code-holistic-<model>.md`, `review-code-detail-<model>.md`, `review-code-scope-<model>.md`. If launched with `--design`, a `spec.md` is also written there.

A pair of hooks (`SessionStart` + `UserPromptSubmit`, wired in [`settings.json`](settings.json)) invokes [`skills/_bg/session-summary.sh`](skills/_bg/session-summary.sh), which reports running and newly-finished gremlins for the current project so you're notified the next time you open Claude Code in that tree. Closed state dirs older than 14 days are pruned on the next hook firing.

## Getting started

1. Clone this repo.
2. Review [`settings.json`](settings.json) and [`home/CLAUDE.md`](home/CLAUDE.md) — these land in `~/.claude/` on `push`.
3. Run `scripts/sync.sh diff` to see what would change against your current `~/.claude/`.
4. **If you have existing content in `~/.claude/skills/`, `~/.claude/agents/`, or `~/.claude/commands/`, run `scripts/sync.sh push --dry-run` first.** `push` mirrors these directories with `rsync --delete`, so any files not present in this repo will be removed from `~/.claude/`. The `DELETE_THRESHOLD` guardrail catches large deletions, but smaller losses still slip through.
5. Run `scripts/sync.sh push` to install.
