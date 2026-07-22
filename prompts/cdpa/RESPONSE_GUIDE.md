Write the complete role report to the exact expected Markdown path, then respond with only this JSON object and no surrounding prose:

```json
{"route":"PLAN|DEV|TEST|REVIEW|AUDIT|DONE","handoff":".plan/<team>/<physical-role>_turn<N>_<task-id>.md"}
```

The handoff file must exist, be non-empty, remain inside the assigned team directory, and exactly match the current physical role, turn, and task ID. Only PLAN may use `DONE`. REVIEW and AUDIT must route clean work back to PLAN.
