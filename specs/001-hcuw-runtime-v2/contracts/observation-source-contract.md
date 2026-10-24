# Contract: Observation Source Authority

## Purpose

Define what each observation source may and may not decide before runtime v2 implements send, bind, wait, route, or repair.

## DOM Full Snapshot

Role: primary transcript and identity authority for future routing.

Allowed outputs:

- Rendered user and assistant turns.
- Message identifiers when available.
- Logical turn identifiers when available.
- Composer, send, stop, retry, dialog, and error UI state.
- Page/task ownership markers for runtime v2 namespace.

Forbidden uses:

- Must not route by simply taking latest assistant text.
- Must not treat synthetic fallback IDs as equivalent to canonical logical IDs without an explicit binding rule.

Future routing rule:

- A route may be parsed only from an assistant response bound to the exact runtime v2 send receipt.

## Sparse Wait Probe

Role: efficient waiting signal and snapshot refresh trigger.

Allowed outputs:

- Last visible user/assistant identifiers and text tail.
- Stop/retry/transport/error/dialog indicators.
- Composer state.
- Response activity tail.

Forbidden uses:

- Must not be the sole route authority.
- Must not silently replace a full DOM binding when identifiers conflict.

## MutationObserver

Role: wake signal only.

Allowed outputs:

- DOM changed event.
- Optional bounded revision counter or timestamp.

Forbidden uses:

- Must not provide transcript text.
- Must not provide route authority.
- Must not infer completion status by itself.

## Passive LISTEN / Network Observation

Role: supplemental transport and diagnostic source.

Allowed outputs:

- Streaming started/active/completed/failed status.
- Terminal network frame text and message identifier when present.
- Permission-action diagnostics when observed.
- Network listener diagnostics.

Forbidden uses:

- Must not route a terminal response unless reconciled with DOM-bound assistant identity.
- Must not transfer authority to latest conversation response.
- Must not override DOM binding conflicts.

## Active Backend Status Diagnostic

Role: rare timeout diagnostic.

Allowed outputs:

- Backend stream status for a known conversation when timeout recovery needs diagnosis.
- Bounded diagnostic metadata.

Forbidden uses:

- Must not run as normal polling.
- Must not authorize route decisions.
- Must not create extra conversation sends, retries, or regenerations.

## Manual User Intervention Rule

If a manual user turn appears after a runtime v2 send and before the runtime has bound the owned assistant response, runtime v2 must pause or fail closed with a named reason such as `manual_user_intervention_after_send`. It must not silently bind the assistant after the manual turn as the result for the original send.

## Route Repair Rule

Route repair is a fallback only after the runtime has recorded actionable diagnostics explaining why the original bound terminal result could not be captured. Route repair must not be treated as the normal second half of every route.
