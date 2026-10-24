# Data Model: Team-only DOM Runtime

## TeamTask

- `task_id`: immutable task id.
- `team`: operator-visible team name.
- `task_text`: original instruction.
- `provider_id`: selected/default provider id for the task; per-role provider policy may override through ProviderInvocation in later gates.
- `submitted_roles`: raw submitted role keys.
- `roles`: normalized initial roles, non-empty, ordered, duplicate-free.
- `route_targets`: optional additional reachable role keys declared by operator.
- `reachable_roles`: immutable creation-time closure of roles, route_targets, and semantic-route destinations.
- `route_contract_id`: immutable id/hash of the creation-time semantic-route contract snapshot.
- `start_role`: member of initial roles; defaults to `roles[0]`.
- `closing_role`: member of initial roles; defaults to `roles[0]`.
- `active_role`: current materialized role or null.
- `depends_on_task_ids`: ordered unique TeamTask ids only.
- `max_routes`: positive limit for non-terminal role-to-role transitions.
- `max_route_repairs`: small non-negative limit per active role turn for malformed/out-of-enum route repair attempts; reset only after valid route commit to another role or task close.
- `route_seq`: number of committed non-terminal role transitions.
- `self_route_policy`: `forbidden` or `bounded(max_self_routes=N)`; bounded counts are cumulative per role for the task lifetime, and every self-route increments route_seq and consumes max_routes.
- `self_route_counts`: cumulative task-lifetime count per role.
- `status`: INBOX, RUNNING, WAITING, BLOCKED, PAUSED, DONE, STOPPED.
- `block_code`: durable machine-readable BLOCKED reason; route-limit control eligibility reads this field.
- `block_retryable`, `block_reason`: durable block metadata. Route/repair exhaustion uses `block_retryable=false`; recovery uses limit-increase + Resume, not Retry.
- `created_at`, `updated_at`.

### Validation

- Role keys canonicalize by trim + uppercase + spaces/hyphens to `_`.
- Canonical key grammar: `^[A-Z][A-Z0-9_]{0,31}$`.
- Keys colliding with terminal route tokens, reserved control route tokens such as `WAIT`, TaskStatus tokens, or dependency projection tokens `WAITING_DEPENDENCY` / `DEPENDENCY_FAILED` are reserved.
- `start_role` and `closing_role` belong to initial roles.
- `reachable_roles` is finite, bounded by configured `max_reachable_roles`, and immutable after creation.
- Public runtime-v2 input contains no legacy Solo/Independent task mode fields.

## RoleSlot

- `role_key`: member of reachable_roles.
- `state`: declared or materialized.
- `system_prompt_ref`: prompt/config material for this role.
- `provider_policy`: provider selection data.
- `conversation_binding_id`: optional before Gate C or for non-browser providers. Under Gate C, every browser-backed role turn that can Send MUST have/project a writer binding before Send so ordinary TeamTask traffic shares the same lease namespace as adopted URL and Direct Chat. Existing/resumed conversations acquire canonical writer identity before Send; only genuinely new conversations may begin provisionally.
- `materialized_at`: null until first validated route-in.

### Invariant

Recording a route, incrementing route_seq, and materializing a new role slot are one durable operation before any Send intent for that role.

## RouteContract

- `current_role`.
- `reachable_roles`.
- `closing_role`.
- `allowed_routes`.
- `semantic_routes`.
- `route_contract_id`.
- `max_route_repairs` and current-turn repair count.
- `block_code`: `ROUTE_CONTRACT_DRIFT`, `ROUTE_CONTRACT_INVALID`, or other bounded route failure code.
- `self_route_policy`.
- `max_routes`, `route_seq`.
- `validation_errors`.

### Rules

- `allowed_routes = reachable_roles - {current_role}`.
- Add DONE only when current_role == closing_role.
- Add current_role only when bounded self-route policy permits it and its self-route budget remains.
- A semantic DONE from a non-closing role is rendered as handoff to closing_role.
- Prompt prose may mention only allowed_routes.
- Route budget exhaustion blocks with `ROUTE_BUDGET_EXHAUSTED` unless the closing role may finish DONE.
- Malformed/out-of-enum route output never expands reachability. Bounded repair exhaustion blocks with `ROUTE_VALIDATION_EXHAUSTED`. Resume does not reset these counters. Same-task continuation uses the existing task-control owner to monotonically raise the relevant limit; consumed counts remain unchanged and the new limit must exceed consumed count.


