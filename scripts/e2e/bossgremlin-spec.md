# Stages documentation for gremlin SKILL.md files

## Context

The three gremlin skills — `/localgremlin`, `/ghgremlin`, and `/bossgremlin` — each run a
multi-stage pipeline that is described only at a high level in their SKILL.md files. The
current documentation covers where artifacts land and what arguments to pass, but does not
enumerate the discrete pipeline stages or describe what each stage produces. This gap makes
it hard to interpret the `/gremlins` stage-column output, understand which artifacts to
inspect after a partial run, or reason about what happened when a stage fails mid-chain.

## Goal

Add a `## Stages` subsection to each of the three gremlin SKILL.md files that enumerates
the stages each gremlin runs and describes what each stage produces. The subsections should
be accurate, concise, and consistent in style across all three files.

## Tasks

**Implement one SKILL.md file per child gremlin. Do not bundle all three into a single child.**

### Task 1 — `skills/localgremlin/SKILL.md`

Add a `## Stages` section describing the four pipeline stages and their outputs:

1. **plan** — runs the planning agent against the instructions or supplied `--plan` file;
   produces `artifacts/plan.md`.
2. **implement** — implements the plan; produces commits on the `bg/localgremlin/<id>` branch.
3. **review-code** — runs three parallel reviewers (holistic, detail, scope) using two
   different models; produces `artifacts/review-code-holistic-<model>.md`,
   `artifacts/review-code-detail-<model>.md`, and `artifacts/review-code-scope-<model>.md`.
4. **address-code** — addresses actionable review findings; produces an
   "Address review feedback" commit (absent if reviewers found nothing actionable).

Position the section after `## Where artifacts go` and before `## Arguments`.

### Task 2 — `skills/ghgremlin/SKILL.md`

Add a `## Stages` section describing the four pipeline stages:

1. **plan** — invokes `/ghplan`; produces a GitHub issue containing the implementation plan.
2. **implement** — implements the plan; produces commits and opens a GitHub PR.
3. **review** — invokes `/ghreview` with a scope reviewer running in parallel; collects
   Copilot and Claude review comments on the PR.
4. **address** — invokes `/ghaddress`; produces new commits on the PR that address reviewer
   findings.

Position the section after `## Where artifacts go` and before `## Arguments`.

### Task 3 — `skills/bossgremlin/SKILL.md`

Add a `## Stages` section describing the boss's repeating handoff loop. The boss does not
run a linear stage sequence — it loops through handoff → child pipeline → land until the
handoff agent signals completion.

Each loop iteration:

1. **handoff** — invokes the `/handoff` agent with the overarching spec and the diff
   accumulated so far; produces `handoff-NNN.md` (updated rolling plan for the remaining
   work), `handoff-NNN-child.md` (plan handed to the next child), and
   `handoff-NNN.state.json` (exit state: `next-plan`, `chain-done`, or `bail`). Exits the
   loop when the exit state is `chain-done` or `bail`.
2. **waiting** — launches the child gremlin with the child plan and polls its `state.json`
   until the child's full pipeline exits. If the child fails, rescues it once before
   retrying.
3. **landing** — squash-merges the child's branch into the boss's target branch.

On `chain-done`, the target branch contains the squash-merged output of every child gremlin
in order.

Position the section after the workflow overview bullet list (items 1–4) and before
`## Arguments`.

## Non-goals

- Do not change any other content in the SKILL.md files.
- Do not add stages documentation to any files other than the three listed above.
- Do not restructure or reformat sections that are not being modified.
