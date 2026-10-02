---
name: solo-role
description: Create and continue CDPA Solo Tasks, especially chained self-spawning Solo REVIEW/DEV/TEST workflows. Use when the user asks to create a solo task, solo role, "solo task liÃªn hoÃ n", a self-propagating review/dev chain, reuse a Solo conversation URL, or wants agents to spawn the next Solo task themselves instead of using Team Task routing. Encodes the canonical CDPA Solo API fields, endpoint requirements, idempotent child creation, chain-role selection, and closure rules so agents do not improvise task creation.
---

# Solo Role

Use CDPA Solo Tasks as a sequential chain of independent one-role tasks. Do not substitute Team Task routing when the request is specifically for Solo Tasks.

## Core contract

1. Use `workflow_mode=solo` with exactly one logical `role`.
2. Require a concrete `browser_endpoint_id`. If the operator explicitly specifies an endpoint, honor it. Otherwise consider both endpoint `9212` and endpoint `9222`, inspect their current active-tab/capacity state, and choose one that has a free slot. Do not blindly default to `9202`. The final Solo create request still pins exactly one endpoint.
3. Keep each child sequential: if the current Solo task is not independently complete, it MUST NOT stop and MUST spawn exactly one next Solo task before finishing. Never fan out parallel implementation children unless the user explicitly asks. After the child create POST is accepted, wait until that exact child task is materialized and visible on the dashboard/task API before the parent may finish; do not create a duplicate child while waiting.
4. Chain progression happens by creating a new Solo Task, not by returning a Team route such as `DEV` or `REVIEW`.
5. The child Goal is the durable chain handoff and MUST be self-sufficient. It must tell the child exactly what remains to do, summarize what the parent actually completed and verified, point to durable plan/report evidence, identify repository/worktree + current HEAD + previous Solo task, carry relevant constraints, require reading repository `AGENTS.md` and this Solo skill before work, carry the endpoint-selection instruction, and carry the chain/child-materialization rules below. Do not rely on transient chat memory.
6. Keep that handoff compact: include only facts the next role needs to continue safely. Prefer paths/IDs/hashes and concise evidence over replaying the full history or generic workflow boilerplate.
7. Preserve unrelated dirty/untracked work. Do not push unless explicitly authorized.
8. Use a final independent REVIEW for closure. If runtime evidence is required, REVIEW -> TEST -> REVIEW closure.
9. A genuine external/manual prerequisite with no legal in-task action may pause without creating useless child work. Otherwise continue the chain until independently complete.

Canonical chain line to copy verbatim into every child Goal:

`EVERY NON-FINAL SOLO AGENT MUST SPAWN EXACTLY ONE NEXT SOLO TASK BEFORE FINISHING, AND MUST CONTINUE THE SOLO REVIEW/DEV/TEST CHAIN UNTIL THE WORK IS INDEPENDENTLY VERIFIED COMPLETE. DO NOT USE TEAM TASKS OR ROUTE-WORKER HANDOFFS FOR THIS CHAIN.`

## Choose the first role

- Existing plan needs independent challenge before coding -> `REVIEW`.
- Clear implementation task with accepted plan/spec -> `DEV`.
- Runtime-only or acceptance-only proof -> `TEST`.
- Audit-only request -> `AUDIT`.
- Use `PLAN` only when the Solo task itself must produce the plan. Do not add a PLAN role merely because normal Team workflow begins with PLAN.

For the common chained engineering flow, prefer:

`REVIEW(plan) -> DEV -> REVIEW(code) -> [DEV -> REVIEW as needed] -> TEST if runtime proof is needed -> REVIEW(closure)`

## Create the task

Prefer the bundled helper instead of hand-writing the API payload:

```powershell
.venv\Scripts\python.exe .skills\solo-role\scripts\spawn_solo_task.py `
  --role REVIEW `
  --task "Review the implementation plan and spawn the next Solo DEV." `
  --goal $goal `
  --team "my-change-plan-review-20260927" `
  --browser 9222 `
  --repository "E:\git-project\target"
```

For an existing persistent ChatGPT conversation, add:

```powershell
  --conversation-url "https://chatgpt.com/<optional-project-path>/c/<conversation-id>"
```

The helper sends UTF-8 JSON and an idempotency key. A repeated identical invocation must not create a duplicate child.