No `pending_route` is retained for route-budget exhaustion; the budget gate runs before Send.
When an exhausted closing role emits a non-DONE value despite its DONE-only enum, classify it as `ROUTE_BUDGET_EXHAUSTED`, not validation exhaustion, and do not increment repair count. After max_routes increase + Resume, rebuild allowed routes from the frozen contract and perform a new Send.
## DomProjection

Used only by ChatGPT Web/browser-backed providers.

- `page_url`, `page_url_kind`.
- `conversation_id` when visible/derivable from canonical URL.
- `last_user_message_id`, `last_user_text`, `user_turn_count`.
- `assistant_text`, `assistant_complete`.
- `composer_state`, `stop_visible`, `retry_visible`.
- `mcp_permission_allow_count`, `permission_prompt_nodes`, and bounded metadata for visible Business/Enterprise `Allow` controls that may be clicked by the browser provider permission handler.
- `visible_thinking_nodes`, `tool_activity_nodes`, `progress_nodes`, `error_banners`. For ChatGPT Web, `visible_thinking_nodes` and `progress_nodes` include bounded projections from `.group/activity-header`, `[class*="agent-activity"]`, `[class*="activity-header"]`, `.summary-lK7Lpm`, `.cadencedShimmerSweep-ICUAVH`, and `.cadencedShimmerHighlight-VcX29h`, including text, tag, class, visibility, viewport intersection, and a stable bounded DOM path.
- `observed_at`.

LISTEN/network and MutationObserver never become decision sources.

## ProviderRegistryEntry

- `provider_id`.
- `module_path`.
- `execution_modes`: browser_dom, acp, exec, http, custom.
- `capabilities`: browser, coding, long_timeout, material_api, adopted_url, direct_chat, etc.
- timeout/output limits.
- enabled flag.

## ProviderInvocation

- `provider_id`.
- `task_id`, `role_key`.
- `task_text`.
- `material_refs`.
- `workspace`.
- `timeout_seconds`, `output_limit_bytes`.
- `environment_policy`.
- `execution_mode`.
- optional provider-specific binding data such as conversation_url only when capability allows it.


## Phase15RecoveryState

Durable diagnostics/projection for browser-control recovery before Gate B:

- latest DOM-control fields: `stop_visible`, `retry_visible`, composer state, error banners, assistant transcript tail.
- permission evidence: `mcp_permission_allow_count`, permission prompt metadata, last permission-click result and timestamp.
- accepted-send repair state: retry seen/stable timestamp, retry click result, no-resend proof, repair decision.
- terminal-route reconciliation: source (`dom_assistant_text|audit_transcript|report_file`), parsed route JSON, expected report path, report sha256/size, RouteContract validation result.
- minimal WAIT callback: continuation id, script/job id, callback idempotency key, bounded result MaterialRefs, wake status.

This state is diagnostic/projection around existing owners. It does not authorize route values by itself and does not create a new DOM source.

## AsyncContinuation

Durable record for a provider/script job that allows a ChatGPT Web role turn to return `WAIT` instead of holding the browser response open.

- `continuation_id`.
- `task_id`, `role_key`, `route_contract_id`.
- `conversation_binding_id` and canonical parent conversation identity when available.
- `provider_job_id` or external script/job identity.
- `spawn_idempotency_key` and `callback_idempotency_key`.
- `state`: `registered|spawn_confirmed|waiting|callback_received|wake_scheduled|waking|completed|timeout|cancelled|failed|superseded`.
- `waiting_code`: `async_callback` while the parent task is WAITING.
- `result_material_refs`, stdout/stderr/result refs, exit/status metadata.
- `wake_policy`: `same_role` only in this feature. Other wake targets are out of scope until separately specified.
- `wake_prompt_ref` / bounded inline wake prompt.
- `timeout_at`, heartbeat/last_progress_at.

`WAIT` is a route-control token, not a TaskStatus. A task waiting for callback uses `status=WAITING` plus `waiting_code=async_callback`. Duplicate spawn/callback requests are idempotent and never create a second wake Send. A callback with the same idempotency key and conflicting payload fails closed with `ASYNC_CALLBACK_CONFLICT`. During WAITING, the parent canonical writer key is in `reserved_async_wait` and may be consumed only by the registered continuation wake, or released/sealed by timeout/cancel/failure/supersede handling.

## ProviderJob

