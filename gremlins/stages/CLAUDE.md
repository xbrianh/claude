# `gremlins/stages/`

Per-stage bodies — the work each pipeline stage actually does. Modules
here are called by orchestrators in `../orchestrators/`; they own no
sequencing logic of their own.

## Modules

- `plan.py` — `run_plan_stage`. Local pipeline only.
- `implement.py` — `run_implement_stage`. Dual-mode (`kind='local'` /
  `kind='gh'`). For gh: enforces the empty-implementation invariant,
  classifies the outcome (`HeadAdvanced` / `DirtyOnly` / `EmptyImpl` /
  `DivergentHead`), creates the impl-handoff branch, and returns an
  `ImplStageResult` carrying `session_id` so commit-pr can resume the same
  claude session.
- `review_code.py` — `run_review_code_stage`. Local pipeline only
  (single-detail-reviewer post-collapse).
- `address_code.py` — `run_address_code_stage`. Local pipeline only.
- `commit_pr.py` — `run_commit_pr_stage`. Gh pipeline. Resumes the
  implement session (`claude --resume <session_id>`) so the same agent
  that wrote the code creates the branch and opens the PR.
- `ghreview.py` — `run_ghreview_stage`. Thin wrapper around `/ghreview
  <pr_url>` plus a `check_bail` call.
- `ghaddress.py` — `run_ghaddress_stage`. Thin wrapper around `/ghaddress
  <pr_url>`.
- `wait_copilot.py` — `run_request_copilot_stage` and
  `run_wait_copilot_stage`. The Copilot review request + polling loop.

The `request-copilot` stage is the exception — its body is inlined as a
closure inside `../orchestrators/gh.py` rather than living here.

## Conventions

- Public function name: `run_<stage>_stage`. Keyword-only args
  (`def f(*, client, model, ...)`).
- Every stage that talks to `claude` takes `client: ClaudeClient` and
  calls `client.run(...)`. **Never spawn `claude -p` directly** — that
  bypasses the test seam in `../clients/claude.py`.
- Prompt templates live in `../prompts/` (and lens files under
  `../prompts/lenses/`). Resolve them via:
  ```python
  PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "<name>.md"
  ```
  Don't hard-code absolute paths or use `cwd`-relative paths — `__file__`
  resolves into `~/.claude/gremlins/...` regardless of the orchestrator's
  cwd.
- Stages that should respect a bail marker (set by the agent via
  `python -m gremlins.cli bail`) call `check_bail(<phase-name>)` from
  `..state` after the claude run. The runner inspects the bail and
  halts the pipeline.
- Most stages return `None`. Stages that produce information the
  orchestrator needs (`implement.py` → `ImplStageResult`,
  `commit_pr.py` → PR URL string) return it; the orchestrator threads
  it into later stages.
- The `label=` argument passed to `client.run(...)` is the stream-event
  prefix and the `FakeClaudeClient` fixture key. Stages that re-enter the
  same logical step within one process (e.g. resumed implement) must use
  distinct labels per phase so the fake's lookup doesn't collide.

## Load-bearing invariants

- `implement.py` enforces the empty-implementation invariant: an empty
  impl in the gh pipeline raises `EmptyImpl` and the runner bails. This
  is the firewall that keeps no-op runs out of `commit-pr` / `ghreview`.
  Don't soften it.
- `commit_pr.py` selects its action clause based on the `ImplOutcome`
  classification from the implement stage. The three shapes
  (`HeadAdvanced`, `DirtyOnly`, plus the empty-handoff fallback) are
  distinct prompts in `../prompts/` — keep them aligned.
