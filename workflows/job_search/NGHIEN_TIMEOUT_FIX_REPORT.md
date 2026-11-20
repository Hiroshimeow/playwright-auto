# Nghien CDP connect timeout fix - 2026-11-17

## Cause and change
- Before editing, confirmed both `connect_over_cdp` calls in `nghien_cycle.py` (lines 230 and 250) used `timeout=12000`. Live logs showed repeated attach timeouts after WebSocket connection; `last_scan` was stuck at `2026-11-16T20:41:16.527748+00:00`.
- Fresh recovery probe (`.venv/Scripts/python.exe workflows/job_search/muse_browser_recovery.py probe --port 9202 --deep`) exited 0: HEALTHY, HTTP/CDP true, 12 tabs, zero challenge tabs, Playwright `ATTACHED 1` using its 30-second budget. This supports insufficient attach budget, not an unavailable browser.
- Changed only those two connect timeouts to `30000`, matching Freelancer and recovery. Eligibility pre-filter, stage machine, exactly-once rules, 30-minute interval, and all other timeouts are unchanged. Neither `freelancer_cycle.py` nor `muse_browser_recovery.py` was edited.

## Regression tests
Added `tests/test_nghien_cycle.py`: two isolated mocked Playwright tests exercise scan and detail attach and assert timeout 30000. They also verify existing navigation, page wait, and text-read timeouts remain unchanged. Both tests failed against the original values and passed after the fix.

Command (repo venv):
```text
.venv/Scripts/python.exe -m pytest tests/test_nghien_cycle.py tests/test_freelancer_cycle.py tests/test_muse_browser_recovery_timeouts.py -v
```
**42 passed**: Nghien **2**, Freelancer **37**, recovery timeouts **3**.

## Restart
- Read-only preflight found 11 listings, zero new candidates, no active endpoint task, and no pending stage actions; it invoked neither cycle nor task creation.
- Restart began `2026-11-16T22:27:15.814632+00:00` (2026-11-17 07:27:15 JST).
- Stopped only old Nghien launcher PID **22112** and its confirmed Python loop child/lock owner **72740**. Stopping the launcher alone would leave the child holding the Windows lifetime lock.
- Relaunched through unchanged `workflows/job_search/start_nghien_cycle.cmd`, matching the original command and `--interval-min 30`.
- New command wrapper **64240**, venv launcher **83048**, actual loop/lock owner **71228**. Both Python processes verified alive. Existing state and lock files retained; OS-level single-owner lock verified held.

## Live result: VERIFIED
First restarted scan advanced `last_scan` to **2026-11-16T22:27:37.337356+00:00** (2026-11-17 07:27:37 JST), after restart. Fresh appended log:

```text
NGHIEN_CYCLE_V2 started, interval 30 min
ALERT 7 nghien entr(ies) BLOCKED and parked (no auto-replay): nghien-job-ac21eb0b53c0, nghien-job-62cedb7b96ed, nghien-job-b81d6decac00, nghien-job-2d8642f825d7, nghien-job-6c4a5a5accae, nghien-job-3e299731659f, nghien-job-5fdd222c9c77
NO_NEW_ELIGIBLE_CANDIDATE 11
```

No new HOLD, cycle failure, dispatch, or retry for this scan. No second restart was needed.

All created/skipped state entries compared equal before/after, including the seven parked BLOCKED entries. BLOCKED/PAUSED board membership and status compared unchanged. Browser 9202 WebSocket identity remained unchanged; no browser restart. Worker **82188** and Freelancer **6304** remained alive with unchanged creation timestamps and were not restarted. No bids, CDPA task dispatch, Send, or replay occurred.
