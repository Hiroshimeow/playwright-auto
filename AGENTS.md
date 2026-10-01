# CDPA Agent Guide

CDPA is the durable control plane for browser-backed multi-agent workflows. Repository source plus durable runtime state are authoritative. Preserve unrelated dirty and untracked work.

## Start here

For non-trivial repository work:

1. Read `.agents/skills/hcu-work/SKILL.md`.
2. Read `.hcu/profile.md`, run `hcuw fingerprint .`, then read `.hcu/rules.md` once.
3. If the HCU baseline or evidence fingerprint drifted, reconcile only affected policy with `.agents/skills/hcu-rules/SKILL.md`.
4. Read `LEARNING.md` when present, then inspect only task-relevant source, tests, diff, handoff, and evidence.
5. Load the smallest matching repository-local skill before substantive work.

Do not repeatedly reload HCUW policy during the same session unless its files change. .specify/ keeps the standard Spec Kit workflow/tooling layout; active feature artifacts live under specs/. Explicit task constraints, repository-specific instructions, accepted specs, tests, and current implementation truth remain authoritative.

## Normal CDPA task intake

Submit ordinary work directly:

```powershell
cdpa "<complete task: repository/worktree, goal, constraints, acceptance evidence>"
```

Use optional flags only for real requirements (`--team`, `--depends-on`, `--browser`, `--upload`, `--temp`). Resume one exact existing team with:

```powershell
cdpa --team <exact-existing-team>
```

New workflow tasks start Fresh. Bootstrap branching is temporarily disabled.

## Execution model

One workflow team owns one browser endpoint/account for its lifetime. Roles do not independently migrate endpoints. Persistent roles may continue only their own durable conversation; Temporary workflows use Fresh context and never auto-reload because reload can destroy temporary document identity.

Normal Send, Resume, Wait, and Route are exact-execution-identity operations. They must never scan or snapshot every ChatGPT tab. Global strict inspection is reserved for destructive cleanup that actually requires global certainty.

Exactly-once Send is non-negotiable: persist request identity before the irreversible boundary, persist accepted provenance after it, and never resolve an accepted or ambiguous Send by blindly sending again. Manual drafts and attachments are operator-owned evidence.

Role reports are file-backed under `.plan/<team>/...`; the worker owns durable task state. PLAN alone may close the workflow with `DONE`.

## Change discipline

Use the HCUW DIRECT / BOUNDED / SPEC scope that matches the change. Prefer the existing owner and remove duplicate paths instead of adding another gate, recovery layer, state store, or abstraction.

Before editing, inspect branch/status/diff and the owning source/tests. Establish root cause before bug repair. Run the narrowest proving checks first, then broader checks only where the changed boundary warrants them. Review the final diff and run `git diff --check`.

Do not reset, stash, clean, mass-revert, switch branches, push, release, or mutate unrelated runtime state without explicit authority. Commit only when the task/workflow authorizes a checkpoint; checkpoint rules live in `.hcu/rules.md` and the operator guide in `README.md`.
