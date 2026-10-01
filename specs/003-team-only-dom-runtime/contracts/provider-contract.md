# Contract: Provider Runtime

## ProviderRegistryEntry

- provider_id
- module_path
- execution_modes: browser_dom, acp, exec, http, custom
- capabilities: browser, coding, long_timeout, material_api, adopted_url, direct_chat, etc.
- timeout/output limits
- enabled

`long_timeout` is a capability, not an execution mode.

## ProviderInvocation

```json
{
  "provider_id": "pi",
  "task_id": "...",
  "role_key": "DEV",
  "task_text": "...",
  "material_refs": [],
  "workspace": "...",
  "timeout_seconds": 25200,
  "output_limit_bytes": 1048576,
  "environment_policy": "inherit-safe",
  "execution_mode": "exec"
}
```

Provider-specific binding fields such as conversation_url are accepted only when that provider declares the capability.

## ProviderResult

```json
{
  "provider_job_id": "...",
  "provider_id": "pi",
  "status": "succeeded|timeout|failed|cancelled|partial",
  "stdout_ref": null,
  "stderr_ref": null,
  "result_ref": null,
  "error": null
}
```

ProviderResult status is context, never a TeamTask route. TeamTask transitions still require validated RouteContract output.

## ChatGPT Web permission/Allow handling

The `chatgpt_web` browser provider MUST carry forward the existing CDPA permission-click behavior for Business/Enterprise accounts. Visible MCP/tool permission prompts and `Allow` buttons are browser-provider control surfaces, not route values. The provider projects permission prompt count/state through the DOM projection, clicks allowed controls only after exact task/team/page ownership and composer-safety checks, records the click in diagnostics/timeline, and then resumes the same wait_response observation without replaying the user Send.

A ProviderJob belongs to exactly one TeamTask and never appears as a dependency id.

## BrowserEndpoint registry reuse

Browser-backed providers resolve endpoint targets through the existing BrowserEndpoint registry. Direct Chat MUST NOT create a private endpoint list.

- Add: reuse `POST /api/browser-endpoints` with operator `address`.
- Accepted address forms: local port shorthand, `host:port` / `ip:port`, or explicit `http`, `https`, `ws`, `wss` URL; normalization is owned by `connection.py::normalize_cdp_endpoint`.
- Every add, cdp_url update, start/attach, and recurring health revalidation path uses the same safe probe policy. HTTP(S) targets only `/json/version` on the admitted normalized authority with bounded timeout/body, rejects redirects/cross-origin substitution, and pins/rewrites any returned `webSocketDebuggerUrl` authority to the admitted authority. WS(S) requires a bounded CDP connector handshake. Hostnames are explicit operator-trust aliases re-resolved per operation; no cross-operation IP-pinning guarantee is claimed. Only structured health/auth/error state may be surfaced, never arbitrary response bodies.
- Update/remove/start/stop reuse existing BrowserEndpoint commands and registry records. Gate C extends their in-use guard to unsealed ProviderSession/ConversationBinding references, including idle bindings.
- ProviderSession/ConversationBinding pin stable `endpoint_id` plus bind-time normalized `cdp_url` and EndpointPool `physical_browser_id`; endpoint defaults or address updates never silently retarget existing bindings. Drift requires rebind before Send.
- EndpointPool physical-browser conflict state is not eligible for browser Send under Gate C. Every browser-backed TeamTask writer, adopted binding, and Direct Chat session acquires the same global lease namespace. Existing/adopted/resumed conversations acquire canonical `(provider_id, canonical_conversation_id)` before Send. Only positively verified provider new-conversation surfaces may use provisional `(provider_id, physical_browser_id, page_binding_id)`; page_binding_id is uniquely claimed 1:1 against the live physical page/target before Send, so two bindings cannot write one new-chat composer. Unresolved identity never qualifies. Re-key atomically claims canonical key while retaining provisional ownership through the current non-terminal SendAttempt. Collision quarantines the canonical key: winner and losers cannot start new Sends until conflicts terminalize; losers remain reconciliation-only then seal. The surviving owner takes a fresh baseline after quarantine clears. Unreconciled abandon keeps the canonical key non-reusable.
- Endpoint mutation remains on the existing operator-control `/api/browser-endpoints` surface under its existing runtime access boundary and is not exposed through provider/material APIs.
- `management_mode=external` is sufficient for user-supplied `<ip>:<port>` and is attach-only. Existing `managed-local` lifecycle support is optional and separate. Plaintext remote HTTP/WS is accepted only by explicit operator input; no automatic remote discovery is introduced.

Writer lease fencing is part of browser provider Send admission: before composer mutation, provider invocation must present current writer key + owner binding + lease_epoch, and intent creation atomically rejects stale epochs. Pre-Send identity validation and composer/send execute on the same already-verified CDP page/session; if that attachment changes, revalidation/rebind is required rather than re-resolving hostname between check and Send.


## Async ProviderJob and wake-back

Phase 1.5 minimal script WAIT is available for script/command timeout callbacks on existing TeamTask role conversations. ProviderJob may be asynchronous. Full provider async execution can be prepared in Gate B, but generalized ChatGPT Web `WAIT` across ProviderSession/adopted URL/Direct Chat and same-conversation wake-back with global writer leases remains Gate C because it requires ConversationWriterLease, ConversationBinding, and SendAttempt reconciliation. For async jobs, ChatGPT Web does not wait for the job result in the same response. The provider/script spawn path returns a durable `continuation_id` and confirmed job identity. The role may then route `WAIT`.

Completion enters through a CDPA callback surface such as `POST /api/async-continuations/{continuation_id}/complete` or an equivalent local CLI command. The callback records ProviderResult/MaterialRefs and schedules a same-TeamTask wake turn against the parent conversation binding. It must not create a new task or new chat unless the continuation policy explicitly says to create a replacement after parent terminal/cancelled state.

The callback surface is idempotent. Same continuation + same callback id returns the existing result. Conflicting callback payloads fail closed with `ASYNC_CALLBACK_CONFLICT`. Wake-back uses the same writer lease, endpoint identity, adopted URL, and SendAttempt reconciliation rules as Direct Chat/TeamTask browser sends.
