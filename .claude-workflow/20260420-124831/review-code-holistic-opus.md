# Review (opus)

## Summary

The implementation covers all ten plan tasks and lands cleanly: a small shared `liveness.sh` library, atomic `state.json` stage writes, a new `/workflows` skill, and enriched session-summary output. The design is right-shaped — reusing the existing `~/.claude/workflows/<id>/` directory, matching the "hooks must never break a session" silent-failure conventions, and keeping each new script small and focused. One real bug (an infinite arg-parse loop in `workflows.sh` on `--ack` with no argument), one regression in the session-summary fallback, and a few minor UX/scope nits listed below.

## Findings

### `workflows.sh --ack` with no id infinite-loops

- **File:** `skills/workflows/workflows.sh:41`
- **Severity:** major
- **What:** `--ack) MODE="ack"; TARGET="${2:-}"; shift 2 || true ;;` — when the user invokes `workflows.sh --ack` with no trailing id, `$#` is 1, `shift 2` fails and (per bash semantics) does *not* consume any positional parameters. `|| true` swallows the error but the loop reiterates with `$1` still equal to `--ack`. The script then spins forever.
- **Fix:** consume exactly one slot unconditionally, then optionally a second: `--ack) MODE="ack"; TARGET="${2:-}"; shift; [[ -n "$TARGET" ]] && shift ;;` (and either do the missing-target check right there, or let the existing post-loop check handle it). Worth reproducing with `bash skills/workflows/workflows.sh --ack` before shipping.

### Session-summary fallback regresses crashed-pid detection

- **File:** `skills/_bg/session-summary.sh:27`
- **Severity:** major
- **What:** when `liveness.sh` isn't found (plausible during a partial sync between this repo and `~/.claude/`), the fallback is `liveness_of_state_file() { echo "running"; }`. The hook previously had its own inline pid-gone-no-marker check (see the pre-change code at the same location); that behavior is now lost in the fallback, so a crashed workflow would be reported as "running" forever until the liveness library arrives. Shipping this into an install that's mid-sync could mask a dead pipeline.
- **Fix:** either keep the original inline crashed-pid detection as the fallback body, or source-check and fail the hook quietly if the library is missing (don't silently downgrade). Given the sync-mirroring setup, the inline fallback is probably preferable.

### Sub-stage poll is a lot of machinery for a small feature

- **File:** `skills/localimplement/localimplement.sh:136-160`
- **Severity:** minor
- **What:** the poll-with-`kill -0` loop (plus `emit_sub_stage` helper) is ~25 lines implementing mid-flight sub-stage visibility — something the user will almost never see (the two reviews finish on a similar timescale and the `/workflows` refresh cadence is the user re-running the command). The logic also quietly assumes bash's script-mode child reaping behavior (verified: `kill -0` on an auto-reaped zombie returns ESRCH, so this actually works — worth a one-line comment noting the dependency). A `wait -n` loop would be strictly shorter if bash 5.1+ is acceptable, but given the poll works, the real question is whether sub-stage granularity is worth the extra surface at all vs. emitting a single "review-code" stage and relying on the existing stage_updated_at timestamp.
- **Fix:** consider dropping the sub-stage entirely and showing only `stage: review-code` (the spec's "per-reviewer sub-stage" is a nice-to-have that adds real code for marginal user value). If kept, add a short comment noting that correctness depends on bash auto-reaping background jobs in non-interactive mode.

### `--ack-all` acknowledges still-alive `stalled:*` workflows

- **File:** `skills/workflows/workflows.sh:149`
- **Severity:** minor
- **What:** `--ack-all` touches the `acknowledged` marker for `dead:*` and `stalled:*`. A stalled workflow is still running — just quiet. Hiding it may bury a slow-but-alive pipeline the user still cares about. The plan's spirit ("acknowledge dead lingering entries") points to dead-only.
- **Fix:** restrict `--ack-all` to `dead:*`. Keep an explicit `--ack <id>` escape hatch for a specific stalled one if the user decides it's truly wedged.

### `--ack <substring>` silently matches multiple workflows

- **File:** `skills/workflows/workflows.sh:124`
- **Severity:** minor
- **What:** `[[ "$id" == *"$TARGET"* ]]` matches *every* workflow whose id contains the substring, and ack's them all. Plan suggested "accept short unique prefixes"; current impl has no uniqueness guard. A user typing `--ack 20260420` on a day with multiple workflows would ack them all.
- **Fix:** collect matches, if >1 print the list and exit without touching markers; if 1, proceed. Keeps the UX forgiving but non-destructive.

### Description fallback includes flag prefixes for `ghimplement -r <ref>`

- **File:** `skills/_bg/launch.sh:97`
- **Severity:** nit
- **What:** when `--description` is omitted and the user invoked `ghimplement -r some/ref "fix thing"`, `INSTR_RAW` starts with `-r some/ref ...`, so the 60-char fallback renders as `-r some/ref fix thing` — not the useful part. The SKILL.md already instructs Claude to pass `--description`, but the fallback path will hit when a less-careful launching session forgets.
- **Fix:** if the first positional is `-r <ref>`, skip past it before slicing. Or simply slice from the last quoted arg. Low priority — the real fix is the SKILL.md guidance, which is already there.

### Kind column doesn't match plan's `[local]`/`[gh]` shape

- **File:** `skills/workflows/workflows.sh:198,226`
- **Severity:** nit
- **What:** plan §"Distinguish ..." specified "short prefix column (`[local]` / `[gh]`)". Current rendering uses `kind_short="${kind:0:5}"` → `local` and `ghimp`. `ghimp` is slightly ugly and ambiguous.
- **Fix:** map kinds explicitly: `case "$kind" in localimplement) kind_short=local;; ghimplement) kind_short=gh;; *) kind_short="$kind";; esac`.
