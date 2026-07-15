# Tampermonkey edge-case parity audit

## Source audited

The source was refreshed before inspection:

```text
repository: local checkout of Hiroshimeow/tampermonkey-auto
remote:     https://github.com/Hiroshimeow/tampermonkey-auto.git
branch:     feat/role-durable-upload-transport
commit:     39de7078f55a2e3a240efd5dba31e1ce6903e3a7
pull:       git pull --ff-only → Already up to date
```

The audited Tampermonkey checkout passed:

```text
node --check tampermonkey.js
node tests/test_tampermonkey_contract.mjs
uv run python -m pytest -q → 180 passed, 1 warning
```

Sources inspected include `tampermonkey.js`, the active `main.py`/`apps/*` runtime, `role.py`, `server.py`, tests, and the F5 recovery plan. `agents.py` is legacy and was not treated as the current orchestration source of truth.

## Ported invariants

| Risk observed in tampermonkey-auto | Playwright control | Verification |
|---|---|---|
| User draft or attachment is overwritten | owned-composer text, exact-text clear, conservative real-attachment detection | unit + live draft set/clear |
| Unrelated assistant activity is treated as Send success | Send accepts only exact new user prompt or Stop evidence | unit |
| Send failure produces duplicate prompt | at most two internal attempts; reload then exact transcript recheck before another click | unit + earlier live send/stop |
| Old assistant answer is reused | pre-send message/turn baseline plus exact prompt provenance | unit |
| Response text changes while the stability timer continues | fingerprint includes message identity, text and image count; timer resets on change | unit |
| F5 exposes a stale complete-looking response | first post-reload candidate must change or be observed again | unit |
| Partial JSON or unclosed code block is accepted | structural incomplete-response guard | unit |
| Image-only response is mistaken for empty output | per-message image count is part of completion evidence | unit |
| Choice UI replaces composer | safe-positive choice detection; explicit resolver; otherwise fail closed | unit; real choice UI pending |
| Snapshot/evaluate briefly fails | bounded retry in readiness/response waits | unit |
| Role moves to another physical tab | immutable page ID + role binding checked before mutations | unit + earlier three-tab live probe |
| Duplicated physical tab inherits page ID | workspace collision rejection and forced new physical page identity | unit |
| Concurrent workflows interleave on one tab | per-physical-page workflow/mutation locks | unit |
| Generic retry repeats side effects hidden in a composite block | retry-safety propagates through Sequence/When/Try/Repeat/Timeout/RoleSequence | unit |
| One role failure cancels all other dispatched roles | parallel gather completes all roles, then aggregates per-role errors | unit |
| Empty/broken role tab is silently considered healthy | explicit `PageHealth` and reload → optional new chat recovery ladder | unit + live health classification |
| Upload starts before the UI is ready | durable marker + exact attachment count + composer + no active response + Send enabled | unit; real upload pending |
| Duplicate/partial upload is repeated | file path/size/SHA identity, already-ready idempotency, partial attachment fail closed | unit |
| Process dies after clicking Send | ledger persists binding/baseline before click; transcript marker proves acceptance; otherwise no resend | unit crash matrix |
| Two processes execute the same request | non-blocking per-request OS file lock | unit |
| Ledger write is torn | global file lock, temp write, file fsync, atomic replace and directory fsync | unit |
| Same logical request gets a new identity | canonical prompt/context plus role, role-prompt hash and file SHA identity | unit |

## Durable request states

`DurableSendBlock` persists this state machine:

```text
NEW
→ PROMPT_SET
→ UPLOADING
→ UPLOAD_READY
→ SENDING
→ SENT
→ COMPLETED
```

Failure states are explicit:

```text
FAILED_RETRYABLE
FAILED_FINAL
```

Crossing into `SENDING` is the side-effect boundary. On restart:

- marker found in the user transcript: reconstruct the receipt and wait; do not send;
- `SENDING` or `SENT` but marker not found: fail closed;
- upload status with all expected attachments visibly ready: continue without re-upload;
- partial attachments, attachments without marker, or manual composer text: fail closed;
- completed request: return the cached `SendReceipt` and response.

## Workflow-level controls now available

```text
RecoverPageBlock
WaitCleanReadyBlock
ResolveChoicePromptBlock
SendPromptBlock
WaitResponseBlock
UploadFilesBlock
WaitUploadReadyBlock
DurableSendBlock
DispatchRouteBlock
WaitRouteResponsesBlock
```

`DurableSendBlock` owns its send/upload recovery. It must not be wrapped in generic `RetryBlock`.

## Runtime evidence boundary

Verified against the persistent Chromium tab:

- DOM snapshot with the new attachment/choice/image fields;
- role binding;
- auth-required health classification;
- clean-ready inspection;
- exact draft set and exact clear;
- Chrome process remained unchanged;
- earlier guest Send → responding → Stop workflow;
- earlier three-tab PLAN/DEV/REVIEW role persistence.

Not yet verified against a logged-in real conversation:

- completed assistant response and post-F5 recovery;
- an actual ChatGPT choice prompt;
- real file input/drop upload and attachment readiness;
- durable process crash during a real Send;
- live duplicate-role takeover and delayed reload generation race;
- product-specific MANAGER/FINISH policy from tampermonkey-auto.

These remain explicit gaps; unit coverage is not treated as live browser evidence.
