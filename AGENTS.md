# AGENTS.md

These instructions apply to the entire repository.

## Primary task entrypoint

After a free-form discussion becomes an actionable task, read this file and `LEARNING.md`, then submit the task through `cdpa` instead of directly editing the repository:

```bash
cdpa "<complete task with scope, constraints, acceptance evidence, and repository path>"
```

Optional task-start controls:

```bash
cdpa "<task>" --team <team-name>
cdpa "<task>" --new plan,dev,review
cdpa "<task>" --new-all
cdpa --team <exact-existing-team>
```

- With task text, `--team` requests a readable team base. If occupied, allocation appends `2`, `3`, and so on.
- Without task text, `cdpa --team <exact-existing-team>` resumes that team's one nonterminal durable task. It must preserve the same task ID, manifest, active hop, request ID, turn, report history, and route-repair state; it never allocates a suffix or creates a manifest.
- Taskless resume fails closed when the exact team is missing, terminal-only, corrupt, or has duplicate nonterminal manifests. An exact existing-team identifier is validated without normalization, lowercasing, sanitization, or truncation; allocated suffixes are part of the identity. Exact-team resume validates every raw catalog entry associated by key, declared team, or path, rejects malformed values/keys/paths/types and duplicate path claims, and compares all canonical index fields (`manifest_path`, `task_id`, `team`, `team_suffix`, `status`, `updated_at`) exactly against the manifest before selection and again inside the manifest lock. Corrupt metadata or state is never repaired or mutated implicitly. `--new` and `--new-all` are invalid in resume mode.
- Resume is only a durable continuation request. It continues the existing autonomous route chain until PLAN returns DONE; a one-turn helper response is not workflow completion.
- `--new role1,role2` starts those roles with a new tab or New Chat exactly once for the task.
- `--new-all` applies that rule lazily to every role PLAN later selects.
- `cdpa` creates the task and returns immediately. Progress and controls live on the Kanban dashboard.

## Task-writing contract

A submitted task must state:

1. exact repository/worktree;
2. requested outcome;
3. scope and explicit non-goals;
4. constraints such as no commit/push or preservation of dirty files;
5. required tests, runtime checks, screenshots, logs, or other evidence;
6. completion authority and expected final report.

Do not prescribe a large implementation when the task can state behavior and acceptance criteria. PLAN chooses the smallest suitable team.

## CDPA workflow invariants

