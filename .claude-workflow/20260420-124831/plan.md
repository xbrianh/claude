# Workflow status visibility

## Context

`/localimplement` and `/ghimplement` pipelines are multi-stage (plan → implement → review → address, with sub-stages). They run detached from the launching Claude session via `skills/_bg/launch.sh`, survive session exit, and can run 3–5 in parallel across branches of the same repo.

Today, the only feedback surface is the `SessionStart` / `UserPromptSubmit` hook in `skills/_bg/session-summary.sh`, which reports a flat *"running (pid N, workdir …)"* line — no stage, no sub-stage, no task description, and filtered to the current project. There is no on-demand, rich status command.

Persistent state already exists at `~/.claude/workflows/<id>/` (from `skills/_bg/launch.sh`): `state.json` with `id`, `kind`, `project_root`, `workdir`, `setup_kind`, `branch`, `status`, `started_at`, `instructions`, `pid` (and `ended_at`, `exit_code` after `finish.sh` runs); plus `log`, `pid`, and `finished` / `acknowledged` markers. This plan extends that state and adds a user-facing status command on top of it.

## Approach

Two-layer change:

1. **Pipelines record their stage into state.json as they advance.** Add a tiny helper `skills/_bg/set-stage.sh <id> <stage> [sub_stage_json]` that atomically patches `state.json`. Call it from `skills/localimplement/localimplement.sh` and `skills/ghimplement/ghimplement.sh` at each `==> [N/M]` stage boundary, and from within the parallel reviewer runner to emit per-reviewer sub-stage updates (`opus=running,sonnet=returned`).

2. **Add a user-invocable `/workflows` skill** that reads every `~/.claude/workflows/*/state.json` on the machine, applies the same liveness logic already present in `session-summary.sh` (augmented with a log-mtime staleness heuristic), and prints one scannable line per active workflow.

Why this split over alternatives:

- **Writing stage into `state.json`** (vs. scraping the `log` file) keeps the status command O(N) cheap and robust: `jq` over a handful of small JSON files, no regex-parsing of partial stream-json logs. Stage transitions are coarse (handful per workflow) so the write volume is trivial.
- **A separate skill** (vs. expanding `session-summary.sh`) preserves the hook's job — passive, low-noise notifications — and gives the user an explicit, on-demand command that can be richer (full descriptions, sub-stage detail, crashed-reason excerpts) without spamming every prompt.
- **Reusing the existing state dir** (vs. a new database, socket, or daemon) fits the solo-laptop scope and inherits existing guarantees: atomic `jq | mv` writes, 14-day prune of acknowledged dirs, crashed-pid detection.
- **Human-readable description**: capture at launch time by adding a `--description` flag (or positional first arg) plumbed through `skills/_bg/launch.sh` from the skill wrappers (`skills/{localimplement,ghimplement}/SKILL.md` already run inside a Claude session that composed the instructions — they can pass a short phrase alongside the full instructions). Truncating `instructions` is a weak fallback; an explicit description is more readable and is what the spec asks for.

### Resolutions to spec open questions

- **Task description capture**: `SKILL.md` for both skills already has Claude context at launch; extend the launcher CLI with `--description <phrase>` and update both SKILL.md files to instruct Claude to produce a ≤60-char phrase and pass it in. Fallback to `instructions[:60]` if omitted.
- **Dead definition**: treat as dead if (a) `status != "running"` and exit code is nonzero (hard exit via `finish.sh`), OR (b) `status == "running"` but recorded `pid` is gone and no `finished` marker (already detected in `session-summary.sh`, lift into shared helper), OR (c) `status == "running"`, pid alive, but log file mtime is older than a threshold (default 30 min — tunable; emit as "stalled?" not "dead" so the user can judge).
- **Dead lingering**: reuse the existing `acknowledged` marker pattern. `/workflows` shows dead entries until a flag (`/workflows --ack <id>` or `/workflows --ack-all`) touches `acknowledged`. 14-day prune already in `session-summary.sh` keeps the directory bounded.
- **Distinguish `/localimplement` vs `/ghimplement`**: yes — trivial since `kind` is already in state.json. Render as a short prefix column (`[local]` / `[gh]`).

## Tasks

- [ ] Task 1 — Add `skills/_bg/set-stage.sh`: CLI `set-stage.sh <wf_id> <stage> [sub_stage_json]` that reads `$HOME/.claude/workflows/<wf_id>/state.json`, patches the `stage` (and optional `sub_stage`) and `stage_updated_at` fields atomically via `jq | mv`. Fail silently on missing state file (stage writes must never break a running pipeline). Mark executable.

- [ ] Task 2 — Extend `skills/_bg/launch.sh`: accept a `--description <phrase>` flag before `<kind>`; record it in the initial `state.json` as `description`; fall back to `instructions[:60]` (existing slice) when absent. Pass `WF_ID` into the pipeline's environment (already exported — confirm) so stage helpers can find it.

- [ ] Task 3 — Instrument `skills/localimplement/localimplement.sh` with stage writes: at each `==> [N/4]` boundary call `"$HOME/.claude/skills/_bg/set-stage.sh" "$WF_ID" plan` / `implement` / `review-code` / `address-code`. Inside `run_dual_review`, after each `wait`, write a `sub_stage` JSON like `{"review_a":"done","review_b":"running"}`. Guard all calls with `[[ -n "${WF_ID:-}" ]]` so direct CLI invocations outside the launcher are unaffected.

