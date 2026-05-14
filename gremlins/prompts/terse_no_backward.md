Backward compatibility is not a concern. Prefer a terse code style and small functions. Touched code should be opportunistically simplified.

Compat-adjacent things also do not belong in this codebase, even though they aren't strictly backward-compat:

- Tombstone tables / `_REMOVED` maps that translate retired command/flag/field names into "use X instead" error messages. The standard "unknown subcommand" / `argparse` error is sufficient — let removed names fall through.
- "Friendly" error messages for inputs that no longer exist. If the input is gone, the generic error is the correct error.
- `_legacy_*` / `_old_*` / `_v1_*` helpers kept around "in case someone still calls them."
- Re-exports or aliases preserving an old import path after a rename.
- Any code whose justification is "so existing X keeps working" where X is a thing that already changed shape.

When you find one of these in the codebase during unrelated work, delete it as part of that work — don't preserve it because it's not the focus.
