# CDPA Clean Rebuild Contract

Date: 2026-10-25

## Authority

- Architecture and acceptance authority: `docs/reference/r11/`.
- Canonical design checkpoint: Runtime v2 / CDPA v11 R11.
- Code base identity: FJP/R11 committed ancestor `0a80878fec4c7c92ed70fdb7e59ff98498be615e`.
- Verified base tree: `a2ba6bf7558be4d8a67dc958bc493b46e1b0162e`.
- G6 rebuild checkpoint restoring that exact tree: `0cd7432ede785741b5b92c2be14ae5af8ceb46c6`.
- Active implementation donor: FJP `C:\Users\DuongNH66\Desktop\git\cdpa`, branch `runtime_v3`, base HEAD `0a80878...`.
- The old G6 CDPA repositories, worktrees, audit snapshots, PM2 CDPA entries, API and dashboard runtimes were deliberately retired and are not implementation sources.

## Non-negotiable architecture

1. One public execution model: TeamTask. A one-role TeamTask replaces legacy Solo.
2. RolePolicy owns reachable roles and route semantics. No implicit self-route and no role-specific hard-coded repair route.
3. Browser-backed execution has one authoritative fresh DOM projection. LISTEN/network/MutationObserver may wake work but may not override fresh DOM truth.
4. One serialized RoleController owns browser mutation for an owned page/conversation.
5. Irreversible actions use durable command/request identity and idempotent reconciliation.
6. ProviderDispatcher separates browser DOM providers from non-browser ProviderJob/ProviderResult execution.
7. Normal observation and liveness checks must not steal focus.
8. Temporary Chat is ephemeral: no automatic F5/reload, auto-close, or replacement chat. Preserve typed/manual state and pause when ownership cannot be safely recovered.
9. Valid stable terminal results take priority over retry/repair scaffolding. Empty/changing assistant shells are not terminal failure.
10. Cron, heartbeat and recovery create ordinary TeamTasks; no second independent workflow engine.

## Donor policy

FJP `runtime_v3` is a working implementation donor, not a branch to merge wholesale.

For every donor slice:
1. identify the R11 owner and contract;
2. inspect the complete donor implementation and tests;
3. port only behavior that fits the owner boundary;
4. add/retain failing-first regression coverage;
5. prove focused tests;
6. prove affected integration suites;
7. commit one coherent owner slice.

Do not re-import legacy duplicated gates, stale LISTEN arbitration, focus-based liveness, destructive Temporary cleanup, implicit self-routing, multiple competing schedulers, or parallel durable ledgers.

## Build order

1. Foundation and provenance.
2. TeamTask / RolePolicy / RouteContract / RoleSlot ownership.
3. Durable command-request identity and lost-ack reconciliation.
4. DomProjection + RoleController browser path.
5. ProviderDispatcher and non-browser provider jobs.
6. AsyncContinuation / MaterialRef / result delivery.
7. Cron + heartbeat + dependency readiness.
8. Direct Chat, adopted URL and endpoint management.
9. Dashboard/API/operator UX over the same owners.
10. Legacy feature-parity acceptance and release hardening.

## Mandatory release gates

- No duplicate durable send/task creation after lost acknowledgment or retry.
- Stable idempotency identity produces stable implicit team identity.
- No automatic F5/reload in Temporary Chat.
- No normal-observation focus stealing.
- No stale LISTEN/network state overriding fresh DOM.
- No retry/repair while a valid stable terminal result exists.
- No implicit self-route/no-op route.
- Permission UI is actionable but never mistaken for route/finality.
- One durable ledger and one scheduling path per concept.
- Focused owner tests, integration tests, broad regression suite and real browser/CDP acceptance all pass.
- Runtime may be enabled on G6 only after the release gates above are evidenced.

## Current baseline

- G6 path: `E:\git-project\cdpa`
- Branch: `rebuild/runtime-v2-r11`
- Verified runtime/test tree: `6bac3efd1c35c4bbcf6e2f229cf3e720593103aa`
- Python: CPython 3.12.12 virtual environment built from `uv.lock`
- Baseline compile and `git diff --check`: PASS
- Spec003 Gate B: 65 passed
- Spec003 Phase 1.5: 26 passed
- R11 critical runtime/attach set: 63 passed
- Full regression clean-teardown run: 1927 passed, 45 skipped, stderr 0 bytes
- FJP `runtime_v3` production parity: zero differences under `src/playwright_auto`
- Gate B T059-T073: complete
- Gate C T074+: open/deferred
- CDPA server/dashboard/worker: intentionally OFF
