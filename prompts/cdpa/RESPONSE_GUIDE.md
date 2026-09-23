Return exactly one valid JSON object:
{"route":"PLAN|DEV|TEST|REVIEW|AUDIT|PAUSE|DONE","handoff":".plan/<team>/<physical-role>_turn<N>_<task-id>.md"}

Only PLAN may use `DONE`.
A temporary MCP/device/browser/tool disconnect, update, restart, or reconnect is not a PAUSE prerequisite: retry the same capability for 3-5 minutes with bounded 10-30 second backoff while preserving the same task/role/conversation; PAUSE only if the bounded window expires and a concrete external/manual prerequisite still remains.
