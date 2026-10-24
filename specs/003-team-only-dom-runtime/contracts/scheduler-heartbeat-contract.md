# Contract: Scheduler and Heartbeat Replacement

## CronJob

CronJob is deterministic runtime state, not an LLM agent. It creates ordinary TeamTasks using a deterministic idempotency key derived from cron id + scheduled occurrence.

## Heartbeat recovery

Stale worker/endpoint/task-role heartbeat recovery creates an ordinary TeamTask with a deterministic recovery idempotency key. It never emits INDEPENDENT_AGENT_TRIGGER_CONTEXT.

## Legacy Independent Agent

Runtime-v2 cannot create or activate Independent Agent work. Existing legacy records remain disabled/hidden or migration-only after reload.

## Dependencies

Dependency graph edges refer only to TeamTask ids. ProviderJob ownership does not create a second dependency-node type.

## Scheduling/recovery episode rules

- Cron does not replay an unbounded backlog of missed historical occurrences after downtime; scheduling resumes from the next eligible occurrence unless an explicit bounded catch-up policy is configured.
- Heartbeat recovery uses a deterministic stale-episode key derived from the monitored subject and the beginning of that stale episode.
- Repeated checks for the same stale episode reuse the same recovery TeamTask/idempotency key.
- A recovery TeamTask must not recursively create another recovery TeamTask for the same stale episode.
