# CDPA Dashboard Live SSE Migration Plan

Date: 2026-09-25
Repository: E:\git-project\playwright-auto-multicdp
Baseline branch: feat/cdpa-multi-cdp-temporary-recovery

## Goal

Replace the current 1-second Live Transcript snapshot polling path with one focused SSE stream while preserving JSONL as the durable audit source, reducing repeated JSONL parsing and DOM churn, and keeping the current dashboard behavior stable outside Live view.

Current deployment acceptance target is the local g6/LAN path. The design must remain compatible with a future named Cloudflare Tunnel, but must not add Cloudflare-specific core architecture.

## Current-state facts

- Worker/runtime telemetry producers append live events to `.runtime/live-events.jsonl`.
- `GET /api/tasks/{task_id}/live` currently rereads/parses the JSONL file and filters by task/role.
- Dashboard refresh currently includes `loadLiveAudit()` in the 1-second global refresh.
- Recent UI patch already avoids rebuilding the entire Task Detail when only live state changes; it replaces only `#task-live` and preserves disclosure state.
- Therefore, the remaining UI issue is Live-panel churn, not whole-detail churn.
- Dashboard frontend proxy `:9224` currently buffers API responses via `response.read()`, so it cannot pass SSE through yet.
- Dashboard API and worker are separate processes. Do not introduce producer-side in-memory broker assumptions without IPC.
- Current public/domain path is not the acceptance environment. LAN g6 is.

## Architecture decision

```text
PageObserver / role runtime / worker
                |
                v
       append_live_event()
                |
                v
       live-events.jsonl
        durable audit
                |
                v
       Dashboard API :9225
        LiveStreamHub
        |- ONE LiveLogFollower
        |- bounded ring buffer
        |- stream_epoch + seq
        '- bounded subscriber queues
                |
                v
               SSE
                |
                v
     streaming proxy :9224
                |
                v
   focused dashboard Live view
                |
        reducer + incremental DOM
```

### Deliberate non-goals

- No Redis.
- No worker -> API IPC.
- No WebSocket.
- No Cloudflare Worker / Durable Object.
- No global durable numeric sequence written into JSONL.
- No generic text diff algorithm in v1.
- No SSE conversion for the entire dashboard.
- No producer-side broker in the worker process.
- Do not change orchestration authority or LISTEN/DOM precedence semantics.

## Hard invariants

### 1. Bootstrap race must be closed at the follower boundary

The server must not send `stream.ready` merely because a subscriber object exists.

Required ordering:

```text
activate follower
-> follower captures a valid initial file position / continuity state
-> attach subscriber
-> stream.ready
-> client GET snapshot
-> client applies buffered stream events after snapshot
```

This prevents the race:

```text
subscriber created
-> event appended
-> follower starts later and chooses EOF
-> appended event is treated as historical and silently missed
```

The client bootstrap contract is:

1. Open SSE.
2. Server activates follower and captures continuity state.
3. Server attaches subscriber.
4. Server emits `stream.ready`.
5. Client buffers subsequent SSE events.
6. Client fetches one `/live` snapshot.
7. Client renders snapshot.
8. Client applies buffered SSE events in order.
9. Client deduplicates overlap using durable `event_id`.
10. Client enters steady-state streaming.

No silent event gap is acceptable.

### 2. Transcript identity prioritizes message_id

Canonical reducer key:

```text
message_id
fallback: author + turn_id
```

Never use `turn_id` alone.

A user message and assistant message may share one logical turn:

```text
user      turn=t17 message=u17
assistant turn=t17 message=a17
```

Using only `turn_id` could overwrite one with the other.

### 3. Final LISTEN remains authoritative

Partial DOM transcript updates are UX projections.

For the same message identity, final LISTEN data with `complete=true` overwrites partial DOM text and is treated as authoritative.

### 4. Operator intent controls streaming

PINNED historical role must not keep an SSE stream just because that scope can theoretically produce telemetry.

Only FOLLOW + visible Live view may stream.

## Backend design

### A. LiveStreamHub

Create a focused module such as:

`src/playwright_auto/live_stream.py`

Responsibilities:

- own `stream_epoch`
- own transport-local monotonically increasing `seq`
- maintain a bounded ring buffer, initially ~256-512 envelopes
- maintain bounded subscribers
- expose subscribe/unsubscribe/replay/reset behavior
- never silently drop events for a slow subscriber

Use synchronous primitives compatible with the current `ThreadingHTTPServer`, e.g. `queue.Queue` and/or `threading.Condition`.

Do not use `asyncio.Queue` unless the server architecture is explicitly migrated, which is not part of this task.