- `provider_job_id`.
- `owner_task_id`: exactly one TeamTask.
- `provider_id`.
- `state`: QUEUED, RUNNING, SUCCEEDED, TIMEOUT, FAILED, CANCELLED, PARTIAL.
- `started_at`, `deadline_at`, `completed_at`.
- stdout/stderr/result MaterialRefs.

ProviderJob ids never appear in `depends_on_task_ids`.

## ProviderResult

- `provider_job_id`.
- `provider_id`.
- `status`: succeeded, timeout, failed, cancelled, partial.
- `stdout_ref`, `stderr_ref`, `result_ref`.
- structured error/metadata.

ProviderResult status is not a TeamTask route.

## MaterialRef

- `material_id`.
- `task_id` and optional `role_key`.
- `source_kind`: file, inline, api, generated_prompt, route_json, provider_result, transcript.
- `material_type`: report, handoff, prompt, skill, rule, task_context, log, artifact.
- `version`: opaque string.
- `mime_type`.
- `content_length`.
- `truncated`.
- bounded inline `content` when available.
- opaque nullable `content_ref`.
- safety/error metadata.

Gate A dashboard renders bounded inline content from `/api/report` and does not dereference content_ref.

## CronJob

- `cron_id`, enabled, schedule.
- TeamTask template.
- deterministic idempotency scope.
- last scheduled time.

## HeartbeatRecord

- `subject_id`, `subject_type`.
- status healthy/stale/offline/unknown.
- heartbeat time and TTL.
- stale recovery TeamTask template.

## DependencyEdge

- `from_task_id`, `to_task_id`: TeamTask ids only.
- edge state: ready, waiting, failed.
- derived task projection: `WAITING_DEPENDENCY` or `DEPENDENCY_FAILED`; these are not TaskStatus values and are reserved from role-key use.
- reason.

Missing ids and cycles fail validation. Readiness requires dependency DONE. RUNNING/WAITING/PAUSED and resumable BLOCKED prerequisites project `WAITING_DEPENDENCY`; STOPPED project `DEPENDENCY_FAILED`. Projection is recalculated whenever prerequisite state changes, so resumed BLOCKED work can move dependents back to waiting/ready. `ROUTE_BUDGET_EXHAUSTED` and `ROUTE_VALIDATION_EXHAUSTED` are resumable BLOCKED reasons. `ROUTE_CONTRACT_DRIFT` remains non-terminal/waiting for dependency projection but cannot be refreshed in place; a replacement TeamTask has a new id and does not automatically satisfy or rewire dependents of the old task.

## BrowserEndpoint (existing registry, reused by Gate C)

- `endpoint_id`: stable registry identity.
- `cdp_url`: normalized endpoint URL from `normalize_cdp_endpoint()`.
- `label`.
- `management_mode`: `external` or existing `managed-local`.
- `health` / probe evidence.
- `physical_browser_id`: EndpointPool projection derived from CDP browser identity when online; aliases to an already-owned physical browser project conflict and are not interactive-session eligible.
- `max_active_tabs`, rate-limit/default/draining settings where already supported.
- removal/draining state.

Operator creation accepts port shorthand, `host:port`/`ip:port`, or explicit `http(s)` / `ws(s)` URL. External endpoints are attach-only and are probed before first registration. ProviderSession and ConversationBinding reference `endpoint_id`; they never persist raw shorthand as identity. Gate C also guards endpoint records referenced by unsealed bindings, so update/remove cannot silently orphan or retarget idle sessions.

## ConversationBinding (Gate C)

