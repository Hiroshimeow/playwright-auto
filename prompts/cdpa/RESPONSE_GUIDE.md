Return exactly one valid JSON object, find other suitable MCP tools if needed.:
{"route":"PLAN|DEV|TEST|REVIEW|AUDIT|PAUSE|DONE","handoff":".plan/<team>/<physical-role>_turn<N>_<task-id>.md"}

Only PLAN may use `DONE`. The handoff must reference the exact role report required by the current turn.
