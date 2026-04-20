# Review (sonnet)

## Summary

The implementation is solid overall: the atomic `jq | mv` pattern in `set-stage.sh`, the US-separator approach to avoid tab-collapse, and the fallback/degrade strategy in `session-summary.sh` are all well-executed. A handful of concrete issues follow — mostly minor/nit, no blockers. The most noteworthy correctness issue is that `--ack` and `--ack-all` accept `stalled` workflows (which may still be alive), diverging from the plan's "dead/finished only" spec. The liveness library also uses `@tsv` + `IFS=$'\t'` internally for three fields while the rest of the codebase documents why that pattern is unsafe — fine today, a latent trap if a field is added later.

## Findings

### `--ack` and `--ack-all` acknowledge `stalled` workflows
- **File:** `skills/workflows/workflows.sh:110` and `skills/workflows/workflows.sh:139`
- **Severity:** minor
- **What:** Both `--ack <id>` and `--ack-all` accept `stalled:*` liveness, touching `acknowledged` and permanently hiding the workflow from future `/workflows` runs. A stalled workflow (pid still alive, log just quiet) may still be producing output — acknowledging it hides it while it runs. The plan (Task 6) says ack applies to "dead/finished workflows" only; stalled is not dead.
- **Fix:** Remove `stalled:*` from the `case` pattern at line 110 and the `if` condition at line 139. If users want to dismiss a stalled workflow they should `--ack` it after it becomes `dead:crashed`.

### Misleading `--ack` skip message
- **File:** `skills/workflows/workflows.sh:116`
- **Severity:** nit
- **What:** `"skipping $id: $live (use --ack-all to force-acknowledge nothing alive)"` is grammatically ambiguous — "nothing alive" can be parsed as "there is nothing alive to acknowledge", which is the opposite of the intent. The sentence also falsely implies `--ack-all` can force-acknowledge a running workflow (it can't — `--ack-all` also skips `running`).
- **Fix:** Replace with something like `"skipping $id ($live is still running; only finished/dead workflows can be acknowledged)"`.

### `liveness.sh` uses `@tsv` + `IFS=$'\t'` — inconsistent with US-separator approach
- **File:** `skills/_bg/liveness.sh:37-40`
- **Severity:** nit
- **What:** `liveness_of_state_file` reads three fields via `jq ... | @tsv` with `IFS=$'\t'`. The same commit documents (in `session-summary.sh` and `workflows.sh`) that tab-as-IFS-whitespace collapses consecutive empty fields silently. The three fields here (`status`, `pid`, `exit_code`) are safe today — `status` is always present, and the collapsed-empty behavior for null pid/exit_code is harmless. But it contradicts the rationale documented elsewhere and is a latent trap if a fourth field is ever appended without adjusting the separator.
- **Fix:** Adopt the same `join("\u001f")` + `IFS=$'\x1f'` pattern used in `session-summary.sh` and `workflows.sh`.

### `emit_sub_stage` inner function leaks into global scope
- **File:** `skills/localimplement/localimplement.sh:435`
- **Severity:** nit
- **What:** `emit_sub_stage` is defined inside `run_dual_review()`. Bash does not scope inner function definitions — after `run_dual_review` returns, `emit_sub_stage` persists in the global function namespace for the lifetime of the shell. Harmless in practice (the name is unique and the function is idempotent), but it breaks the encapsulation assumption and could confuse a future reader who grep-searches for where `emit_sub_stage` is defined.
- **Fix:** Either move the definition outside `run_dual_review` (making it a top-level helper, parallel to `set_stage`) or name it `_run_dual_review_emit_sub` to make the scope intent clear.

### Double `jq` fork per workflow in `session-summary.sh`
- **File:** `skills/_bg/session-summary.sh:79`
- **Severity:** nit
- **What:** Before this change, the hook issued one `jq` call per workflow dir. Now it issues two: one for the outer `IFS=$'\x1f' read` and one inside `liveness_of_state_file`. The original code had a comment specifically calling out `fork+exec` expense on macOS. For 1–5 workflows this is negligible, but worth knowing in case the workflow dir count grows.
- **Fix:** No action required now. If performance becomes a concern, `liveness_of_state_file` could accept pre-parsed fields as arguments instead of re-reading the file.

### `--ack <id>` substring match can acknowledge multiple unintended workflows
- **File:** `skills/workflows/workflows.sh:101`
- **Severity:** nit
- **What:** `[[ "$id" == *"$TARGET"* ]]` — if `TARGET` is a common substring (e.g., a date fragment like `20260420`), multiple workflow dirs match and all are acknowledged simultaneously. The output does list each acknowledged id, so the user sees what happened, but the intent was almost certainly to match one specific workflow.
- **Fix:** After accumulating matches, if `matched > 1` print a warning: `"ambiguous id '$TARGET' matched $matched workflows — use a longer prefix to be specific"`. Alternatively, require unique match before acting (print candidates and exit 1 if >1 match).
