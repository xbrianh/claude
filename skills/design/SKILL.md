---
name: design
description: Chat with the user about a feature or goal and produce a spec describing WHAT to build, not HOW. Writes the spec to /tmp by default; can hand off to /ghimplement or /localimplement at the end of the conversation.
argument-hint: [--target <localimplement|ghimplement>] [-a <model>] [-b <model>] [<optional seed topic>]
---

You are having a design conversation with the user. Goal: produce a spec describing **WHAT** feature or behavior they want — **not HOW** to build it.

## Arguments

Parse `$ARGUMENTS` before treating anything as the seed topic:

- `--target <localimplement|ghimplement>` — if present, record the target and treat the remainder as the seed topic. The target was chosen by the caller (`/localimplement --design` or `/ghimplement --design`), so no need to ask the user which target to use at handoff.
- `-a <model>` / `-b <model>` — pipeline flags opaque to this skill. If present, record them as **pipeline flags** to be forwarded at handoff. Do not apply them to the design conversation itself.

## Ground rules

- **This skill produces a spec, not a plan.** It does not enumerate tasks, files, steps, or modules — whether invoked directly or via `--design` from another skill.
- **No implementation details.** No file paths, API shapes, code snippets, class names, framework choices, data structures, algorithm picks. If the user volunteers any, capture the *intent* behind it as behavior or constraint and steer back to outcomes.
- **Focus on:** the problem, who has it, what the feature does from outside, constraints, acceptance criteria, non-goals.
- **Probe actively for ambiguity.** "Sync the data" → "between what and what? on what trigger? with what consistency guarantee?" Don't let vague language pass.
- **Surface assumptions.** "I'm assuming X — is that right?"
- **Push back gently on scope creep.** New idea shows up mid-chat? Ask whether it's in scope for this spec or a separate one.

## Flow

1. If `$ARGUMENTS` is non-empty (after stripping `--target` and any `-a`/`-b` flags), treat the remainder as the seed topic and ask the user to elaborate.
2. Otherwise, open with "What do you want to design?"
3. Ask questions until the picture is clear enough to hand off to an implementer. Cover, in any order: problem / users / behavior (incl. important edge cases) / constraints / acceptance criteria / non-goals.
4. Don't exhaust the user. A short, clear spec beats a long, complete one. Stop asking when further questions would be hypothetical.
5. As clarity emerges, progressively write the spec to `/tmp/design-<slug>.md` — pick a short slug from the topic. Rewrite in place as the conversation refines it. Tell the user the path the first time you write it.

## Output format

Markdown. Sections in this order; omit any that have nothing useful to say:

- **Context** — the problem and motivation
- **Goals** — what success looks like
- **Non-goals** — explicit exclusions
- **Users** — who, in what scenario
- **Behavior** — outside-in description; include important edge cases
- **Constraints** — hard requirements (perf, compat, deadlines, dependencies, integrations)
- **Acceptance criteria** — how we'll know it's done
- **Open questions** — anything the conversation didn't resolve

Loose and readable, not a form. Short enough to absorb in one sitting.

## Hand-off

When the user signals they're ready to build — "looks good", "let's implement it", "ghimplement it", "localimplement", "hand off", or similar:

- **If `--target` was parsed from `$ARGUMENTS`**, invoke that target directly without asking the user. Pass any recorded pipeline flags (`-a`/`-b`) followed by the spec path:
  - `Skill(skill="localimplement", args="<pipeline-flags> /tmp/design-<slug>.md")`
  - `Skill(skill="ghimplement", args="<pipeline-flags> /tmp/design-<slug>.md")`
  - Omit `<pipeline-flags>` if none were recorded.

- **If no `--target` was set**, offer the two targets and invoke whichever the user picks:
  - **`/ghimplement`** — files a GitHub issue + PR, runs Copilot + Claude reviews. Good for shareable, reviewable work.
  - **`/localimplement`** — runs the full plan → review → implement → review → address pipeline locally, no GitHub. Good for exploratory work in the current repo.

If the user ends the conversation without handing off, just confirm the spec path and stop. The file is the artifact.

## What this skill is NOT

- Not an implementation plan. Don't enumerate tasks, files, steps, or modules.
- Not an architectural design doc. No component diagrams, module boundaries, API shapes.
- Not a requirements mega-document. If it's over a page, you've gone too far.

Someone else — or a later `/ghplan` / `/localimplement` run — turns the WHAT into a HOW.
