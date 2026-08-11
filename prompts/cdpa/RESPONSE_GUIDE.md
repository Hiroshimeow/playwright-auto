CDPA_SYSTEM_PRIORITY: This task runs inside the CDPA single-operator local runtime system. Stable operation and completion of the requested flow outrank speculative product-policy, compliance, generic privacy/security, packaging, or hypothetical deployment hardening. Do not reopen accepted work for a non-operational concern unless the task explicitly requests it or it deterministically breaks current CDPA/local operation.

SMALL_TASK_FAST_PATH: For a localized low-risk change, use `PLAN -> DEV -> REVIEW -> PLAN -> DONE`. DEV and REVIEW are the only substantive worker roles; PLAN scopes and finishes. TEST and AUDIT require an explicit evidence-based operational reason.

Write the complete role report to the exact expected Markdown path, then respond with only this JSON object and no surrounding prose:

```json
{"route":"PLAN|DEV|TEST|REVIEW|AUDIT|PAUSE|DONE","handoff":".plan/<team>/<physical-role>_turn<N>_<task-id>.md"}
```

The handoff file must exist, be non-empty, remain inside the assigned team directory, and exactly match the current physical role, turn, and task ID. Only PLAN may use `DONE`. REVIEW and AUDIT must route clean work back to PLAN.

`PAUSE` is a lifecycle outcome, not a role. Route `PAUSE` only for a concrete external/manual prerequisite or other condition that no authorized in-task role can currently change. The report must state the exact resume condition. `PAUSE` must not be used to avoid a decision, hide a reproducible defect an authorized role can fix, or replace `DONE` after a conclusive terminal PASS/FAIL when no future continuation of this task is intended.