- [ ] Task 4 — Instrument `skills/ghimplement/ghimplement.sh` with stage writes: at each `==> [N/6]` boundary call `set-stage.sh` with `plan` / `implement` / `commit-pr` / `request-copilot` / `ghreview` / `wait-copilot` / `ghaddress`. Same `WF_ID` guard.

- [ ] Task 5 — Update `skills/localimplement/SKILL.md` and `skills/ghimplement/SKILL.md` so the wrapper instructs Claude (at launch) to pass `--description "<≤60-char phrase>"` to the launcher. Include an example phrase derived from the user's instructions.

- [ ] Task 6 — Create `skills/workflows/` with `SKILL.md` and `workflows.sh`:
  - `workflows.sh [--all] [--ack <id>] [--ack-all]`
  - Default: list all currently-active workflows (running + dead-unacknowledged) on the machine, sorted by `started_at`.
  - Output columns: `kind`, `id` (short-hash form), `stage` / `sub_stage`, `liveness`, `description`, `age`.
  - Liveness logic (shared helper `_bg/liveness.sh`): hard-exit → `dead (exit N)`, pid-gone-no-marker → `dead (crashed)`, stale log (>30 min default, env-tunable `BG_STALL_SECS`) → `stalled? (no log update 45m)`, otherwise `running`.
  - `--ack <id>` / `--ack-all` touch the `acknowledged` marker for matching dead/finished workflows.
  - Exit 0 always; any unexpected error logs to stderr and continues (same "hooks must never break a session" principle).
  - `SKILL.md` frontmatter: `allowed-tools: Bash(~/.claude/skills/workflows/workflows.sh:*)`. Brief instructions telling Claude to run the script and print the output verbatim.

- [ ] Task 7 — Refactor shared liveness logic: extract the pid-gone-no-marker detection currently inlined in `skills/_bg/session-summary.sh` into `skills/_bg/liveness.sh` (sourced library: defines `liveness_of_state_file <path>` echoing `running` / `dead:<reason>` / `stalled:<reason>`). Use it from both `session-summary.sh` and `workflows.sh` so they never disagree.

- [ ] Task 8 — Enrich `session-summary.sh` output to include `stage` and `description` from `state.json` (leveraging the new fields) so the passive notification matches the richer on-demand view. Keep it project-scoped (existing behavior); the on-demand `/workflows` command is the machine-wide view per spec.

- [ ] Task 9 — Update `scripts/sync.sh` tracked-paths list / `CLAUDE.md` only if needed: the new `skills/workflows/` directory will be picked up automatically by the existing `DIR_PAIRS` entry for `skills/`. Confirm no list edits required; run `scripts/sync.sh push --dry-run` mentally (in plan, not code) to verify.

- [ ] Task 10 — Manual verification: launch 3 `/localimplement` workflows in parallel against different toy tasks; run `/workflows` and confirm all 3 show with distinct descriptions and stages; kill one mid-run and confirm it reports `dead (crashed)`; let another complete and confirm it disappears after `--ack`; re-run `/workflows` a few seconds after a stage advances and confirm the new stage is reflected.

## Open questions

- **Stall threshold**: 30 min is a guess. A planning stage doing a lot of code reading could reasonably go 5+ min without a log line; an implementation stage rarely does. Should the threshold vary by stage, or is one global "definitely suspicious" number (45 min?) good enough? Suggest: single global threshold, env-tunable, err on the side of false negatives (don't call it stalled unless it really probably is).
- **Description generation at launch**: the cleanest path is for the wrapper (skill) to pass `--description`, but that relies on the *launching* Claude session doing it correctly. Alternative: have `launch.sh` shell out to `claude -p` to summarize the first 500 chars of instructions into a phrase. That adds ~5–10s to launch latency — the spec says launcher must return fast. Recommend the wrapper-side approach and accept that a poorly-instructed launch may fall back to the truncated-instructions default.
- **Machine-wide view vs. project-filtered default**: the spec says "see workflows launched from *any* Claude Code session on this machine". But when the user has 5 projects open, seeing workflows from all 5 in every status call may be noisy. Suggest: `/workflows` defaults to **all projects** (matches spec literal); `/workflows --here` filters to current project. Could flip the default if the all-projects view proves noisy in practice.
- **Sub-stage shape**: JSON object `{"review_a": "running", "review_b": "returned"}` vs. a human string `"opus=running, sonnet=returned"`. JSON is easier to evolve for other sub-stages (e.g., "waiting for Copilot"); a string renders faster. Lean JSON, render it in the status formatter.
- **Should `/workflows` also surface recent log tail on `--verbose`?** Spec says "dead, with a short reason" — that maps to exit code + marker. Showing a log tail is strictly extra. Mark as stretch, not in first pass.
- **Acknowledgment UX**: `/workflows --ack <id>` requires the user to type a workflow id. Should `/workflows` accept short unique prefixes (`--ack 20260420-19`)? Nice-to-have; defer.
