# CDPA Self-Evolution: Mandatory Goal and Rules

Owner: G6 `DESKTOP-B5E4IUE`, repository `E:\git-project\cdpa`.
Version: 2026-11-07. Status: binding operator directive for this campaign.

## Mandatory entrypoint

Every Pi coordinator, CDPA Solo, PLAN, DEV, TEST, REVIEW, AUDIT and successor recovery task MUST read this complete file **before any implementation, review, test, browser action or route decision**. The task constructor MUST include its absolute path and the full campaign brief below. Successors MUST reread this file. If unavailable, preserve evidence and record a safe hold; do not pretend to comply.

## Full immutable user goal

Build CDPA + Pi Coding into an autonomous harness comparable in purpose to Hermes. From one user prompt, it must autonomously plan, delegate, edit, test, review, deploy, verify, operate and improve **CDPA itself** without routine operator intervention. CDPA must operate reliably, with no silent mid-task block or dead run. Whenever any execution is unable to progress, Pi must create a narrowly scoped successor/recovery task with the entire campaign goal, its subsidiary requirements, and the no-silent-stop rule. Every task must carry this exact mission and have continuity of ownership until the complete mission has independently proven end-to-end acceptance.

Two independent reviews are mandatory for significant changes:
- M365 Copilot Opus on **FJP** for architecture, simplification, over-engineering critique and independent reasoning; advisory only.
- CDPA Solo REVIEW on **G6** to inspect actual CDPA code, evidence and tests.

All source implementation, test runs and CDPA runtime modifications are **on G6 only**, and must be executed by CDPA agents, not by Pi. Pi Coding acts as the highest-authority **operational coordinator**: interrogates state, prioritizes, delegates, monitors, resumes and orchestrates recovery. It may control G6 task lifecycle and CDPA runtime only through verified existing control interfaces. It must not itself implement CDPA product features.

## Hard rules

1. Never silently terminate work before all acceptance criteria pass. Each unsuccessful task must persist a reason plus **an atomically committed successor** with full goal OR an explicit safety hold requiring further reconciliation. When a safety hold is resolved, Pi must continue via a new task. No unbounded spawn storms or handoffs to the same stuck state without new evidence.
2. Do not use Windows minute-by-minute Scheduled Tasks as the coordinator. Run a long-lived Pi-supervised process with visible heartbeats, retained output, restart state and a single runtime owner. Do not install duplicate schedulers or conflicting process supervisors.
3. CDPA agents own coding, repairs, upgrades, tests, independent code review and release evidence. Pi owns continuous operation, prioritization, task orchestration, health detection, Resume/Stop/Clear where approved, and safe handoff.
4. M365 **Opus** must independently challenge meaningful design/code changes to prevent over-engineering. Provide evidence, ask for the smallest owner/fix, request falsifiable tests and reject unnecessary abstractions. No claim of Opus review without an actual completed response.
5. Pi must never infer success from HEALTHY API or an accepted Send. Acceptance requires exact owned assistant terminal route, tests, release evidence, correct task identity and no unrelated-tab interference.
6. Preserve existing CDPA durable request/command provenance and exact-once Send guarantees. Never resend an accepted or ambiguous ChatGPT request. No F5 of Temporary Chat, no stealing another user's tab, no bypass of login, CAPTCHA or Cloudflare challenge.
7. On a genuine provider challenge on **G6 9202**, Pi may request a bounded **restart of only the owned 9202 browser** using `C:\Users\admin\Desktop\9202_PLUSHCU.bat`, after preserving normal-tab URLs/drafts and reconciling existing accepted Send and ownership. Restart is not permission to bypass challenges or resend an accepted request. Do not touch separate 9212/FJP profiles. If challenge persists or ownership is ambiguous, enter documented safe hold, continue diagnosis without unsafe browser actions.
8. Persist every task's parent, exact identity, owner, lifecycle, evidence, and continuation decision. No competing owner may operate the same browser/request. A safety hold is not mission DONE.
9. Preserve unrelated dirty files. Root-cause first, focused tests then broad tests, code review and real E2E; source/drift reload must not restart Chrome. Keep a reversible implementation.
10. Remove the obsolete Windows Scheduled Tasks `CDPA-Pi-SelfRepair-G6` and `CDPA-G6-HealthWatchdog`; do not resurrect them. The new coordinator must have a documented single-instance launch and recovery method.
11. Do not call the full campaign DONE until G6 independent release gates, live task continuation, restart recovery, noninterference, broad regression, and both review paths are all independently proven.

