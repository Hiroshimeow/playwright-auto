# Data Model: Runtime Team Execution

## Runtime Task Submission

Represents one operator request to create a real CDPA Team Task through runtime-v2.

Fields:
- `task`: non-empty task text.
- `team`: non-empty team base/name requested by the operator.
- `browser`: non-empty browser endpoint id, defaultable by runtime-v2 policy.
- `roles`: non-empty ordered list of selected workflow roles.
- `start_role`: role that must be present in `roles`.
- Forbidden fields: Solo/Independent fields such as `workflow_mode=solo`, `solo_role`, `role`, `conversation_url`.

Validation:
- `roles` must contain no duplicates.
- `start_role` must be included in `roles`.
- `roles` must include PLAN or REVIEW.
- Request must fail before legacy enqueue when validation fails.

## Runtime Task Record

Represents runtime-v2 evidence for a submitted task.

Fields:
- `runtime_task_id`: runtime-v2 identifier.
- `legacy_task_id`: legacy CDPA task id returned by legacy API/CLI.
- `legacy_command_id`: legacy command id for enqueue/create.
- `team`, `browser`, `roles`, `start_role`.
- `status`: runtime-v2 submission status and latest projected status when available.
- `submission_status`, `latest`, `active_role`, `block_code`, `legacy`: read-only projection fields added by `GET /api/tasks`.
- `created_at`, `updated_at`.
- `submit_stdout`, `submit_stderr`, `submit_command` or request summary.

Relationships:
- One Runtime Task Record links to one legacy CDPA task when submission succeeds.
- Runtime-v2 may list old records, but legacy task state remains owned by legacy CDPA.

## Workflow Role Set

Represents selected roles for one Team Task.

Fields:
- `roles`: canonical selected roles.
- `start_role`: first active role for task execution.
- `closing_role`: derived role authorized to route DONE.

Rules:
- Closing role is PLAN when PLAN exists.
- Closing role is REVIEW when PLAN is absent and REVIEW exists.
- No closing role means the team is invalid.

## Accepted Send Evidence

Represents proof that a prompt crossed the accepted-user boundary exactly once.

Fields:
- `request_id`, `ledger_path`.
- `receipt.user_message_id`.
- `receipt.binding.page_id` and role binding.
- `conversation_url` and optional canonical `conversation_id`.
- Observed message list containing the accepted user message.

State transitions:
- `sending` -> `sent` -> `waiting` when accepted.
- `waiting` with provisional URL and exact user turn remains observing.
- `waiting` with canonical URL enriches receipt/ledger.
- `waiting` without exact page/user provenance blocks and exposes reconciliation.

## Audit Source State

Represents one bounded source displayed by runtime-v2 audit.

Fields:
- `source`: actions, dom, mutation, or listen.
- `status`: active, idle, inactive, degraded, or unavailable.
- `message`: current human-readable summary.
- `updated_at`.
- `last_event_id`.
- `payload`: bounded structured details.

Rules:
- Audit state is projection-only.
- Audit state must not mutate legacy task manifests or route decisions.

## Legacy Bridge Config

Represents a generated runtime-v2 bridge configuration for starting or using legacy CDPA.

Fields:
- `repositories.allowed_roots`: includes the runtime-v2 repository root.
- `independent_agents.seed_builtins`: must be false for runtime-v2 spawned work.
- Existing legacy settings copied only as needed.

Rules:
- Stored under `.runtime-v2/task-runner/`.
- Must not overwrite or patch `cdpa.yaml`.
