Three independent code reviews of the most recent implementation follow. The reviewers have different lenses by design, so their findings will mostly be complementary rather than overlapping — still deduplicate where they do overlap. For every actionable finding you agree with, make the fix in the code. For findings you disagree with or choose to skip, note them briefly in your final summary with a reason.

---
**Holistic reviewer** (model: {model_a}):

{text_a}

---
**Detail reviewer** (model: {model_b}):

{text_b}

---
**Scope reviewer** (model: {model_c}):

{text_c}

---

{address_commit_instr}{bail_section}

End with a short summary (to stdout) of: what you addressed, what you skipped and why.
