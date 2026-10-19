# Fresh-context continuation repair

## Outcome and evidence
Palace accepted hop197 once; its cached response was not admitted and current exact DOM/history does not prove a final route. Its pristine route-repair child198 carries minimal_continuation_prompt and generation2; later explicit New Chat selected fresh generation3 but retained pre-fix URL intent. No historical cached route can be promoted to a response.

## Contract
- Accepted parent receipts/ledger rows remain unchanged and are never replayed.
- Existing Change URL and Resume controls remain the only operational path; no new store/control/state.
- Superseding a pristine repair for newer URL intent preserves its turn, exact report, validation, attempt, and route/report locks, including continuity context generation. URL supersession invalidates the context-free minimal flag even at the same conversation generation.
- A minimal continuation is valid only in its original conversation generation, without fresh acquisition. Fresh-context repair uses the existing self-contained repair contract, workspace and exact report; never original substantive task replay or invented route.
- Current generation fencing and locked persistence remain authoritative. NEW_PENDING with verified allocated page is as usable a pre-Send target as BOUND; no fresh page implies no admission.

## Plan and verification
1. RED regression for context-changed continuation and repair metadata through URL supersession.
2. Consolidate the two existing pristine URL supersession blocks into one owner, preserving repair fields; extend existing prepared-target condition to verified NEW_PENDING.
3. Select existing format-repair prompt rather than generic continuation after a new context; preserve same-context generic behavior.
4. Focused tests, relevant worker/URL/repair tests, independent review and local checkpoint.
5. Fresh shared safety capture before minimum worker reload. Apply existing fresh role-URL control only after exact receipt/pristine/draft gates; Resume original workflow once. Verify a new repair request (not accepted197 replay), valid response/report routing, and semantic progress.

## Source verification gate (2026-10-19)
Initial regressions: 5 failed / 1 passed, proving generic prompt after generation change, lost repair metadata, and blocked verified NEW_PENDING. Independent review found allocation-generation and deferred-readiness holes; both reproduced RED, corrected in existing owners, then re-reviewed Ready. Same-generation canonical URL regression also reproduced RED and now passes. Parent full relevant selection: 304 passed, 21 skipped. Reviewer: 53 focused passed; 153 worker/execution/report passed, 21 skipped. compileall and git diff --check passed. Live recovery remains unverified at this gate.

## Non-goals and stop boundary
No historical result forgery, ledger surgery, backend-history routing, business implementation, provider Retry/Regenerate, browser restart, or Lantern semantic actions. If new final output is unavailable due to provider/human/irreversible state, persist exact external condition rather than claim recovery.
