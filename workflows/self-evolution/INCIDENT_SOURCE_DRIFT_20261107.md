# Live CDPA source drift during Pi-dispatched TEST — 2026-11-07

Exact CDPA task: `cdpa-idem-de15d32234531e7e0f01c19f`, team `g6-evolution-coordinator-crash-test-20261107-r1`, endpoint G6 `9202`. Created by Pi cycle 2 from parent `cdpa-idem-8a09e7d357e1fd22c6e965d2`. Its first-turn Solo prompt contains the full canonical campaign goal and bounded TEST instruction. Request `...-hop1` accepted exactly once; `requests.json` recorded `status=sent`, `attempts=1`. At last check task `RUNNING/wait_response`.

The worker's live `/health` changed from HEALTHY to `SOURCE_DRIFT`, with `worker_source_drift.changed` = `src/playwright_auto/cdpa_routes.py` and `src/playwright_auto/cdpa_worker.py` at 2026-11-06 19:07 UTC. File mtimes around G6 04:07 local. Both source files have new uncommitted edits; **the editor/owner of these changes has not been established**, so do not attribute them to the TEST.

Safety: Do not restart CDPA API/worker while this accepted Send is still awaiting its assistant terminal route, and do not retry or resend its request. Continue to observe exact task progress, then when terminal or a precisely reconciled safety hold exists, use the existing G6 CDPA-only reload procedure `workflows/self-repair/reload_cdpa.ps1` after `-CheckOnly`; this script must not restart browser 9202/9212. Verify new worker health SOURCE_DRIFT=false, original task manifest and exact request attempts remain 1, 9202/9212 listener identities unchanged, and no duplicate successor.

The Pi coordinator process owner `run_pi.py` remains independent of CDPA runtime and should not be stopped for source drift. No forced CDPA restart, browser Send/reload, or task control was performed in this incident note. Full campaign release remains NOT_ACCEPTED.


## Pi reconciliation hold — 2026-11-07 04:36 JST

- **PI_ACTION:** SAFETY_HOLD.
- **Failed invariant:** CDPA-only reload returned `RELOAD_OK`, but the subsequent `/health` check again reported `SOURCE_DRIFT` for `src/playwright_auto/cdpa_worker.py`; its mtime is `2026-11-07 04:34:44 +0900`, after the reload boundary.
- **9202 ownership:** no G6 self-evolution task is RUNNING/WAITING/PAUSED after exact Stop/Clear reconciliation. Historical accepted requests remain preserved with `attempts=1`; no request was replayed.
- **Runtime/process evidence:** API PID `48320`, worker lease owner PID `24596`; the resident Pi state remains stale at `.runtime/self-evolution/pi-runner.json` (`owner_pid=32200`, `pi_pid=30876`, iteration `6`, `SOURCE_DRIFT`). Process inspection also found multiple `run_pi.py` descendants; ownership/adoption is not independently proven.
- **Browser safety:** 9202 and 9212 target URLs were unchanged by the CDPA-only reload. No browser restart, focus, Send, Resume, Retry, or Temporary Chat reload was performed. FJP was untouched.
- **Next falsifiable action:** identify the editor/owner of the uncommitted `cdpa_worker.py` change and reconcile the Pi process chain by read-only evidence; only after source identity is stable and `/health` remains `worker_status=HEALTHY`, `worker_source_drift.drifted=false`, and `worker_reload_required=false` may Pi dispatch exactly one fresh canonical successor.
- **Acceptance:** campaign remains `NOT_ACCEPTED`; no release claim.
