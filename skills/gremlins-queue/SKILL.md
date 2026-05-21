---
name: gremlin-queue
description: Run a background `gremlins queue run` and interleave launch+land pairs as the user names units of work. Use when the user wants to queue gremlin work conversationally, asks to "queue X" or "queue those", or wants the runner kicked off for the session.
argument-hint: [<optional first unit to queue>]
---

You are operating the gremlin queue on behalf of the user. The model is: a single long-lived `gremlins queue run` process executes queued commands serially in the background; you append paired `launch` + `land` commands as the user names work.

## Ground rules

- **One runner per session.** Start `gremlins queue run` once. Do not re-spawn it per task. If it's already running, leave it. Check with `gremlins queue list --json` (the top-level state includes `runner_active`) or just try `gremlins queue run --detach` — it refuses if one is active.
- **One unit of work = one launch + one land.** Never collapse into a single command. Never skip the land step.
- **Operator-supplies-id.** Pick a short kebab-case `--gremlin-id <id>` per unit and pass the same id to both commands. The queue runner doesn't know gremlin ids — the explicit `land <id>` is how the right gremlin gets landed before the next launch starts.
- **Don't expand scope.** "Queue A and B" means A and B. Don't sneak in adjacent work the user didn't name.
- **Default pipeline is `gh-terse`** for issue-driven work in this project, unless the user names a different pipeline or the unit is clearly a PR extension / boss chain / local plan.

## The canonical pattern

For each unit of work the user names:

```
gremlins queue add "gremlins launch gh-terse --plan '#<issue>' --gremlin-id <id> --wait"
gremlins queue add "gremlins land <id>"
```

Picking ids: short, kebab-case, descriptive (e.g. `fix-cache-ttl`, `pr-697-retry`). Must match `[A-Za-z0-9_-]+`.

The `--wait` on launch is important: it makes the queued launch command block until the gremlin process exits, so the queue doesn't move on to `land` while the gremlin is still working.

## Starting the runner

Two equivalent options:

- **Detached** (preferred — survives if the chat session ends): `gremlins queue run --detach`. Writes pid to `runner.pid` and logs to `runner.log` under the queue state dir. Stop with `gremlins queue stop`.
- **Foreground-via-Bash-background**: launch `gremlins queue run` via Bash with `run_in_background=true` and consume its stdout to react to `queue: running <stem>` / `queue: done <stem>` / `queue: failed <stem>` events live.

Shortcut for the very first item: `gremlins queue add --run "<cmd>"` queues the command and then `execvp`s into `queue run` in the same shell. Only useful if you're not already detached.

## Flow

1. **Ensure the runner is up.** If not, start it (see above).
2. **If `$ARGUMENTS` names a unit**, queue it immediately as a launch+land pair.
3. **Then wait for the user.** As they name more units, append more pairs. When they ask "what's queued?" or "what's running?", use `gremlins queue list` (human) or `gremlins queue list --json` (machine). `--watch [SEC]` gives a live view if useful.
4. **On failure** the runner STOPS — `queue run` exits with code 1 the moment any queued command returns non-zero. Tell the user, read the failed gremlin's log/state, and decide together whether to `rescue`, `resume`, file a new issue, or fix externally and then `gremlins queue requeue` (moves `failed/` back to `pending/`) before restarting the runner.

## Useful subcommands

- `gremlins queue list [--watch [SEC]|--json]` — show all items by bucket (pending / running / done / failed). `--json` is for programmatic checks.
- `gremlins queue requeue [--done]` — move failed items (and optionally done items) back to pending.
- `gremlins queue clear [--failed|--done|--pending|--purge|--item STEM]` — remove items. Default removes both done and failed. `--purge` empties all four buckets.
- `gremlins queue set-state {pending|running|done|failed} --item STEM` — manual bucket move; for surgical recovery.
- `gremlins queue stop` — SIGTERM the detached runner via `runner.pid`.

## What this skill does NOT do

- Doesn't pick what to work on — the user names units.
- Doesn't write code or plans inline.
- Doesn't poll `queue list` in a tight loop — react to the event stream from `queue run`, or use `--watch` for a human view.
- Doesn't land gremlins ad-hoc outside the queue while the runner is active — the queued `land` is the single point of landing.

## Reference

`gremlins prompt-for-assistant` prints the full operator guide if you need a refresher on subcommands, state locations, or the four collaboration roles.
