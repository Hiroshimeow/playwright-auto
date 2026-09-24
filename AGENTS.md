# CDPA Agent Guide

This repository is the CDPA control plane. Repository source and durable runtime state are authoritative. Preserve unrelated dirty/untracked work.

## 1. Normal task intake

When discussion becomes actionable, submit one normal workflow task directly through `cdpa`.

```powershell
cdpa "<complete task: repository/worktree, goal, constraints, acceptance evidence>"
```

That is the default command. Do not put Manual Dispatcher or an Independent Agent in the critical path merely to re-dispatch normal work.

Prefer task text over flags. Put the target repository/worktree path in the task text; the API infers it. Use `--repository` only when inference is impossible or must be overridden.

### Optional flags

Use only when the request actually requires them:

```powershell
cdpa "<task>" --team readable-name
cdpa "<task>" --depends-on <parent-task-id>
cdpa "<task>" --browser <endpoint>
cdpa "<task>" --upload <file>
cdpa "<task>" --temp
```

- `--team`: optional readable base name. Omit by default.
- `--depends-on`: durable dependency. Repeat only for real dependencies.
- `--browser`: pins the entire workflow team to one explicit endpoint/account. Omit for normal pool placement.
- `--upload`: explicit file context.
- `--temp`: forces Temporary Chat when the selected endpoint is not already temporary by default.

Do not use public bootstrap flags. New workflow tasks always start with Fresh context. Bootstrap branching is temporarily disabled.

Resume an existing exact team with:

```powershell
cdpa --team <exact-existing-team>
```

Resume preserves the same task, team, endpoint, active hop, request identity, and report history.

## 2. One team = one browser endpoint

A workflow team uses one browser endpoint/account for its lifetime.

- When `--browser` is omitted, the pool chooses a usable endpoint once at first placement.
- After first placement, the selected endpoint is durably pinned for every role in that team.
- PLAN/DEV/REVIEW/TEST/AUDIT must not independently migrate to different endpoints.
- If the pinned endpoint is full, cooling down, offline, or unavailable, the task waits. It does not spill roles onto another endpoint.
- An explicit `--browser` pins the team immediately.

The endpoint registry owns endpoint defaults. In particular, an endpoint with `temporary_default=true` makes an unoverridden workflow Temporary when that endpoint is selected.

## 3. Temporary Chat

Temporary workflows are intentionally minimal:

- Fresh context only.
- No bootstrap.
- No automatic F5/reload recovery. Reload may destroy the temporary document identity.
- Recovery uses the exact durable role/page/conversation state or waits for operator action.
- Persistent Project grouping is skipped for Temporary chats.

Do not add periodic refresh logic to Temporary workflows.

## 4. Role/page ownership

Normal Send/Resume admission is exact-role scoped.

Each new role has an immutable `binding_id`. Runtime ownership is anchored by:

- task-level browser endpoint,
- exact `page_id`,
- role `binding_id`,
- `task_id` and physical role as durable owner metadata.

`team`, `task_id`, role, page ID, and conversation URL remain useful audit evidence, but unrelated ChatGPT tabs are not an admission dependency.

### Critical rule

Normal Send, Resume, Wait, and Route must never scan or snapshot every ChatGPT tab on an endpoint.

An unrelated or unbound ChatGPT tab that is slow, stale, or unreadable must not block another task.

Global strict scanning is reserved for destructive operations that actually need global certainty, such as Clear Team / close-owned-tabs cleanup.

Duplicate exact bindings or a conflict on the target role remain fail-closed.

## 5. Send boundary

Exactly-once Send protection remains mandatory.

The normal sequence is:

1. acquire/recover the exact role page;
2. verify exact ownership;
3. verify composer/attachments/transcript baseline;
4. persist durable request identity;
5. fill the exact draft;
6. verify Send-ready state;
7. click Send once;
8. persist accepted user-message/turn identity;
9. wait for and validate the response.

Never solve ambiguous accepted-send state by blindly sending again.

Manual drafts and attachments are operator-owned evidence and must not be overwritten.

## 6. Context

New workflow roles start Fresh. Reuse only the same role's own durable conversation when continuing that task.

Bootstrap donors, source bootstrap URLs, cross-endpoint branching, and bootstrap prewarm are not part of normal workflow execution while bootstrap is disabled.

Do not infer tool/device authority from a Windows/Linux path. Honor explicit task tool requirements and actual session capabilities.

## 7. Projects

For persistent workflow conversations, CDPA automatically groups each accepted conversation into the ChatGPT Project named for the execution repository on that endpoint/account.

- Reuse an existing same-name Project on that account when present.
- Otherwise create it.
- Attach the conversation after accepted Send; do not wait until terminal routing.
- Project grouping is organizational metadata. Failure to group must not block substantive work.

Temporary chats are not added to persistent Projects.

## 8. Workflow routing and reports

Normal localized flow is:

```text
PLAN -> DEV -> REVIEW -> PLAN -> DONE
```

Add TEST/AUDIT only when the task actually needs them.

Role responses follow the repository file-backed report contract under `.plan/<team>/...`. Preserve accepted Send provenance and report history across recovery.

PLAN owns final workflow closure. Do not mark DONE with a known fixable in-scope regression.

## 9. Operator controls

Use supported durable controls rather than editing manifests or steering browser state manually:

- Pause
- Resume
- Retry hop where explicitly retryable
- Restart role
- New chat
- Open tab
- Route PLAN
- Change goal
- Stop
- Clear Team

Resume is continuation of the same durable task, not a new task.

## 10. Engineering rules

Before editing this control repository:

- inspect status/branch/diff;
- read relevant current source and tests;
- preserve every pre-existing dirty/untracked file;
- do not reset, stash, clean, mass-revert, or overwrite unrelated changes;
- prefer removal of duplicate gates/state machines over adding another recovery layer;
- keep non-destructive hot paths local to the exact execution identity;
- keep destructive cleanup fail-closed;
- run focused regressions plus relevant broader tests;
- run syntax checks and `git diff --check`.

When code and old documentation disagree, current verified code plus this file are authoritative; update stale documentation in the same repair.

<!-- HCUW:BEGIN -->
## HCUW (managed)

For non-trivial engineering work, use the repository-local `hcu-work` skill:
`.agents/skills/hcu-work/SKILL.md`.

Read `.hcu/profile.md` and `.hcu/rules.md` once per agent session. If
`.hcu/rules.md` does not exist, initialize it before substantive work by
following `hcu-work`.

When the user asks to change durable engineering policy or when HCUW reports
rule drift, use:
`.agents/skills/hcu-rules/SKILL.md`.

After the files are loaded, do not repeatedly reload them every turn unless they
changed. Repository-specific instructions, explicit user constraints, accepted
specs, tests, and implementation truth remain authoritative.

HCUW owns only this marked block. Existing AGENTS.md content before and after
the block is preserved.
<!-- HCUW:END -->