- `binding_id`.
- `task_id`: nullable for standalone Direct Chat; otherwise bound TeamTask id.
- `role_key`: optional when no TeamTask is bound.
- `provider_id`, `endpoint_id`.
- bind-time `endpoint_cdp_url` and `physical_browser_id` evidence.
- `conversation_url`, canonical conversation id.
- `page_target_identity`: live page/CDP target identity observed within `physical_browser_id` for the current attachment.
- `page_binding_id`: durable internal identity allocated only after the runtime verifies a genuine provider new-conversation surface and atomically claims the unique `(provider_id, physical_browser_id, page_target_identity)` before first Send. One live page target has at most one provisional binding. It is a pointer to persisted page-binding evidence, not proof by itself. Recovery must revalidate provider/new-chat state, physical browser, target evidence, and stored pre-Send baseline against the recovered page before any further Send; if exact target recovery cannot be proven, recovery is reconciliation-only and may seal the binding.
- `writer_identity_key`: canonical `(provider_id, canonical_conversation_id)` whenever identity is knowable before Send; otherwise provisional `(provider_id, physical_browser_id, page_binding_id)` only for a genuinely new conversation. Canonical keys are globally unique across all active browser writer bindings, including ordinary TeamTask turns, endpoint aliases, and distinct physical browsers/profiles.
- `binding_status`: `bound|active|idle|blocked|released|sealed`. Live endpoint references are exactly `bound|active|idle|blocked`; `released|sealed` do not block endpoint update/remove finalization. `idle` holds no active writer lease only after the logical browser turn is fully terminal (assistant terminal DOM + valid route/turn commit, or block/terminal task), not merely after Send acceptance. `released` is permitted only when no non-terminal SendAttempt exists and the writer lease is released. DONE/STOPPED automatically release and mark released unless unresolved evidence requires sealing. `sealed` is non-reusable evidence after unresolved/collision handling.
- identity evidence and verified_at.
- identity mismatch block code: `CONVERSATION_IDENTITY_MISMATCH`.
- writer lease owner/id/heartbeat.

One writer identity key has one writer at a time globally. When a new conversation gains canonical identity, the runtime atomically claims the canonical key while retaining the provisional key until the current SendAttempt is terminal. If claim collides, the canonical key becomes quarantined: neither the winner nor any loser may Send again until all conflicting non-terminal attempts are terminal. The loser remains reconciliation-only and seals after terminal state. The surviving canonical owner must take a fresh baseline after quarantine clears. If a conflicting attempt is abandoned without reconciliation, quarantine remains permanent/non-reusable for runtime writes. Simultaneous re-key is serialized.

## ConversationWriterLease (Gate C)

- `writer_identity_key`: canonical provider+conversation key or provisional provider+physical-browser+page-binding key.
- `owner_binding_id`.
- `lease_epoch`: monotonically increases on every ownership grant/transfer; stale owners cannot record Send intent.
- `state`: `owned|reserved_async_wait|quarantined|released`.
- heartbeat / acquired_at / released_at.
- `quarantine_reason` and retryability when applicable.

Send intent creation MUST atomically verify `(writer_identity_key, owner_binding_id, lease_epoch)` and persist the same epoch into SendAttempt before any composer mutation. Takeover increments epoch. `WRITER_LEASE_BUSY` means another owner currently holds the key; `WRITER_LEASE_LOST` means a stale owner attempted to continue after epoch change; `WRITER_LEASE_QUARANTINED` means the key cannot currently be used. `reserved_async_wait` is a formal ConversationWriterLease state: an AsyncContinuation owns the canonical reservation while no browser/page lease is active. Other writers block with retryable `WRITER_LEASE_BUSY`. Successful same-role callback transitions `reserved_async_wait -> owned` before Send intent. Timeout/cancel/failure with release transitions `reserved_async_wait -> released`; unresolved or conflicting failure that must preserve evidence transitions the binding to sealed and the lease to `quarantined` or `released` according to the durable block outcome.

Lease lifetime for a TeamTask role turn or Direct Chat message extends through assistant terminal DOM state and all route/repair Sends until a valid turn/route commit, block, or task terminal state. A role routed away becomes idle only after that point; if it later returns, it must reacquire the canonical key and may block with `WRITER_LEASE_BUSY`.

## SendAttempt projection (Gate C)

This is a projection owned by the existing command ledger, not a new state store.

- `attempt_id`.
- `writer_identity_key`, `owner_binding_id`, and fenced `lease_epoch` captured at intent creation.
- `task_id`, optional direct-chat session id.
- `role_key`, `binding_id`.
- baseline last-user-message id and user-turn count.
- exact sent text hash.
- state: intent_recorded, submitted, accepted, unconfirmed, reconciled_accepted, reconciled_absent.

`intent_recorded`, `submitted`, and `unconfirmed` are non-terminal and keep the writer lease. On recovery, ambiguous intent_recorded/submitted first enters reconciliation (or unconfirmed) before retry is considered. No non-terminal state resends. Only proven reconciled_absent may lead to a new attempt id.

## ProviderSession / DirectChatMessage (Gate C)

ProviderSession binds endpoint + provider + ConversationBinding and exposes composer/stream state.

DirectChatMessage records idempotency key, submitted text/material refs, send attempt id, and result/stream cursor. It never owns send retry semantics itself.
