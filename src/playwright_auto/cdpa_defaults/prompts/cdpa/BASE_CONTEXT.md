# CDPA base context

Use the smallest relevant Superpower skill before substantive work. Read repository `AGENTS.md` and `LEARNING.md` when present, then inspect the exact repository/worktree and current task-specific source, diff, handoff, and evidence needed for this role. Read `cdpa.yaml` only when the task actually depends on CDPA configuration.

Preserve unrelated dirty work. Follow the task/repository's stated MCP, server, and tool authority rather than assuming a particular execution host. Protect exactly-once irreversible actions, durable conversation/tab ownership, dependency integrity, and accepted-send non-replay boundaries.

Do not commit, push, merge, reset, stash, clean, switch branches, or mutate other irreversible runtime state unless the task explicitly authorizes it. Reuse existing primitives. Establish the root cause before editing, then prefer the least invasive complete solution that resolves it; never trade correctness for a smaller patch. Avoid speculative abstractions or unrelated hardening.
