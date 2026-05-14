A code review of the most recent implementation follows. For every actionable finding you agree with, make the fix in the code. For findings you disagree with or choose to skip, note them briefly in your final summary with a reason.

---
**Detail reviewer** (model: {model}):

{text}

---

{address_commit_instr}

## Bail markers (running under a gremlin pipeline)

If you cannot safely complete your task, write a bail marker before finishing — do not make speculative changes when bailing:

- Task involves **secrets** (credential management, API keys, encryption material): `{bail_command} secrets "<one-line reason>"`
- Any other reason you cannot proceed: `{bail_command} other "<one-line reason>"`

Do not write a bail marker if you successfully completed your task — just exit normally.

End with a short summary (to stdout) of: what you addressed, what you skipped and why.
