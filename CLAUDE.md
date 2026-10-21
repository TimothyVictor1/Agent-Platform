# CLAUDE.md

@AGENTS.md

## Extra notes for Claude Code

- You own the hardest, most cross-cutting parts of this project (contracts, agent loop, coordination protocol, scheduler, supervisor, safety) and you review OpenCode's branches before they are merged. Think about concurrency, failure modes and restarts on every change.
- Plan before coding on any task larger than a few files: write a short plan (files, functions, tests) at the top of your work, then implement.
- When you review another branch, use the review checklist in `TASKS.md` §R and report findings as a numbered list ordered by severity. Do not rewrite the branch; propose exact fixes.
- Keep `docs/architecture.md` and `docs/coordination-protocol.md` in sync with what you build.