## Exact 9202 browser recovery commands (include in EVERY agent prompt)

These commands are for recoverable G6 9202 session failure **after** reconciling the exact CDPA task, accepted/ambiguous Send and owned tabs. Preserve normal URLs and unsent drafts before closing. Do not touch 9212 or FJP.

From PowerShell in `E:\git-project\cdpa`:

```powershell
# Review exact 9202 ownership, accepted Send and task state FIRST.
# Save current normal tabs/drafts with the existing read-only capture helper:
.venv\Scripts\python.exe .runtime\self-repair\backup_browser_9202_20261030.py
# Close only 9202 with CDP Browser.close (not taskkill / all Chrome):
.venv\Scripts\python.exe .runtime\self-repair\close_browser_9202_20261030.py
# Restart the dedicated HCU profile and reconnect:
Start-Process -FilePath "C:\Users\admin\Desktop\9202_PLUSHCU.bat"
# Restore only confirmed normal conversation URLs, without retrying Send:
.venv\Scripts\python.exe .runtime\self-repair\restore_browser_9202_20261030.py
```

The existing scripts use a fixed historical backup filename. Before operational use, ensure the captured backup belongs to the current incident and preserve the previous historical backup; never restore stale URLs by accident. A browser restart may restore session readiness but MUST NOT solve/bypass CAPTCHA or replay an accepted request.

## Mandatory task-spawn command (applies to every generation)

Do not construct a shortened `cdpa` prompt or forward only a summary. Every agent must use the canonical constructor below to submit any new CDPA task. The constructor reads this entire file at task creation time and embeds its complete current contents into the actual `--goal` argument; `--task` is only a concise display title. The agent must pass its bounded instruction and a unique team; Pi must reconcile current task ownership before dispatch. For each new Solo task, inspect the actual first-turn `hops[0].prompt` and receipt before treating dispatch as correct: both must contain the full campaign goal, the exact bounded instruction and the canonical spawn command, not merely a populated manifest goal field. Never Send a task with only the generic Solo role header.

```powershell
cd E:\git-project\cdpa
.venv\Scripts\python.exe workflows\self-evolution\spawn_cdpa.py --role REVIEW --team <unique-team> --instruction "<bounded review task>" --parent <parent-task-id>
```

Do not treat the above as permission for each child to spawn its own successor. Pi is the sole dispatch owner. The instruction for a child must explicitly tell the child to read this policy and use this exact constructor for any further delegation, even after 10+ generations; test full-text inclusion, not just a file path. Never send a second accepted or ambiguous request.

## Delegation

- Pi coordinator: read this, inspect live task and exact ledger, identify highest-value safe next action; submit or reconcile one task, observe evidence, and keep going while mission incomplete.
- CDPA G6 Solo: read this + AGENTS.md; inspect source/requirements/tests; implement or review one bounded slice; attach evidence; hand off to Pi.
- M365 FJP Opus: read the complete goal (include this text in supplied prompt); analyze evidence, identify smallest existing owner and concrete falsification tests, separate observation from hypotheses; never edit G6 code.
- Any recovery successor: inherit the full text, parent task ID, exact request/Send state, evidence locations, failure reason and next safe diagnostic.

## Acceptance

Demonstrate persistent coordinator liveness; real Pi delegation, real G6 CDPA Solo and FJP Opus completion, atomic successor handoff across crash, idempotent Resume/no Send replay, verified restart of CDPA-only runtime on drift, safe bounded browser restart on allowed challenge, no lost task, broad regression PASS and actual full campaign completion. Anything unproven is NOT_ACCEPTED.
