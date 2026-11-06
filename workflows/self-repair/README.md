# G6 CDPA Pi Supervisor

Control plane: Windows Scheduled Task `CDPA-Pi-SelfRepair-G6` every 30 minutes (maximum execution 26 minutes). Executable `.venv/Scripts/python.exe workflows/self-repair/supervisor.py once`, running as the logged-on G6 user. Pi root-cause repair uses `cliproxy-g8/gpt-5.6-luna` at thinking `high`. Each check starts a fresh Pi CLI invocation; MISSION.md, PROGRESS.md, receipts, and the runtime ledger carry context, avoiding the oversized previous Pi session.

## Operator commands from repo root
- `python workflows/self-repair/supervisor.py status`: read current phase/task
- `python workflows/self-repair/supervisor.py once`: safely reconcile one tick
- `python workflows/self-repair/supervisor.py pause`: hold successor creation
- `python workflows/self-repair/supervisor.py resume`: release hold
- `powershell -NoProfile -ExecutionPolicy Bypass -File workflows/self-repair/reload_cdpa.ps1`: reload only G6 CDPA API/dashboard/worker, not Chrome. Use `-CheckOnly` to inspect launcher ownership.
- `Get-ScheduledTaskInfo -TaskName CDPA-Pi-SelfRepair-G6`: confirm actual scheduler runs
- Dashboard: http://127.0.0.1:9224 ; API: http://127.0.0.1:9225

## Files
- `MISSION.md` defines the full goal and release requirements
- `slices.json` defines stable small tasks
- `PROGRESS.md` is regenerated each tick (do not hand-edit)
- `.runtime/self-repair/state.json` is the authoritative campaign ledger
- `receipts/<slice-id>.json` is the per-Solo evidence
- `.agents/skills/cdpa-self-repair/SKILL.md` must be read once by every new Solo

## Operator precedence: reload CDPA before waiting for ChatGPT
The owner explicitly prioritizes reloading G6 CDPA when `SOURCE_DRIFT` or a stalled Solo is detected. Stop the CDPA task and reload only the CDPA API/dashboard/worker using the existing launcher. Browser tabs remain untouched and are independent of the CDPA process. Recover task status and accepted Send provenance from the durable ledger, then launch the next fresh Solo. Never replay a prior accepted Send, and never claim the release gate passed merely because the restart worked.

## Fail-safe behavior
One task at a time and one idempotency key per slice. Reconcile any prior task before creating another. If task is blocked, Pi may diagnose/repair locally, but cannot blindly replay an ambiguous Send or steal an unrelated page. No new Solo is admitted until the previous browser writer is reconciled. A stale, receipt-backed code slice may request a single version-checked CDPA Stop after 30 minutes of no activity and no Stop/Retry/composer, then advance only after the exact Stop is applied. This never makes the old Send DONE and never applies to `08-real-browser`. Source drift permits reading/retiring the old task but blocks a new task until the worker is safely reloaded. Pi timeouts do not change release status. No silent downgrade to a different model. A durable NEEDS_PI state is retryable only within the configured bounded recovery budget; credentials, login or uncertain browser identity require an explicit hold.

## Release
`NOT_ACCEPTED` until real isolated ChatGPT CDP acceptance and broad regression evidence are present. Healthy HTTP/worker and a green focused test alone are insufficient.
