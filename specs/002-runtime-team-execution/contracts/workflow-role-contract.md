# Contract: Workflow Role Selection and Closing Authority

## Owner boundary

Legacy workflow role validation owns canonical role selection, task initial role, and DONE route authority. Runtime-v2 may request roles and start role, but legacy owners must enforce the final task manifest invariants.

## Role set invariants

- A Team Task role set may omit PLAN.
- A Team Task role set must include PLAN or REVIEW.
- A Team Task role set must reject duplicate or unknown roles.
- A Team Task start role must be selected in the role set.
- A Team Task must not silently add PLAN when PLAN was not selected.

## Initial role rule

- The task starts from the selected `start_role` requested through runtime-v2.
- Legacy `POST /api/tasks` and `TaskStore.create_task` accept an optional Team Task `start_role`; it must be one of the selected roles, and it is rejected for Solo Task.
- When `start_role` is omitted, the task starts at the first selected role in canonical order.
- If legacy canonical ordering is applied, it must preserve the requested start role as active role and first hop target.
- There must be no hardcoded `PLAN` initial role for Team Tasks.

## Closing role rule

- If PLAN is selected, PLAN is the only selected role allowed to route DONE.
- If PLAN is absent and REVIEW is selected, REVIEW is the only selected role allowed to route DONE.
- If neither PLAN nor REVIEW is selected, task creation must fail.
- Non-closing roles can route only to selected roles or PAUSE when allowed by existing route policy.

## Prompt contract

Prompt route instructions must reflect the selected roles:

- DEV+REVIEW team DEV prompt may route to DEV or REVIEW, not DONE.
- DEV+REVIEW team REVIEW prompt may route to DEV, REVIEW, or DONE.
- PLAN-containing team preserves PLAN as DONE authority.
- Route PLAN must not be offered to teams without PLAN, except accepted-send reconciliation may route to the feature-defined closing role.

## Dashboard/API contract

- Dashboard role selector must not force PLAN selected.
- Reuse/team role lock must show actual selected roles.
- CLI `--roles DEV,REVIEW` must submit a Team Task with those roles.
- Taskless resume must reject `--roles`.
