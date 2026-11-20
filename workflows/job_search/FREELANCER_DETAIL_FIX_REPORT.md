# Freelancer detail readiness fix - 2026-11-16

## Result
Fixed and deployed. **New Freelancer loop PID: 79520**, alive with the owner lock. First scan: **detail_checked 6 / eligible_checked 6**, previously **0 / 0**. The original 30-minute recurrence is retained, deliberately in scan-only maintenance mode: no dispatch, CDPA task creation, bids or Send.

## Root cause: live DOM on 9202
Used freelancer_page(): connect_over_cdp, new disposable page, close only that page; never browser.close(). Existing operator/worker tabs were not navigated.

Inspected both `https://www.freelancer.com/projects/api/Automation-OpenAI-Make-Integration/details` and the plain project URL. Both returned HTTP 200 and the same Details view. Explicit /details stayed there; the plain URL resolved to /details.

**The label exists in the Details section**, after Skills Required, not another tab. The page rendered title, Open status, Details/Proposals tabs, Project Details, budget, full description, skills, Project ID, bid UI, Clarification Board and About the Client.

DOM (Angular attributes omitted):
```html
<p class="mb-xxsmall mobile:hide text-body-12"> Project ID: 40820321 </p>
<p class="text-body-12 mobile:show"> Project ID: 40820321 </p>
<div class="ProjectDetailsCard-title mobile:hide ng-star-inserted"> Project Details </div>
```
Desktop ID is visible, mobile duplicate hidden. Both textContent values are `" Project ID: 40820321 "`. Desktop innerText appears trimmed, hiding the defect. Live counts:
- Original regex `^Project ID:`: **0** matches.
- Diagnostic regex `^\s*Project ID:`: **2** matches.

Playwright regex matching sees leading DOM whitespace, so the original anchored expression cannot match. This is not the earlier attach stall.

### Related username symptom
Live DOM contained the exact visible @duonghcu account-menu paragraph, plus another visible copy in the bid section. _verify_page passed repeatedly on fresh navigation/direct calls. No leading whitespace was observed in that username. The ID-label defect does **not** explain the recorded timeout that already resolved a visible username paragraph. That intermittent readiness/timing symptom was not reproduced or conclusively diagnosed. Authentication and host guards were not weakened; the deployed scan authenticated and read all six details successfully.

## Changes
`workflows/job_search/freelancer_cycle.py`:
- Readiness now waits for `page.locator('main').get_by_text('Project Details', exact=True).first` (20 seconds), not the incidental footer ID.
- Preserved canonical input URL validation, _verify_page, main extraction, split at Clarification Board / About the Client, Project Details presence and minimum 100-character check after splitting.
- classify_location unchanged, including explicit-location restrictions and UNKNOWN semantics.
- Added opt-in `FREELANCER_SCAN_ONLY=1`: keeps the original daemon command recurring read-only; guarded_dispatch also blocks before intent reservation/task creation. Default behavior unchanged without this variable. Existing --scan-only remains single-run.

`tests/test_freelancer_cycle.py`: eight new mock-based tests cover heading readiness without ID, both split boundaries, invalid canonical URLs, auth failure, readiness timeout, missing heading/short post-split content, identity/host guards and maintenance dispatch/daemon interlocks.

## Tests
Used `E:\git-project\cdpa\.venv\Scripts\python.exe` throughout:
```text
-m pytest tests/test_freelancer_cycle.py tests/test_muse_cdpa_bridge.py tests/test_muse_cdpa_followup.py -q
43 passed

-m pytest tests/test_freelancer_cycle.py tests/test_muse_*.py -q
60 passed (final run: 6.46s)
```
Freelancer: **32 tests**, 24 existing + 8 new. No separate Nghien-named suite exists in this checkout; filename and nghien_cycle/Nghien content searches found only the Freelancer test module. All available neighboring job-search/Muse suites were run, rather than claiming nonexistent Nghien tests passed. git diff --check passed (unrelated existing line-ending warnings).

## Live direct verification before restart
Imported fetch_detail_text while PID 85408 held its owner lock; no --once/cycle invocation:

| Canonical project suffix | Characters | Project Details | Verdict |
| --- | ---: | --- | --- |
| api/Automation-OpenAI-Make-Integration | 2731 | present | UNKNOWN |
| tensorflow/Driven-AmiBroker-Forex-Automation | 2561 | present | UNKNOWN |

Both returned non-empty details. Byte-for-byte production state comparison stayed unchanged; counters correctly remained zero until the loop scanned.

## Deployment
Read PID 85408's exact command first and reused it:
```text
"C:\Users\admin\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"  -u E:\git-project\cdpa\workflows\job_search\freelancer_cycle.py --interval-min 30
```
Original venv-launcher parent: 85916. The daemon had no cooperative shutdown handler or console. Its log ended with completed SCAN / NO_NEW_ELIGIBLE_CANDIDATE, idle for many minutes and unchanged during a further three-second check. Targeted Stop-Process only on 85408 between scans, not a tree kill. This was an idle OS termination, **not application-cooperative graceful shutdown**. No scan/state write was interrupted. Verified owner-lock acquisition/release before replacement.

Initial exact-command replacement 86064 logged HOLD readiness: No module named playwright. Windows base interpreter needed its original venv-launcher environment. Stopped only that idle replacement and verified lock release. Restored `__PYVENV_LAUNCHER__=E:\git-project\cdpa\.venv\Scripts\python.exe`; an import probe confirmed the exact base executable then resolves the repo venv/Playwright. No packages/environment files changed.

Final launch: captured command, repo cwd, append handles to freelancer-cycle.log, DEVNULL stdin, detached/no-window flags, restored venv context and FREELANCER_SCAN_ONLY=1. **PID 79520**, started **2026-11-16 21:30:07 JST**; alive, owner lock held, first scan completed.

## Before/after evidence
`.runtime/job-search/freelancer-cycle.json`, saved only by the loop:

| Field | Before | After |
| --- | --- | --- |
| last_scan UTC | 2026-11-16T11:57:56.990140+00:00 | 2026-11-16T12:31:26.516123+00:00 |
| listings | 79 | 80 |
| candidates | 31 | 31 |
| detail_checked | **0** | **6** |
| eligible_checked | **0** | **6** |
| created entries | 0 | 0 |

last_scan is assigned after discovery, before details finish. Six ELIGIBILITY UNKNOWN log lines cover Automation-OpenAI-Make-Integration, Driven-AmiBroker-Forex-Automation, Selenium-Gmail-Email-Automation, Python-Automation-for-Portal-Updates, Senior-Engineer-for-RAG-Support and Freelance-Profile-Setup-Coaching. Log ends:
```text
SCAN {"listings": 80, "candidates": 31, "detail_checked": 6, "eligible_checked": 6}
SCAN_ONLY
```
UNKNOWN means no explicit lock found after reading, not final authorization to bid. Historical detail_errors were deliberately not manually cleared.

## Safety / handoff
- No bids, Send, dispatch or CDPA task creation; production created remains empty.
- No production state manually edited; counters saved by the loop's own scan.
- Nghien PID **22112** and CDPA worker PID **82188** remained alive with unchanged start times; neither stopped/modified.
- Browser not restarted; existing tabs not navigated/closed.
- No changes to nghien_cycle.py, muse_browser_recovery.py or muse_pi_coordinator.py.
- Inline diagnostics through repo venv; no one-off scripts in TEMP/home.
- **Deployed loop remains scan-only.** A future authorized restart without FREELANCER_SCAN_ONLY restores ordinary dispatch; not done here.