Subscriber queue should be bounded, e.g. ~256 events.

If a subscriber cannot keep up:

```text
queue full
-> mark overflow / terminate stream
-> browser reconnects
-> replay if ring still contains requested seq
-> otherwise stream.reset + snapshot
```

### B. LiveLogFollower

The Dashboard API process has exactly one follower for the JSONL source.

Requirements:

- follower is dormant when subscriber count is zero
- when first subscriber arrives, activation captures the current continuity state before subscriber attachment
- read only newly appended bytes during steady state
- preserve incomplete trailing line between reads
- parse each newly completed JSON line once
- publish accepted events to the hub
- handle malformed/truncated lines safely
- detect file replacement/truncation used by JSONL trim logic
- rotation/truncation invalidates continuity cleanly rather than guessing

On Windows, avoid a design that unnecessarily holds the JSONL file handle open forever if that interferes with `os.replace()` trim behavior.

A safe implementation may stat/open/seek/read/close on each follower tick while remembering byte offset and partial line.

Follower cadence may start around 100-250 ms while active and become modestly adaptive if useful. Do not optimize prematurely beyond what tests justify.

On rotation / replacement / size < offset:

- create a new `stream_epoch`
- invalidate replay continuity
- emit/reset stream state
- require snapshot reconciliation

### C. Sequence and event identity

Durable audit identity remains the existing:

`event_id = live-<uuid>`

Transport identity becomes:

`stream_epoch + seq`

Example SSE ID:

`7fb3c1:18422`

Do not add a shared cross-process global numeric sequence to JSONL.

### D. Event semantics

Do not clone the Responses API schema.

Keep events close to current telemetry.

Minimum projected types:

- `stream.ready`
- `stream.reset`
- `transcript.upsert`
- `audit.append`

A transcript upsert contains the full currently known text, not a generic delta:

```json
{
  "type": "transcript.upsert",
  "stream_epoch": "7fb3c1",
  "seq": 18422,
  "event_id": "live-...",
  "task_id": "...",
  "role": "DEV",
  "generation": 3,
  "values": {
    "message_id": "...",
    "turn_id": "...",
    "author": "assistant",
    "text": "full current text",
    "complete": false
  }
}
```

This must correctly handle rewrite cases such as:

```text
ABCDEF
-> ABXY
```

Frontend replaces message text directly.

Do not add a generic diff engine.

### E. Existing snapshot endpoint

Keep:

`GET /api/tasks/{task_id}/live`

It remains required for:

- bootstrap
- PINNED historical role
- reconnect/reset recovery
- debugging

Optimize `read_recent_live_events()` to avoid reading/parsing the entire file for a recent snapshot.

Preferred direction:

```text
seek EOF
-> read backwards in bounded chunks
-> parse complete lines from newest to oldest
-> filter task/role
-> stop once enough matching events are collected
```

Preserve API ordering expected by current frontend/tests.

Do not introduce SQLite/indexing for this.

### F. SSE API

Add:

`GET /api/tasks/{task_id}/live/stream?role=DEV`

Validation should match the existing `/live` endpoint.

Headers should include the SSE essentials:

- `Content-Type: text/event-stream`
- `Cache-Control: no-cache, no-transform`
- `Connection: keep-alive`
- `X-Accel-Buffering: no` where useful

Heartbeat roughly every 20 seconds while idle is acceptable.

Reconnect:

- use `Last-Event-ID`
- same epoch + requested seq still in ring => replay from next seq
- old epoch or evicted seq => `stream.reset`
- reset requires snapshot bootstrap

## Frontend proxy design

Current `dashboard.py::_proxy()` buffers via `response.read()`.

Keep normal JSON/API proxy behavior unchanged.

Special-case SSE:

```text
API response received
-> detect text/event-stream
-> send response status/headers immediately
-> forward frames/chunks incrementally
-> flush browser output
-> keep connection open
-> close backend promptly when browser disconnects
```

Do not accidentally buffer until a large chunk fills.

Use a read primitive/loop appropriate for SSE frame or line delivery.

The existing ~10s backend timeout cannot be blindly reused for long-lived SSE. The streaming path needs timeout semantics compatible with the heartbeat interval.

Test HTTP behavior rather than globally changing every request path.

## FOLLOW / PINNED role model

Add explicit operator intent state, conceptually:

```text
roleFocusModeByTask:
  FOLLOW
  PINNED
```

### FOLLOW

- Clicking a RUNNING task defaults to FOLLOW.
- Selected role follows `active_role`.
- When routing changes DEV -> REVIEW while still FOLLOW, focus switches to REVIEW.
- If Live tab is visible, old DEV SSE closes and REVIEW SSE opens.