- PLAN is the entry role and the final reporting role.
- PLAN lazily selects and routes only the roles needed for the task: DEV, TEST, REVIEW, and AUDIT.
- Roles may route to themselves or another logical role without a loop limit.
- REVIEW or AUDIT may declare implementation work clean, but must route back to PLAN for the final report.
- Only PLAN may mark the task DONE.
- Independent agents are normal manifest-backed CDPA tasks with `task_mode="independent"`, one `AGENT` role, one exact team, one conversation, no workflow routing, and no route JSON. The task itself is the standby/active job holder; do not add another scheduler, queue, event store, coordinator, or sidecar.
- Each enabled independent agent has exactly one nonterminal task. The worker claims the oldest eligible canonical event, sends the saved system prompt plus the shared independent rule once per conversation generation, accepts plain Markdown, and waits for an explicit `independent_continue` or `independent_complete` command. Completion creates exactly one deterministic successor with the same immutable agent identity, settings, watermarks, and conversation URL.
- Maintainers and Monitor are built-in identities on the same engine. Maintainers owns the exclusive recovery trigger by default and may use at most five cycles. Monitor uses interval, completion, role, team-state, CHECK_ALL, and Run-now triggers and may activate Maintainers by immutable name. Custom independent agents use the same creation and trigger machinery; team-member mode is not implemented.
- Worker controls persist immutable command snapshots and explicit origin (`operator`, `independent_agent`, `worker`, or `repair_task`). An independent-agent target control is valid only for its active canonical event. Explicit operator Pause/Stop/Restart/New Chat/Clear Team remains authoritative. A control is `applied` only after its action-specific operational postcondition passes; ineffective recovery stays explicit and retryable without hiding a final user step.
- Dependency manifests persist only `depends_on_task_ids`; children are derived. Independent agents create or reuse normal repair tasks through `independent_create_repair`. `CONTINUE_IN_PARALLEL` leaves safe work running. `HOLD_FOR_REPAIR` appends the repair dependency, moves the affected task to WAITING, preserves its exact hop/request/receipt/report provenance, and releases the same hop after repair DONE.
- Repair requests remain repository-bounded and root-cause deduplicated: root cause/reason ≤1200 characters, reproduction ≤2400, 1–8 allowlisted source areas, 1–16 one-line required tests ≤300 characters each, and an optional one-paragraph lesson ≤600. Validation occurs before task or dependency mutation. The worker never parses repair/action instructions from assistant prose.
- One active task owns each exact team's role tabs. `--reuse-team` queues work for that exact team without suffix allocation, and queued tasks reuse role conversations only through the existing safe rebind path.
- In inline report mode, the worker materializes the exact role report from accepted Markdown without weakening route, provenance, stability, or report validation.
- Upload identities are captured at task creation. Before a new upload/send, validate and upload the same immutable byte snapshot once per role conversation generation; pre-send source drift fails closed. After an exact request crosses the irreversible Send boundary, recover from the exact durable request ledger without rereading mutable source bytes or duplicating upload/Send.
- Dashboard attachment projections expose sanitized filename, size, MIME type, and hash prefix only; raw attachment paths or contents must never enter the dashboard payload.
- After a send is accepted, consume only a new assistant message after the persisted pre-send baseline. Do not re-validate the already-sent user prompt from rendered DOM text.
- Parse and validate the assistant route JSON and report. A malformed route is repaired in the same tab with compact identity, the validation error, the shared guide, and the generic report naming rule, at most three times. Never resend the original task, handoff, constructor, or an exact expected output path.
- While no valid response has been accepted, track the latest assistant/transport activity signature and F5 only after 20 minutes without progress while the composer is empty. Stop may be visible or may have disappeared behind transport timeout UI. Persist refresh timing, activity signature/last-change time, and pre-refresh assistant identities; never click Retry or Regenerate.
- Complete a hop only after one valid route JSON object and its report validate and the candidate is unchanged across at least two samples. Invalid output while Stop or transport UI is active remains waiting; route repair is allowed only for a clean, inactive candidate that stays malformed through the stability grace period.
- A valid non-DONE route repeats the same cycle in the requested next role: reuse exactly one matching role tab, otherwise open one tab, assign its physical role, bind team/task identity, and send the next handoff.
- Agent prompts use one allowlisted envelope only: `title`, `task-id`, `team`, physical `role`, physical `source-role`, `turn`, `workspace`, logical `allowed-routes`, `goal`, and exact `handoff`. The constructor is included only on the first conversation generation; the shared response guide is included on every normal prompt. Team rosters, logical-role duplicates, controller/run metadata, hashes, timestamps, page/transport fields, manifest/ledger paths, and worker-computed exact report paths stay internal. The durable transport must send that exact agent-facing text and must not append request markers or IDs.
- Browser actions must preserve durable request identity and never blindly resend an ambiguous in-flight prompt. The CDPA worker is a long-lived CDP reconnect supervisor: transport/browser disconnects escape per-task error handling without converting manifests to `BLOCKED`, then manifests and ledgers are reloaded on the next connection. Before declaring the response budget exhausted, perform one final bounded provenance-aware read and route/report validation.
- Before any automatic recovery refresh, persist the visible pre-refresh assistant message/turn identities and content fingerprints. After refresh or worker restart, those responses remain stale even if the DOM assigns new IDs. Recovery may refresh once under policy; it must never click Retry or Regenerate.
- Physical role names are team-scoped: `<team-base>-plan`, `<team-base>-dev`, `<team-base>-test`, `<team-base>-review`, and `<team-base>-audit`; append the team slot only on collision, for example `prompt-design-plan2`. Durable tab ownership is the unique physical-role + team + task tuple. `page_id` is replaceable runtime metadata, except that an accepted in-flight receipt remains strictly bound to its persisted page. Duplicate exact owners fail closed before mutation.
- Persist the accepted user message/turn identity for every markerless send. Long prompts may be collapsed behind `Show more`; rendered text is never authoritative post-acceptance provenance. After F5/rehydration, either persisted non-empty message ID or turn ID may prove the same accepted user request; when durable identity exists, never fall back to visible-text matching. A legacy receipt may be upgraded only from exactly one post-baseline user message and must never trigger another send.
- Resume rechecks the existing hop in the same worker boundary and reports the resulting state. Retry is available only for an explicitly structured retryable block; it is not a substitute for Resume, ownership repair, manual-composer resolution, or ambiguous-send recovery.
- Clear Team is a durable idempotent lifecycle. Nonterminal clear requires confirmation and a fail-closed duplicate/inspection preflight before irreversible mutation. Once `CLEARING` is persisted, nonterminal work is durably abandoned and transitioned to STOPPED with no active hop before Stop/close attempts. Persist `stop_pending`, `close_pending`, `closing`, and `verify_pending`; count only tabs observed closed, then require a clean zero-match exact-owner verification before `CLEARED` and persist `verified_empty_at`. Inspection or verification uncertainty preserves `CLEARING` and the concrete error for worker restart. Clearing a DONE or STOPPED task preserves that terminal result and all reports.
- The dashboard separates active work, `OFFLINE / RECOVERABLE`, and `History / Reports`; CDP disconnection is shown as unknown availability rather than false offline state. A terminal or `CLEARED` task remains recoverable while any exact assigned-role tab is still observed and moves to History only after connected observation proves zero matches. The create form uses non-empty task text for creation and empty task text plus an exact team for durable resume. Polling uses keyed in-place DOM updates.
- Task discovery, direct load, exact-team resume, and atomic writes apply one strict primary-manifest validator: unique canonical filename from `task_slug`, exact three-part layout, full required task-state types, configured role/physical-role identity, hop/request/active-pointer consistency, self-declared path/team/task, and matching catalog key/metadata. Raw catalog validation is total for the requested exact team: non-object entries, malformed keys, missing/relative/out-of-root paths, missing team/task fields, invalid suffix/status/timestamp types, key/path/team/task mismatches, duplicate path claims, and any canonical catalog-to-manifest value mismatch fail before any manifest or catalog write. Validate proposed state before replacement. Malformed canonical-layout JSON remains diagnostic reservation state even without the catalog: reserve only its directory task ID and affected team suffix, while unrelated teams remain allocatable. Schema-shaped evidence, copied/partial manifests, request ledgers, locks, temporary files, and mismatched catalog entries never enter worker/dashboard task loops.
- Tabs without a role are free tabs. CDPA must not route to, reuse, or clean them.

