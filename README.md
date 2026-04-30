# Claude Code config

Personal [Claude Code](https://claude.com/claude-code) configuration — global instructions, settings, skills, agents, and the `ghgremlin` gremlin. The repo is bidirectionally mirrored with `~/.claude/` via [`scripts/sync.sh`](scripts/sync.sh).

## Layout

```
CLAUDE.md             # repo-level doc for Claude, loaded when cwd is this repo (not synced)
home/CLAUDE.md        # global preferences, synced to ~/.claude/CLAUDE.md
settings.json         # harness settings: hooks, permissions, plugins
gremlins/             # Python package: shared plan/implement/review/address stages, ClaudeClient protocol, orchestrators for local/gh/boss pipelines, fleet manager, handoff agent. Synced to ~/.claude/gremlins/.
skills/
  _bg/                # background-gremlin scripts: finish.sh, launch.sh
  design/             # /design: chat-driven spec writer; produces /tmp/design-<slug>.md
  ghplan/             # /ghplan: draft a plan, post it as a GitHub issue
  ghgremlin/          # /ghgremlin: run the full gremlin in the background via python -m gremlins.cli launch
  ghreview/           # /ghreview: review a PR and post inline comments
  ghaddress/          # /ghaddress: address review comments on a PR
  localgremlin/       # /localgremlin: thin shims that exec into `python -m gremlins.cli`; the orchestrator and stages live under gremlins/
  localreview/        # /localreview: standalone detail-only code review over local changes (foreground)
  localaddress/       # /localaddress: standalone address-code stage over the detail review file (foreground)
  gremlins/           # /gremlins: thin shim into `python -m gremlins.cli fleet`; on-demand status + stop/rescue/rm/close/land subcommands (logic in gremlins/fleet.py)
  handoff/            # /handoff: thin shim into `python -m gremlins.cli handoff`; foreground chain-step decision agent (logic in gremlins/handoff.py)
  bossgremlin/        # /bossgremlin: chained serial gremlin workflow; runs multiple child gremlins via handoff agent
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

Directory pairs (`skills/`, `agents/`, `commands/`, `gremlins/`) sync with `rsync --delete`, so `push` and `pull` mirror — extras on the destination side are removed. Only those directory-pair deletions count toward `DELETE_THRESHOLD`: the guardrail refuses a non-dry-run sync that would delete more than 5 files this way unless `--force` is passed. With `--dry-run`, the script still previews the deletions but does not refuse the run.

Before any non-dry-run `push`, the script snapshots `~/.claude/` into `/tmp/claude-backup-<timestamp>-<suffix>/` and prints the path. If a push clobbers something, recover by copying files back out of that directory. Retention follows the OS `/tmp` policy — no built-in cleanup.

## Skills

The skills cluster into a GitHub-issue-driven gremlin and a local gremlin (`/localgremlin`), with `/design` as an optional standalone spec writer and `/ghreview` / `/ghaddress` usable on their own:

- [`/design`](skills/design/SKILL.md) — runs a WHAT-focused spec conversation; writes the spec to `/tmp/design-<slug>.md` and stops. The user can then pass that path to `/ghgremlin` or `/localgremlin` themselves.
- [`/ghplan`](skills/ghplan/SKILL.md) — draft a plan and post it as a new GitHub issue.
- [`/ghgremlin`](skills/ghgremlin/SKILL.md) — GitHub-issue-driven counterpart to `/localgremlin`: runs plan → implement → PR creation → Copilot + Claude review → address via the [`gremlins`](gremlins/) package (`python -m gremlins.cli gh`). Orchestrator lives in [`gremlins/orchestrators/gh.py`](gremlins/orchestrators/gh.py); [`skills/ghgremlin/ghgremlin.sh`](skills/ghgremlin/ghgremlin.sh) is a thin shim kept for manual invocation.
- [`/ghreview`](skills/ghreview/SKILL.md) — review a PR and post inline comments.
- [`/ghaddress`](skills/ghaddress/SKILL.md) — address review comments on a PR and reply to each thread.
- [`/localgremlin`](skills/localgremlin/SKILL.md) — local (no-GitHub) counterpart to `/ghgremlin`: runs plan → implement → detail review → address-code locally via the [`gremlins`](gremlins/) package (`python -m gremlins.cli local`), with all artifacts written to `~/.local/state/claude-gremlins/<id>/artifacts/` (off the product branch). Orchestrator and stages live under [`gremlins/orchestrators/local.py`](gremlins/orchestrators/local.py) and [`gremlins/stages/`](gremlins/stages/); `/localreview` and `/localaddress` dispatch to the same code via `gremlins.cli review` and `gremlins.cli address`.
- [`/localreview`](skills/localreview/SKILL.md) — standalone detail-only code review over local changes, foreground. Writes `review-code-detail-*.md` to `--dir` (defaults to cwd).
- [`/localaddress`](skills/localaddress/SKILL.md) — standalone address-code stage that reads the `review-code-detail-*.md` file from `--dir` and applies actionable findings. Foreground. In a git repo, creates one `Address review feedback` commit (no push).
- [`/gremlins`](skills/gremlins/SKILL.md) — on-demand status of background gremlins. Subcommands: `stop <id>`, `rescue <id>`, `rm <id>`, `close <id>`, `land <id>` (squash-land a local gremlin or merge a gh PR). Flags include `--here`, `--running`, `--dead`, `--stalled`, `--kind`, `--since`, `--recent`, `--watch`.
- [`/handoff`](skills/handoff/SKILL.md) — foreground chain-step decision agent. Reads the current plan and the diff landed so far, produces `next-plan` / `chain-done` / `bail`, and writes an updated plan plus a child plan for the next gremlin. Accepts `--plan <path> [--out <path>] [--base <ref>] [--model <model>] [--timeout <secs>]`.
- [`/bossgremlin`](skills/bossgremlin/SKILL.md) — background chained serial workflow. Requires `--plan <spec-path>` (immutable top-level spec) and `--chain-kind local|gh`. The boss invokes `/handoff` between child gremlins, lands each one before proceeding, and notifies when the chain finishes. Use `/gremlins` to monitor (`KIND=boss`) and `rescue <boss-id>` to resume a stalled chain.

The gh pipeline (driven by `python -m gremlins.cli gh`) chains them: `/ghplan` → implement → `/ghreview` (Copilot + Claude) → `/ghaddress`, producing a merged-ready PR from a single instruction.

### Background execution

Both `/ghgremlin` and `/localgremlin` run **in the background**. Their SKILL.md wrappers invoke `python -m gremlins.cli launch`, which:

- Creates an isolated worktree (via `git worktree add --detach` for git projects, `cp -a` otherwise) so concurrent invocations don't collide.
- Dispatches per kind to the right `gremlins.cli` subcommand: `localgremlin` → `local`, `ghgremlin` → `gh`, `bossgremlin` → `boss`.
- Spawns the gremlin detached (subshell + `nohup`), so it survives `Ctrl-C`, shell exit, and Claude Code quitting.
- Records per-gremlin state under `~/.local/state/claude-gremlins/<id>/` — or `$XDG_STATE_HOME/claude-gremlins/<id>/` if `XDG_STATE_HOME` is set — (`state.json`, combined `log`, `finished` / `closed` markers), deliberately rooted outside `~/.claude/` so Claude Code's sensitive-file guardrail doesn't block subagent writes. Terminal state (`status`, `exit_code`, `finished` marker) is written by the `_run-pipeline` subcommand boundary in `gremlins/cli.py`.
- Returns within ~1s with the gremlin id, workdir, log path, and state-file path.

`/localgremlin` artifacts under `~/.local/state/claude-gremlins/<id>/artifacts/`: `plan.md`, `review-code-detail-<model>.md`. If a spec file is passed as the first positional argument, it is also copied there as `spec.md`.

A pair of hooks (`SessionStart` + `UserPromptSubmit`, wired in [`settings.json`](settings.json)) invokes `python -m gremlins.cli session-summary` ([`gremlins/fleet/session_summary.py`](gremlins/fleet/session_summary.py)), which reports running and newly-finished gremlins for the current project so you're notified the next time you open Claude Code in that tree. Closed state dirs older than 14 days are pruned on the next hook firing.

## Getting started

1. Clone this repo.
2. Review [`settings.json`](settings.json) and [`home/CLAUDE.md`](home/CLAUDE.md) — these land in `~/.claude/` on `push`.
3. Run `scripts/sync.sh diff` to see what would change against your current `~/.claude/`.
4. **If you have existing content in `~/.claude/skills/`, `~/.claude/agents/`, or `~/.claude/commands/`, run `scripts/sync.sh push --dry-run` first.** `push` mirrors these directories with `rsync --delete`, so any files not present in this repo will be removed from `~/.claude/`. The `DELETE_THRESHOLD` guardrail catches large deletions, but smaller losses still slip through.
5. Run `scripts/sync.sh push` to install.
