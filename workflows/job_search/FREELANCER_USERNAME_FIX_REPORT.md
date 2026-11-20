# Freelancer username readiness fix - 2026-11-16

## Result
Deployed with **one restart: PID 79520 -> 6304**. Same command and 30-minute interval, **FREELANCER_SCAN_ONLY=1**, owner lock held. Fresh production scan: **detail_checked 6 / eligible_checked 6**. No bids, Send or CDPA task creation.

## Root cause: live diagnosis before editing
Inspected on browser 9202 using only disposable pages:
- https://www.freelancer.com/projects/selenium/Python-Automation-for-Portal-Updates/details
- https://www.freelancer.com/projects/data-analysis/Freelance-Profile-Setup-Coaching/details

Account is ordinary light DOM under app-user-card, a p.Username-userId containing exactly @duonghcu. Portal Updates has two exact matches (navigation account and bid preview); Coaching has one (navigation account). Other usernames are comments. No open shadow roots, whitespace mismatch or hidden-first duplicate observed. Both Portal matches visible with nonzero rectangles (account 84 x 23.51 px, preview 85.17 x 23.51 px). Existing .first already avoids strict multi-match errors.

**Original timeout reproduced live**: reproduction.json attempt 6 failed after **12.125 seconds**; immediate DOM snapshot showed both exact visible paragraphs and readable Project Details. Non-strict text-presence predicate passed on that same page in **0.140 seconds**, without navigation/auth changes. Initial dom-probe also reproduced a timeout.

Timed probes establish late asynchronous hydration: zero usernames at DOMContentLoaded; Project Details observed at 10.890 seconds after navigation, account identity at 12.343 seconds. Mutation observers confirmed delayed insertion.

Root cause at repaired boundary: authentication readiness depended on one visible-first locator's polling/deadline during hydration. Session-account presence and completion of that wait are not equivalent. Strictness, whitespace and shadow DOM do not explain the reproduced case. A visible element immediately after timeout does not prove it was visible throughout the preceding wait; no proven Playwright engine bug is claimed. The observed hydration/polling race is repaired without visibility or single-duplicate selection.

Evidence directory: .runtime/job-search/username-fix/ (dom-probe.json, timed-probe.json, reproduction.json, hydration-timeline.json).

## Fix
Only _verify_page changed in workflows/job_search/freelancer_cycle.py:
- Authorized host checked before and after readiness.
- Non-strict all_text_contents on **app-user-card .Username-userId**; any exact @duonghcu after trimming satisfies identity.
- Python monotonic 30-second deadline, 200 ms polling; no .first, visibility state or renderer-timer wait. Missing/wrong account fails closed; browser errors propagate.
- Account-card scope excludes public comments and bid-preview usernames as session evidence.

AST comparisons against byte-preserving pre-edit snapshots confirm fetch_detail_text, classify_location and _freelancer_context unchanged. Previous Project Details readiness fix, URL/auth/content-length/section-splitting/eligibility guards preserved. No new dependencies/config/state model.

## Tests
Six readiness tests replace one (net +5): old visible-wait failure with present identity, duplicate/hidden-first-independent presence and whitespace, hydration beyond old deadline, absent/wrong/prefix/case identity, host guards before/after readiness, browser error propagation. Existing guards and scan-only tests retained.

Repository .venv/Scripts/python.exe:
```text
-m pytest tests/test_freelancer_cycle.py -q
37 passed
-m pytest tests/test_freelancer*.py tests/test_muse_*.py -q
65 passed (final post-deployment run: 3.14s)
```
All five Muse suites included (28 tests): auto_repair, browser_recovery_timeouts, cdpa_bridge, cdpa_followup, pi_coordinator. Evidence: pytest.txt. git diff --check passed (only unrelated existing line-ending warnings). Actual task diffs reviewed against snapshots because these files were already untracked. Unrelated dirty work preserved.

## Live verification before restart
Fixed fetch_detail_text: Portal Updates **3226 characters**, Coaching **1941 characters**; both Project Details present, UNKNOWN eligibility (not bid authorization).

Full live main(['--scan-only']) using isolated copied state and separate probe lock while old loop retained production lock: **82 listings, 32 candidates, 6 details / 6 eligible**, rc 0; last_scan 2026-11-16T14:33:29.559699+00:00. create_task instrumented to raise if called; never called. No second production writer. Evidence: fixed-details.json, predeploy-scan.json.

## Deployment and production verification
Captured/reused exact PID 79520 command:
```text
"C:\Users\admin\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"  -u E:\git-project\cdpa\workflows\job_search\freelancer_cycle.py --interval-min 30
```
Preflight verified exact interpreter imports Playwright with existing venv-launcher context (__PYVENV_LAUNCHER__ pointing to repository .venv/Scripts/python.exe) and FREELANCER_SCAN_ONLY=1. No packages/environment files changed.

Old owner lock held. Log/state unchanged across three-second idle check; PID command and creation time rechecked immediately before targeted Stop-Process -Id 79520. Not a tree kill. **Idle OS termination, not cooperative graceful shutdown.** Lock release confirmed before exactly one replacement launch with captured command, repo cwd, existing append log, DEVNULL stdin, detached/no-window flags. No failed replacement/additional restart.

Replacement **PID 6304**, started **2026-11-16T14:36:24.603414Z (23:36 JST)**. Separate lock probes failed after startup and again after completed scan, confirming lock ownership while replacement alive. CIM confirmed same command/executable.

Production state saved only by loop:

| Field | Before | After |
| --- | --- | --- |
| last_scan UTC | 2026-11-16T13:38:03.037402+00:00 | **2026-11-16T14:37:38.605747+00:00** |
| last_scan JST | 22:38 | **23:37** |
| listings / candidates | 82 / 30 | 81 / 32 |
| detail_checked / eligible_checked | 4 / 4 | **6 / 6** |
| created count | 0 | **0** |

Completion verified 14:39:44.987562Z (23:39 JST). last_scan assigned after discovery, before details finish. New log has six ELIGIBILITY lines and ends:
```text
SCAN {"listings": 81, "candidates": 32, "detail_checked": 6, "eligible_checked": 6}
SCAN_ONLY
```
No username timeout/HOLD in deployed scan. Historical detail_errors compare equal to pre-fix snapshot; deliberately retained, not manually cleared. Next cycle expected around 00:09 JST November 17 (sleep after completion); second scheduled recurrence not waited for.

Evidence: deployment.json, processes.before.json, processes.after.json in evidence directory. Deployed source SHA256: 8ac7654d945876d844caefbcea232d4a257290dc2bef9282931afdd9d67845db.

## Safety / limits
No bids, Send, CDPA tasks or dispatch; created remains empty. Nghien PID **22112** and worker PID **82188** retain identical command/executable/creation times before/after; neither stopped/modified. Browser not restarted; only probe-owned disposable pages touched. Paused jobs-cash-20261030 not resumed/modified. No manual production state edits. Loop remains **scan-only**.

Thirty seconds is bounded readiness, not a guarantee under arbitrary site/renderer failure; absent identity still HOLDs. One deployed full scan verified; future recurrences remain subject to site availability.
