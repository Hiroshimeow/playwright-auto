Return exactly one valid JSON object:
{"route":"<closed enum from the current RouteContract>","handoff":".plan/<team>/<physical-role>_turn<N>_<task-id>.md"}

Only the current task's `closing_role` may use `DONE`. The handoff must reference the exact role report required by the current turn.