### PINNED

- User manually choosing another role changes mode to PINNED.
- PINNED historical role uses snapshot only.
- PINNED role does not keep SSE alive.
- Provide a clear `Follow active role` affordance.

## Canonical stream predicate

Exactly one stream is allowed only when all conditions are true:

```text
document is visible
AND selected task exists
AND task.status == RUNNING
AND selected detail tab == Live
AND focus mode == FOLLOW
AND active_role exists
AND selected_role == active_role
```

Otherwise no SSE.

Expected cases:

- Overview => OFF
- Reports => OFF
- PINNED historical role => OFF
- hidden browser => OFF
- terminal task => OFF
- task switch => close old immediately
- role switch => close old immediately

Maximum: one SSE per dashboard browser tab.

## Frontend lifecycle module

Add a focused module such as:

`src/playwright_auto/dashboard_assets/live_stream.js`

One authority function:

`syncLiveStream()`

State should include only what is necessary, such as:

- current stream object
- current scope
- stream status
- bootstrap buffer
- last transport seq
- current epoch

All relevant UI/state changes call the one synchronizer rather than directly creating EventSource instances.

## Reducer

Do not let raw SSE callbacks directly rebuild DOM.

Maintain bounded client state:

```text
messagesByIdentity
messageOrder
auditEvents
seenEventIds
streamStatus
lastSeq
```

Transcript key:

```text
message_id
fallback: author + turn_id
```

Rules:

- existing message => overwrite text/complete state
- new message => insert once
- duplicate durable `event_id` => ignore
- LISTEN final for same identity => authoritative overwrite
- `stream.reset` => rebootstrap safely

Do not persist large live transcript state to localStorage.

Suggested bounds:

- transcript ~100 messages
- audit ~300 events
- seen IDs bounded accordingly

## Incremental DOM

The recent patch already limits live-only refresh to `#task-live`.

SSE migration must go further.

Do not `replaceWith(nextLive)` for every event.

Target behavior:

- new message => append one message node
- changed message => update existing text node
- completion => update state/badge only
- audit event => append one audit row
- old bounded audit row => remove oldest
- preserve disclosure state
- preserve text selection
- preserve report state
- preserve scroll intent

## Render coalescing

Reducer state updates may be immediate, but DOM work should be coalesced.

Use `requestAnimationFrame()` and/or a modest 50-100 ms maximum flush cadence.

Many telemetry updates in a short burst should result in a small number of paints.

Do not make SSE hotter than the polling UI it replaces.

## Scroll behavior

- if user is already at/near bottom => auto-follow
- if user scrolls up => never force them down
- indicate new activity while not following bottom
- provide a way to jump to latest
- active streaming indicator only on the active message

## Polling after migration

Do not SSE-ize the whole dashboard.

Target direction after Live SSE proves stable:

- Live => SSE
- Board RUNNING => ~2 s + ETag
- Board idle => ~5 s
- Runtime => ~3-5 s
- Agents => ~10 s or refresh after mutation
- Pending commands => ~1 s only while pending
- CPU/disk => ~5 s
- hidden dashboard => pause polling + close SSE

Remove `loadLiveAudit()` from the global 1-second refresh only after the SSE path is proven and fallback behavior is covered.

## JSONL trim behavior

Do not change `_trim_live_log()` cadence in the first migration unless profiling proves it material.

The current per-append stat is not the primary bottleneck and changing retention timing adds avoidable risk.

## Cloudflare future compatibility

Current acceptance path is local/LAN g6.

Future path may be:

```text
browser
-> Cloudflare HTTPS
-> named Cloudflare Tunnel
-> dashboard :9224
-> API :9225
```

Core implementation should therefore already provide:

- `text/event-stream`
- `no-transform`
- regular heartbeat
- immediate flushing
- bounded stream count
- bounded payload/state
- no hidden proxy buffering

Do not make Cloudflare a current acceptance gate and do not add Cloudflare-specific services to the core design.

## Required tests

### Backend follower / hub

- first-subscriber activation captures continuity before subscriber attach
- event appended during activation cannot be silently lost
- newly appended line parsed once
- partial trailing line handled
- malformed line safe
- task/role filtering
- bounded ring eviction
- bounded subscriber overflow
- multiple subscribers do not cause multiple JSONL followers
- zero subscribers => follower dormant
- rotation / replace
- truncate / size < offset
- epoch reset

### SSE API

- correct headers
- `stream.ready` ordering contract
- heartbeat
- replay within retained ring
- old/invalid cursor => reset
- clean disconnect/unsubscribe
- role/task validation

