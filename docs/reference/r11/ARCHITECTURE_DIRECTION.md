# CDPA runtime-v2 architecture direction

## Decision

Stop extending the legacy G6 control plane as a pile of special-case gates. Continue from the FJP Spec003 direction and converge to a smaller runtime with one owner per concept.

## Core model

```text
TeamTask
  ├─ RolePolicy / RouteContract
  ├─ RoleSlot(s)
  ├─ ProviderDispatcher
  │    ├─ chatgpt_web -> Browser DOM adapter -> RoleController
  │    └─ non-browser -> ProviderJob
  └─ durable command/request ledger for irreversible actions
```

One-role TeamTask replaces Solo. Independent Agent is not a runtime-v2 execution model.

## Browser control

```text
exact owned page
   ↓
DomProjection (single producer / fresh snapshot)
   ↓
serialized RoleController
   ├─ permission handling
   ├─ response-active waiting
   ├─ terminal-valid acceptance/routing
   ├─ visible Retry/error/interrupt recovery
   └─ pre-send composer readiness
```

### DomProjection is authoritative

It must normalize at least:

- exact conversation/page binding;
- composer present/editable/non-empty/manual attachments;
- Stop / active generation;
- assistant current content + stability/finality evidence;
- exact accepted user turn when available;
- Retry / delivery-timeout UI;
- interrupt/connection UI;
- MCP/tool permission surface + actionable Allow control;
- visible dialogs/blockers;
- canonical URL identity.

No decision may depend on a stale LISTEN/network projection. MutationObserver may later wake a fresh DOM projection but cannot own state.

## Controller priority

The exact implementation may refine names, but the semantic order must prevent the known failures:

1. **Ownership / destructive-safety invariants** for the exact page/request.
2. **Actionable permission UI** (MCP Allow) on the exact owned page.
3. **Current valid terminal result**: if route/report is valid and stable, accept it before considering Retry/repair/stale banners.
4. **Actually active response**: wait; do not block merely because the page is responding.
5. **Visible Retry/interrupt/error state**: classify from DOM and apply one bounded recovery owner. Do not let stale error UI block a clean pre-send composer.
6. **Pre-send composer readiness**: if no response is active and composer is safe/usable, fill/send through exactly-once owner.
7. **Invalid terminal candidate**: route/report repair only when stable, inactive, current, and genuinely invalid.

The controller must not infer state from absence of Stop alone. It must use the full DOM projection and terminal candidate evidence.

## Route contract

- `reachable_roles` is frozen from task policy.
- current role is excluded by default.
- self-route requires explicit bounded policy; ordinary unfinished work stays in the same turn and does not emit a route.
- DONE belongs to configured `closing_role`.
- prose and JSON schema derive from the same RouteContract.
- repair cannot widen routes or invent self-route.

## Focus policy

Observation and liveness must never call browser/tab focus. Background DOM/CDP reads are sufficient. Focus is operator UX only when explicitly requested.

## Temporary policy

Temporary Chat is an ephemeral document identity:

- no automatic F5/reload;
- no auto-close merely because task blocks;
- no replacement with New Chat when canonical/temporary identity already exists;
- if the exact document is unrecoverable, pause with typed state rather than fabricating continuity.

## What NOT to port from legacy G6

Do not re-create:

- generic choice-prompt heuristics;
- competing LISTEN vs DOM truth;
- raw-DOM duplicate identity fatal gates;
- focus-driven liveness;
- multiple overlapping response activity timers;
- self-route as an implicit continuation mechanism;
- generic BLOCK cleanup that destroys Temporary context;
- pre-send fatal handling for stale Retry banners or active response;
- independent-agent execution as a second workflow engine.