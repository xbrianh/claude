# Global Preferences

## Communication style

Be brief and get to the point. Skip preamble, restatement of the request, and trailing summaries of what you just did — I can read the diff. Answer the question, report the result, stop. If I want more detail or explanation, I'll ask.

## Coding style

- **Functional first.** Prefer pure functions and plain data. Reach for a class only when you genuinely need to keep state; otherwise a function is the right unit.
- **No inheritance.** Single inheritance is almost always the wrong tool — use composition. Multiple inheritance is never acceptable.
- **Short functions.** If a function doesn't fit on a screen, split it. Long functions are a smell, not a goal.
- **Few comments.** Names carry the meaning. Only comment when the *why* is non-obvious (a constraint, a workaround, a subtle invariant). Never narrate the *what*.
- **Simple over clever.** No idiosyncratic language tricks, no metaprogramming flexes, no one-liner golf. Clear names, obvious control flow, code a tired reader can follow.
- **No early optimization.** Write the straightforward version first. Optimize only when there's a measured reason to.
- **No speculative generality.** Don't add abstractions, options, or hooks for hypothetical future needs. Three similar lines beat a premature abstraction.