## Shared files

- Global editable policy: `cdpa.yaml` at repository root.
- Durable task manifest: `.plan/<team>/<task-id>/<task-title-slug>.json`.
- One independent report per role turn:
  `.plan/<team>/<physical-role>_turn<N>_<task-id>.md`.
- A route handoff references the exact report path instead of embedding a full report.
- Only the worker/dashboard writes task manifests. In file report mode, agents write only their own role-report file. In inline report mode, agents must not create, edit, or write a role-report file; the worker materializes it from the accepted response Markdown.
- Every newly formed team must read `LEARNING.md` before planning or implementation.
- Every role reads `LEARNING.md`. Before routing DONE, PLAN adds only genuinely new, concise, reusable lessons; if there is no new lesson, it leaves the file unchanged. Any PLAN mutation must use `uv run python -m playwright_auto.cdpa_learning --repository <repository-root>` with exactly one JSON request on stdin containing `disposition`, `old_text`, and `new_text`; never mutate `LEARNING.md` through generic file tools. The worker never invents or auto-generates lessons.
- PLAN remains the normal workflow owner for final-task learning. After a verified recovery outcome, Maintainers may use the same shared `playwright_auto.cdpa_learning` operation for one bounded repository-root `LEARNING.md` edit. The operation owns the repository lock, exact-current-content precondition, UTF-8/Markdown/sanitization/duplicate checks, atomic replacement, fsync, and read-back verification; no other file becomes writable through this learning path.

## Engineering rules

- Preserve unrelated dirty work.
- Prefer existing primitives and the standard library; do not add a second orchestration engine, database, WebSocket, generic DSL, or dependency without demonstrated need. The only production orchestration path is `cdpa`/dashboard → `TaskStore` → `CDPAWorker`; do not restore the retired AgentFlow CLI or its independent runtime state bridge.
- Keep storage, routing, prompt construction, response policy, browser actions, worker orchestration, and dashboard controls as replaceable modules with narrow interfaces.
- Keep selectors and timing/response conditions out of flow-control code.
- Use TDD for non-trivial behavior, then independent correctness review and a ponytail pass.
- Never commit, push, merge, reset, stash, or switch branches unless the user explicitly requests it.
- Never call `browser.close()` on the persistent CDP browser.
