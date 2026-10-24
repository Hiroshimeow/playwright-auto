# CDPA v11 — internal convergence R11 (15 October 2026)

## Provenance and scope

- Parent: ChatGPT-internal R10 archive `cdpa-v11-internal-convergence-r10-20261015.zip`, SHA-256 `ff22ad9228422c86ba69c98f5eefa000f5bde795398c36b5fd5f92163b20fdd6`.
- Git identity remains intentionally unchanged at committed ancestor `0a80878fec4c7c92ed70fdb7e59ff98498be615e` on `refactor/hcuw-runtime-v2`; R1–R11 product work remains intentional WIP inside the portable source archive.
- R11 is internal-only. No FJP/G6 CDPA source/runtime/browser/deployment is modified.

## Reproduced R11 defects

The independent R8 review identified two request-identity holes that were still present in R10.

1. Generic `RuntimeTaskRunner.submit_task()` accepted `idempotency_key=None`. `_post_legacy_api()` then generated a new random key for every POST. If the legacy API accepted a task and the runtime-v2 process lost the acknowledgment/local record, retrying the same logical submission could create a second durable command.
2. Even with a caller-supplied stable key, omitted `team` generated a random `hcuw-v2-team-*` value during validation. A retry with the same key therefore changed the request fingerprint/payload and could fail with idempotency conflict instead of reconciling the accepted command.

Failing-first regressions reproduced both behaviors before the fix.

## Implementation

1. Generic runtime-v2 TeamTask submission now requires a non-empty `Idempotency-Key` of at most 200 characters before any legacy runtime/API side effect.
2. When `team` is omitted and a stable key is present, the default team is `hcuw-v2-team-<sha256(key)[:12]>`. Different explicit intent keys still produce different teams even for identical task text.
3. `_submit_team_with_legacy_api()` and `_post_legacy_api()` no longer have a random-key fallback. Their TeamTask path always receives the validated stable key.
4. Runtime-v2 API forwards the HTTP `Idempotency-Key` header to the task runner and exposes missing/invalid keys as 400 and key conflicts as 409.
5. Runtime-v2 dashboard proxy forwards the key. The browser UI allocates one UUID per pending submission, reuses it while the submission lacks a successful HTTP acknowledgment, and clears it only after `response.ok`. It no longer hard-codes one shared dashboard team name; the stable-key-derived default team owns identity.
6. Cron/Heartbeat paths are unchanged because they already derive deterministic idempotency keys from their trigger identities.
7. No new durable state, ledger, gate, service, registry, executor, or recovery protocol is added.

## Verification

- New focused identity regressions: PASS.
- Runtime-v2 runner/API/dashboard + real legacy attach bundle: **63 passed** before broadening.
- Runtime-v2 + Gate B + dependency + lifecycle-owner + real attach bundle: **164 passed**.
- Real legacy DashboardAPI/SQLite crash-window test now omits `team`: first remote command is accepted, local save is forced to fail, retry with the same key resolves the same command; no second command is created.
- Full suite from the R11 tree progressed to **573 passed, 25 skipped** with zero product assertion failures, then stopped because this container lacks Playwright Chromium headless-shell (`chromium_headless_shell-1200`).
- `python -m compileall -q src/playwright_auto`: PASS.
- `git -c core.autocrlf=false -c core.whitespace=cr-at-eol diff --check`: PASS.
- R9 remains the broad non-browser baseline (**1570 passed, 42 skipped**); R8 remains the latest complete browser-capable internal full-suite baseline (**1905 passed, 54 skipped**).

## Remaining boundary

- Official current-source FJP/Windows + disposable browser/CDP acceptance remains external evidence.
- A fresh M365 Opus 5.5 post-review remains pending transport health; the previous new review attempt failed before Send, so no Opus opinion is claimed.
- The runtime-v2 generic API contract now intentionally requires callers to supply stable logical intent identity. Callers that cannot preserve such identity must fail before the remote side effect rather than invent a new key on retry.