Read [references/cdpa-solo-contract.md](references/cdpa-solo-contract.md) before creating or repairing a Solo chain when API fields, CLI behavior, long-lived conversation reuse, child-role selection, or verification is material.

## Child Goal is the chain protocol

The prompt passed to the next Solo task is the chain's durable state. Every non-final parent owns writing it. The child must be able to continue correctly from that prompt plus repository truth without access to the parent's transient chat.

Before spawning, re-check current repository/runtime truth and write a compact handoff containing:

- `Repository` / worktree and current `HEAD`.
- `Read first`: repository `AGENTS.md`, `.skills/solo-role/SKILL.md`, and any task-specific skill named by the repository.
- `Previous Solo`: exact parent task ID/team and durable plan/report/handoff path when one exists.
- `Completed`: only the parent's material changes/findings plus concise verification evidence. Do not claim unverified work.
- `Remaining`: the complete concrete work the child must perform, including known defects/open acceptance evidence. This is the child's primary task specification.
- `Role`: why this child is DEV/REVIEW/TEST/etc. and what that role must prove before it may close or spawn the next role.
- `Constraints`: host/repository safety, no push/commit unless authorized, preservation of unrelated dirty work, and any task-specific restrictions.
- `Endpoint`: explicitly tell the child to choose between 9212 and 9222 from current free capacity unless the operator pinned one; pass exactly one concrete endpoint when spawning its child.
- `Chain rule`: if anything remains unfinished or independently unverified, spawn exactly one next Solo task with another self-sufficient handoff; never substitute a Team route.
- `Materialization rule`: after spawning, poll the exact child task ID until it is visible in the dashboard/task API; only then may the parent finish.

Use this compact template:

```text
Repository: <absolute repository/worktree>
HEAD: <current hash>
Read first: AGENTS.md; .skills/solo-role/SKILL.md; <task-specific skill if any>
Previous Solo: <task id/team>
Plan/report: <durable path(s), if any>

Completed: <concise verified work + evidence>
Remaining: <complete exact responsibility for this child>
Role: <role-specific responsibility / closure evidence>
Constraints: <only relevant safety/task constraints>
Endpoint: for the next Solo child, inspect 9212 and 9222 and choose one with free capacity unless operator-pinned; pass one concrete endpoint.

CHAIN: If work or independent verification remains, you MUST spawn exactly one next Solo task before finishing. Its Goal must again contain the current HEAD, what you completed and verified, everything still remaining, durable evidence paths, constraints, Read-first instructions, endpoint rule, and this chain rule. Do not use Team routing for the chain.
MATERIALIZATION: After child POST is accepted, do not finish yet. Poll that exact child task ID until it is materialized and visible in the dashboard/task API. A transient 404 means wait, not spawn another child.
CLOSURE: Only a final independent closure role with all required acceptance evidence may end the chain without a child.
```

Keep `Completed` and `Remaining` information-dense. Link durable reports/plans instead of pasting their contents. Never forward stale conclusions as truth: the next role re-checks repository/runtime evidence appropriate to its responsibility.

## Verification after spawn

Treat the POST response containing a `task_id` and `queued`/accepted command as successful submission. It proves the child create command was accepted, so do not retry with a new idempotency key or create another child.

A non-final parent is not allowed to finish yet. Poll the exact child until it is materialized and visible through the dashboard/task API:

```powershell
Invoke-RestMethod http://127.0.0.1:9225/api/tasks/<task-id>
```

Transient 404/not-yet-visible means wait and check the same task ID again. Only after the exact child is visible may the parent record the child task ID/team in its report/handoff and finish.

## Never do these

- Do not send Team-only fields such as `roles`, `new_roles`, `new_all`, `temporary`, `required_browser_endpoint_id`, `depends_on_task_ids`, or uploads in a Solo create request.
- Do not omit `browser_endpoint_id`.
- Do not use `route=DEV/REVIEW/TEST` as the chaining mechanism for a Solo task.
- Do not create a second child because the first child is still `queued` or temporarily returns 404 before the worker applies the create command.
- Do not reuse an old task's conclusions as proof; every REVIEW/TEST verifies current repository/runtime truth independently.
- Do not make the child Goal depend on transient chat memory alone; include durable plan/report paths and exact IDs/hashes.