### Proxy

- first SSE frame reaches browser without waiting for stream end
- no `response.read()` buffering on SSE
- normal JSON proxy path unchanged
- heartbeat survives idle period
- backend disconnect handled
- browser disconnect propagates
- stream timeout does not kill healthy idle stream

### Frontend lifecycle

- at most one EventSource
- FOLLOW active role opens only on visible Live tab
- Overview/Reports => no SSE
- PINNED role => no SSE
- task switch closes old stream
- role switch closes old stream
- hidden document closes stream
- visible document bootstraps/reconnects
- DEV -> REVIEW route while FOLLOW migrates stream
- manual historical role selection remains PINNED

### Bootstrap race

Explicit regression for:

```text
activate follower/capture offset
-> attach subscriber
-> ready
-> snapshot begins
-> event appended during snapshot
-> event is preserved in buffered SSE
-> overlap deduped by event_id
```

No gap and no duplicate transcript.

### Transcript identity

Explicit regression proving:

```text
user      turn=t17 message=u17
assistant turn=t17 message=a17
```

remain distinct.

Also test fallback `author + turn_id` when message_id is unavailable.

### Reducer / DOM

- full-text upsert rewrite ABCDEF -> ABXY
- LISTEN final overrides partial
- duplicate event ignored
- new message appended once
- existing message updated in place
- Live panel not fully replaced per event
- audit bounded
- transcript bounded
- disclosure preserved
- selection preserved
- user scroll-up not overridden
- new-activity indicator behavior

## Implementation order

1. Re-check clean baseline and current source/tests.
2. Produce a detailed PLAN report before DEV touches code.
3. Reverse-tail snapshot reader and focused tests.
4. FOLLOW/PINNED state + tests.
5. LiveStreamHub + follower + ring + bounded subscriber queues.
6. SSE endpoint on :9225.
7. Streaming-aware proxy on :9224.
8. Frontend `syncLiveStream()` lifecycle.
9. Race-safe bootstrap protocol.
10. Reducer with correct transcript identity.
11. Incremental transcript renderer.
12. Incremental audit renderer.
13. Scroll/new-activity behavior.
14. Remove Live from 1-second poll after fallback/reconnect tests pass.
15. Tune remaining polling cadence conservatively.
16. LAN g6 CPU/load/UX soak.
17. REVIEW.
18. PLAN closure only after regressions and acceptance evidence pass.

## Acceptance checklist

- [ ] clean baseline preserved; unrelated work not overwritten
- [ ] zero focused Live views => zero SSE and follower dormant
- [ ] one dashboard tab => max one SSE
- [ ] RUNNING + FOLLOW + Live + visible => stream active role
- [ ] PINNED historical role => snapshot only
- [ ] Overview / Reports => no live stream
- [ ] hidden browser => no live stream
- [ ] task/role change closes old stream immediately
- [ ] follower activation ordering closes the bootstrap race
- [ ] snapshot/SSE overlap dedupes by durable event_id
- [ ] reconnect replays when ring permits
- [ ] expired epoch/ring produces explicit reset + snapshot
- [ ] no silent gap
- [ ] no unbounded subscriber memory
- [ ] one JSONL follower regardless of subscriber count
- [ ] no full JSONL parse every second
- [ ] snapshot reader stops after enough recent matches
- [ ] JSONL rotation/truncation safe
- [ ] transcript identity is message_id first, fallback author+turn_id
- [ ] user and assistant in same turn remain distinct
- [ ] final LISTEN overrides partial DOM projection
- [ ] no full `#task-live` replacement for each SSE event
- [ ] DOM/state bounded
- [ ] scroll-up intent preserved
- [ ] current non-Live dashboard behavior does not regress
- [ ] worker/orchestration semantics unchanged
- [ ] LAN g6 idle CPU does not regress
- [ ] LAN g6 focused Live CPU/load improves versus polling baseline
- [ ] future named Cloudflare Tunnel requires no protocol redesign

## PLAN role requirement

The first PLAN role must not simply restate this document.

It must audit the actual current source at the clean baseline and produce an implementation-ready plan for DEV including:

- exact files/functions/classes to change
- any deviations required by current code
- concurrency/locking ownership for follower activation, ring, queues, and disconnect
- exact bootstrap state machine and ordering
- exact reconnect/reset semantics
- transcript identity rules
- proxy streaming mechanics for the current Python HTTP stack
- migration/fallback path
- concrete tests to add/update by file
- risk list and rollback boundaries
- staged implementation order that keeps tests runnable after each phase

PLAN should route to DEV only after those details are explicit enough that DEV does not need to redesign the architecture.
