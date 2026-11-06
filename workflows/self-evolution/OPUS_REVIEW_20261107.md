# M365 Opus review 2026-11-07

Completed on FJP. Worker turn: 1b197046-m365-worker-20261107-025031-019cb2c0. Reviewer analyzed submitted evidence, did not independently inspect G6 code.

Accepted: old one-shot Pi with 20s timeout and exhausted recovery cannot autonomously operate; heartbeat alone is insufficient. Multiline task arguments through cmd.exe may be truncated: observed live r1 task_text 71 chars versus expected 7KB. Fixed by direct Python CLI; live r2 task_text 7836 chars with full goal and spawn instruction. One Pi operator should own task orchestration, CDPA owns browser Send and code changes. Avoid duplicate watchdog layers. Preserve dirty worktree.

Rejected: Opus proposed another SEND_INTENT table, which duplicates CDPA's existing durable Send owner. It also proposed a Windows Scheduled Task; operator requires deleting the two obsolete tasks and using a script. A bounded restart of owned 9202 may recover the browser session but must not automatically bypass CAPTCHA or replay accepted requests.

Required tests: Pi actually dispatches and monitors CDPA, task r2 reaches a verified terminal, complete prompt survives many generations, no duplicate accepted Send, process restart and single-instance lock, full regression and browser noninterference.

Verdict: design critique completed; release NOT_ACCEPTED.
