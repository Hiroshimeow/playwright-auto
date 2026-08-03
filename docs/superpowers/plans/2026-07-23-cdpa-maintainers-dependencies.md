# CDPA Global Maintainers, Dependencies, Queue, Inline Reports, and Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn CDPA into a dependency-aware Kanban runtime with one global operational Maintainers role, durable same-team queues, worker-written inline reports, optional file upload, and an error-first dashboard.

**Architecture:** Keep the existing production path `cdpa/dashboard -> TaskStore -> CDPAWorker -> ChatGPTPage`. Add a global Maintainers sidecar inside the existing worker process; it scans task manifests for operational incidents, uses one exact `MAINTAINERS` tab on CDP `9222`, returns one structured recovery decision plus an inline report, and never joins normal PLAN/DEV/TEST/REVIEW/AUDIT routing. Add dependency and queue readiness as pure manifest scheduling predicates before browser acquisition; do not introduce a database, WebSocket, second worker process, or second orchestration engine.

**Tech Stack:** Python 3.11+, stdlib JSON/filesystem/subprocess/HTTP, Playwright CDP, pytest, existing atomic file locks and durable request ledger, existing Tailwind CDN dashboard.

## Global Constraints

- Repository/worktree: `/home/ayumi/Workspace/git_project/playwright-auto-delay-actions`.
- Feature branch: `feat/cdpa-maintainers-dependencies`.
- Preserve the unrelated untracked file `CDPA_NEXT_SESSION_HANDOFF.md`; do not stage, edit, delete, or move it.
- Do not push, merge, or rewrite history.
- One implementation team only: `unstopable`.
- Implement phases strictly in order. A later phase may begin only after focused tests, full relevant regression, independent REVIEW, and PLAN phase closeout for the prior phase.
- Phase 1 is global Maintainers. Every later phase must integrate its new operational failures with Maintainers before that phase is accepted.
- `MAINTAINERS` is global to the connected CDP browser on port `9222`, not a member of any task team and not a legal normal route.
- Maintainers must never invoke itself, enter normal route JSON, mark a task DONE, edit production code, edit tests, or directly mutate task manifests.
- Maintainers may propose one allowlisted operational action; the worker validates and applies it through existing task controls or dedicated atomic graph operations.
- Maintainers reports are worker-written inline reports under `.plan/maintainers/<team>_turn<N>_<UTC timestamp>.md` using a colon-free UTC timestamp such as `20260723T123456Z`.
- Maintainers reads the current `LEARNING.md` content in its first conversation generation. After a resolved incident it may return one concise reusable lesson; the worker appends that lesson under the existing file lock only when it is non-empty and not already present.
- A task child whose dependency becomes `STOPPED` remains `WAITING`; it is not automatically STOPPED or BLOCKED.
- Maintainers is authorized to create a replacement task and atomically rewire affected dependencies when that is the smallest path to help the original team finish its requested outcome.
- Dependency storage is one-way: each child stores `depends_on_task_ids`; children are derived by scanning canonical manifests. Do not persist mirrored child lists.
- A dependency graph is a DAG. Reject missing parents, self-dependency, duplicate dependencies, and cycles before any manifest/catalog mutation.
- Only one active task may own an exact team’s role tabs. Additional tasks for that exact team are durable queue entries.
- `--report-back` and `--report-to` do not exist.
- Inline report mode lets the worker materialize the exact Markdown report path from assistant response text; it must not weaken route, provenance, stability, or report-path validation.
- Upload reuses the existing durable upload primitives and file identities. No new upload dependency and no repository auto-zip.
- Use TDD for every behavior. Add the smallest failing test first, run it red, implement the minimum code, run it green, then run the relevant wider suite.
- Use Ponytail full mode: reuse existing controls, locks, report validation, `DurableSendBlock`, `ChatGPTPage`, and dashboard polling. No speculative plugin interfaces.
- Commit at phase boundaries only after TEST and independent REVIEW accept that phase. Use one descriptive commit per accepted phase.

---

## File Map and Stable Interfaces

### New files

- `src/playwright_auto/cdpa_maintenance.py`
  - Global Maintainers state store, incident detection/deduplication, prompt/response contract, report materialization, recovery dispatch, and the global tab coordinator.
  - Stable public interfaces introduced in Phase 1:

```python
MAINTAINER_ROLE = "MAINTAINERS"

@dataclass(frozen=True)
class MaintenanceDecision:
    action: str
    reason: str
    role: str | None = None
    lesson: str | None = None
    replacement: dict[str, object] | None = None


def maintenance_incident_key(state: Mapping[str, Any]) -> str | None: ...
def ensure_maintenance_incident(state: dict[str, Any]) -> dict[str, Any] | None: ...
def parse_maintenance_response(text: str) -> tuple[str, MaintenanceDecision]: ...
def maintenance_report_relative(team: str, turn: int, at: datetime) -> str: ...

class MaintainerCoordinator:
    async def advance(
        self,
        tasks: Sequence[tuple[Path, dict[str, Any]]],
        browser_context: Any,
    ) -> bool: ...
```

- `src/playwright_auto/cdpa_dependencies.py`
  - Pure dependency validation, cycle detection, readiness calculation, derived children, and atomic replacement/rewire planning.
  - Stable interfaces introduced in Phase 4:

```python
@dataclass(frozen=True)
class DependencyReadiness:
    ready: bool
    waiting_on: tuple[str, ...]
    stopped: tuple[str, ...]
    missing: tuple[str, ...]


def task_index(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]: ...
def validate_new_dependencies(
    task_id: str,
    parent_ids: Sequence[str],
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]: ...
def dependency_readiness(
    task: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> DependencyReadiness: ...
def derived_children(tasks: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]: ...
```

- `src/playwright_auto/cdpa_defaults/prompts/cdpa/MAINTAINERS.md`
  - Global constructor. It states the no-code boundary, Ponytail recovery policy, one-action contract, no self-invocation, and task-completion support objective.

- `tests/test_cdpa_maintenance.py`
  - Unit tests for incident generation, deduplication, parsing, report naming, lesson append, global tab acquisition, and recovery controls.

- `tests/test_cdpa_dependencies.py`
  - Unit tests for DAG validation, readiness, STOPPED parent behavior, replacement, rewire, and queue release.

### Modified files

- `src/playwright_auto/cdpa_config.py`
  - Add the packaged Maintainers constructor path and maintenance timing/config values without adding `MAINTAINERS` to normal `roles`.
- `src/playwright_auto/cdpa_defaults/cdpa.json` and root `cdpa.yaml`
  - Add `paths.maintainers_constructor` and bounded Maintainers wait/refresh settings.
- `src/playwright_auto/cdpa_actions.py`
  - Extract/reuse a generic exact physical-role locator for the global `MAINTAINERS` tab. Do not duplicate DOM selectors.
- `src/playwright_auto/cdpa_store.py`
  - Validate optional maintenance/dependency/queue/upload fields; create queued/waiting tasks; provide allocation-lock atomic graph mutations.
- `src/playwright_auto/cdpa_worker.py`
  - Run scheduling predicates before normal hops, run the Maintainer coordinator once per worker iteration, and apply validated recovery decisions.
- `src/playwright_auto/cdpa_routes.py`
  - Add inline role-report parsing while preserving existing file-mode route/report validation.
- `src/playwright_auto/cdpa_prompts.py`
  - Emit file-mode or inline-mode response guidance from task options.
- `src/playwright_auto/cdpa_cli.py`
  - Add `--inline-report`, repeatable/comma-capable `--depends-on`, `--reuse-team`, and repeatable `--upload`.
- `src/playwright_auto/dashboard.py`
  - Accept new task fields, derive dependency/queue/maintenance projections, expose an ordered task timeline, and return actionable primary errors.
- `src/playwright_auto/dashboard.html`
  - Add WAITING lane, primary error panel, maintenance panel/report link, parents/children/queue sections, attachments, and correctly timestamped timeline.
- `AGENTS.md`, `LEARNING.md`, `README.md`
  - Document Maintainers, dependency and queue semantics, inline reports, upload, and CLI examples.
- Existing tests:
  - `tests/test_cdpa_core.py`
  - `tests/test_cdpa_worker.py`
  - `tests/test_cdpa_cli_dashboard.py`
  - `tests/test_cdpa_response.py`
  - `tests/test_dashboard.py`
  - `tests/test_upload.py`

---

# Phase 1 — Global Maintainers Operational Sidecar

## Task 1: Define the Maintainers state and response contract

**Files:**
- Create: `src/playwright_auto/cdpa_maintenance.py`
- Create: `tests/test_cdpa_maintenance.py`
- Modify: `src/playwright_auto/cdpa_store.py`

**Interfaces:**
- Consumes: existing task manifest fields `status`, `block_code`, `block_reason`, `stop_reason`, `active_hop_id`, `active_role`, `updated_at`, and `controls`.
- Produces: optional manifest field `maintenance`, `MaintenanceDecision`, deterministic incident keys, and report names.

- [ ] **Step 1: Write failing incident-deduplication tests**

```python
def test_blocked_task_gets_one_open_incident_for_same_snapshot():
    state = task_state(status="BLOCKED", block_code="role_offline", active_hop_id=3)
    first = ensure_maintenance_incident(state)
    second = ensure_maintenance_incident(state)
    assert first is second
    assert len(state["maintenance"]["incidents"]) == 1
    assert first["state"] == "OPEN"


def test_maintainer_failure_never_creates_an_incident_for_itself():
    state = task_state(status="BLOCKED", block_code="maintainer_failed")
    assert maintenance_incident_key(state) is None
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_cdpa_maintenance.py -q
```

Expected: collection/import failure because `cdpa_maintenance.py` and its functions do not exist.

- [ ] **Step 3: Implement the minimal incident model**

Each new task may contain:

```python
"maintenance": {
    "active_incident_id": None,
    "incidents": [],
    "last_resolved_at": None,
}
```

Each incident uses:

```python
{
    "incident_id": "maint-<sha256-prefix>",
    "key": "<task-id>|<status>|<block-code>|<active-hop>|<updated-at>",
    "task_id": "cdpa-...",
    "team": "team-name",
    "trigger_status": "BLOCKED|STOPPED",
    "trigger_code": "role_offline",
    "trigger_reason": "...",
    "source_hop_id": 3,
    "source_role": "DEV",
    "state": "OPEN|RUNNING|RESOLVED|ESCALATED",
    "turn": 0,
    "decision": None,
    "report_path": None,
    "created_at": "...",
    "updated_at": "...",
    "resolved_at": None,
    "last_error": None,
}
```

Rules:

```python
- BLOCKED creates an incident unless block_code starts with "maintainer_".
- STOPPED creates an incident unless terminal_state is DONE.
- CLEARING/CLEARED does not resume old tabs, but may later permit REPLACE_TASK.
- The same incident key is idempotent.
- A new incident is allowed only after status/hop/error/updated_at evidence changes.
```

- [ ] **Step 4: Extend manifest validation compatibly**

Do not bump `SCHEMA_VERSION`. Old manifests without `maintenance` remain valid. When present, require a mapping, a list of incident mappings, unique non-empty incident IDs, supported states, and an `active_incident_id` that references an OPEN/RUNNING incident.

- [ ] **Step 5: Run focused tests GREEN**

```bash
uv run pytest tests/test_cdpa_maintenance.py tests/test_cdpa_core.py -q
```

Expected: all selected tests pass.

## Task 2: Add the global Maintainers tab and durable conversation state

**Files:**
- Modify: `src/playwright_auto/cdpa_actions.py`
- Modify: `src/playwright_auto/cdpa_config.py`
- Modify: `src/playwright_auto/cdpa_defaults/cdpa.json`
- Modify: `cdpa.yaml`
- Create: `src/playwright_auto/cdpa_defaults/prompts/cdpa/MAINTAINERS.md`
- Modify: `tests/test_cdpa_maintenance.py`
- Modify: `tests/test_cdpa_actions.py`

**Interfaces:**
- Consumes: existing `ChatGPTPage`, `PageBinding`, `ChatGPTWorkspace.open_role`, action delay policy, and packaged prompt loading.
- Produces: one exact global physical role `MAINTAINERS` and `.plan/maintainers/state.json`.

- [ ] **Step 1: Write failing exact-global-role tests**

```python
def test_global_maintainer_reuses_exactly_one_maintainers_tab():
    pages = [fake_page(role="MAINTAINERS", page_id="maint-1")]
    acquired = asyncio.run(actions.acquire_global_role("MAINTAINERS"))
    assert acquired.page_id == "maint-1"
    assert acquired.created is False


def test_global_maintainer_duplicate_tabs_fail_closed():
    pages = [
        fake_page(role="MAINTAINERS", page_id="maint-1"),
        fake_page(role="MAINTAINERS", page_id="maint-2"),
    ]
    with pytest.raises(RoleOwnershipError, match="multiple global tabs"):
        asyncio.run(actions.acquire_global_role("MAINTAINERS"))
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_actions.py tests/test_cdpa_maintenance.py -q
```

Expected: failures because global-role acquisition is missing.

- [ ] **Step 3: Extract the smallest shared physical-role locator**

Add to `CDPATabActions`:

```python
async def acquire_global_role(self, physical_role: str) -> AcquiredRole:
    """Reuse exactly one role-only tab or lazily open it; never bind task/team."""
```

It must:

```python
- inspect only supported ChatGPT pages;
- match exact `snapshot.page_role == physical_role` and a non-empty page ID;
- fail on duplicates;
- call `ChatGPTWorkspace.open_role` only when no match exists;
- never bind task ID or team;
- preserve the same conversation across incidents.
```

- [ ] **Step 4: Add durable global state**

Store `.plan/maintainers/state.json` under its own file lock:

```python
{
    "version": 1,
    "physical_role": "MAINTAINERS",
    "page_id": None,
    "page_url": None,
    "conversation_generation": 0,
    "constructor_sent_generation": None,
    "turn": 0,
    "active_incident": None,
    "last_error": None,
    "updated_at": "...",
}
```

This file is not a task manifest and must never enter task discovery/catalog reconciliation.

- [ ] **Step 5: Write the constructor**

The constructor must explicitly state:

```text
You are the one global CDPA Maintainers role for CDP 9222.
Your only objective is to help the affected team complete its existing requested task.
Apply Ponytail full mode: choose the smallest operational recovery that preserves completed work.
Never modify source code, tests, requirements, role reports, or task deliverables.
Never invoke MAINTAINERS or route into the normal role chain.
Return exactly one allowlisted operational action and one inline maintenance report.
Prefer resume over restart, restart over replacement, and replacement only when the original task cannot safely continue.
```

On the first conversation generation, include current `LEARNING.md` text in the prompt. Do not require Maintainers to use filesystem tools to read it.

- [ ] **Step 6: Run focused tests GREEN**

```bash
uv run pytest tests/test_cdpa_actions.py tests/test_cdpa_maintenance.py tests/test_cdpa_core.py -q
```

Expected: all selected tests pass.

## Task 3: Send incidents, parse one decision, write report, and apply recovery

**Files:**
- Modify: `src/playwright_auto/cdpa_maintenance.py`
- Modify: `src/playwright_auto/cdpa_worker.py`
- Modify: `src/playwright_auto/cdpa_store.py`
- Modify: `tests/test_cdpa_maintenance.py`
- Modify: `tests/test_cdpa_worker.py`

**Interfaces:**
- Consumes: `DurableSendBlock`, existing worker controls, `TaskStore.update`, and `LEARNING.md`.
- Produces: a worker-owned Maintainers request/response lifecycle and one operational action per changed incident snapshot.

- [ ] **Step 1: Write failing parser and report-name tests**

Maintainers response format:

````markdown
# Maintenance report

Operational evidence and the smallest recovery choice.

```json
{"action":"RESTART_ROLE","role":"DEV","reason":"Owned DEV tab is offline before send acceptance.","lesson":"Prefer controlled role reopen before discarding a task hop."}
```
````

Tests:

```python
def test_parse_maintenance_response_requires_report_and_one_action():
    report, decision = parse_maintenance_response(RESPONSE)
    assert report.startswith("# Maintenance report")
    assert decision.action == "RESTART_ROLE"
    assert decision.role == "DEV"


def test_report_name_is_fixed_and_colon_free():
    value = maintenance_report_relative("alpha", 2, datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc))
    assert value == ".plan/maintainers/alpha_turn2_20260723T010203Z.md"
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_maintenance.py -q
```

Expected: parser/report tests fail.

- [ ] **Step 3: Implement decision validation**

Phase-1 allowlist:

```python
{
    "WAIT",
    "RESUME_TASK",
    "RETRY_HOP",
    "RESTART_ROLE",
    "NEW_CHAT_ROLE",
    "OPEN_ROLE_TAB",
    "ROUTE_PLAN",
}
```

Validation rules:

```python
- exactly one terminal JSON object;
- exactly keys action, reason, role, lesson, replacement;
- action in allowlist;
- role required only for role actions and must be a configured normal logical role;
- replacement must be null in Phase 1;
- report body before JSON must be non-empty Markdown;
- lesson is optional, stripped, one paragraph, maximum 600 characters;
- no action may target MAINTAINERS.
```

- [ ] **Step 4: Materialize the maintenance report atomically**

Worker writes the inline Markdown to the exact generated path, fsyncs it, stores path/hash/size on the incident, then applies the decision. Agents never write this file directly.

- [ ] **Step 5: Map decisions to existing controls**

Use the same semantics as `_apply_control`:

```python
"RESUME_TASK"   -> queue/apply resume
"RETRY_HOP"     -> retry only when block_retryable is true
"RESTART_ROLE"  -> restart_role
"NEW_CHAT_ROLE" -> new_chat
"OPEN_ROLE_TAB" -> open_tab
"ROUTE_PLAN"    -> route_plan
"WAIT"          -> no mutation; incident remains OPEN with next-check evidence
```

Do not directly mutate hop/browser state in Maintainer code. Call a shared worker control function or queue a normal control record.

- [ ] **Step 6: Add state-change loop protection**

After an action, persist the pre-action snapshot key. Maintainers may receive another turn only when task `status`, `block_code`, `active_hop_id`, `updated_at`, or dependency evidence changes. If the action returns with the same snapshot and same error, mark the incident `ESCALATED`; do not resend the same prompt and do not call Maintainers for its own escalation.

- [ ] **Step 7: Append a new reusable lesson only after resolution**

The worker appends `decision.lesson` to `LEARNING.md` only when:

```python
incident["state"] == "RESOLVED"
and lesson.strip()
and lesson.strip() not in current_learning_text
```

Use the existing file lock primitive. Never overwrite the file and never append incident chronology.

- [ ] **Step 8: Run focused tests GREEN**

```bash
uv run pytest tests/test_cdpa_maintenance.py tests/test_cdpa_worker.py tests/test_cdpa_actions.py -q
```

Expected: all selected tests pass.

## Task 4: Phase-1 operational runtime acceptance

**Files:**
- Modify only tests/evidence required by failures found during verification.

- [ ] **Step 1: Run the complete suite**

```bash
uv run pytest -q
```

Expected: zero failures.

- [ ] **Step 2: Run a live synthetic incident**

Create a disposable CDPA task, deliberately close its active role tab before send acceptance, and verify:

```text
main task -> BLOCKED(role_offline)
Maintainers incident -> OPEN -> RUNNING
one MAINTAINERS tab exists
Maintainers chooses OPEN_ROLE_TAB or RESTART_ROLE
worker applies the action
main task resumes without duplicate original send
maintenance report exists under .plan/maintainers/
Maintainers does not edit source/test files
```

Record manifest/report/dashboard evidence under `.plan/unstopable/evidence/phase1/` without committing runtime artifacts.

- [ ] **Step 3: Independent TEST and REVIEW gate**

TEST must verify durable restart behavior and duplicate-tab fail-closed behavior. REVIEW must inspect that Maintainers cannot enter normal routes and that all mutations pass through worker validation.

- [ ] **Step 4: Commit Phase 1**

```bash
git add AGENTS.md LEARNING.md cdpa.yaml src/playwright_auto tests

git commit -m "feat: add global CDPA maintainers"
```

Do not include `.plan/**` or `CDPA_NEXT_SESSION_HANDOFF.md`.

---

# Phase 2 — Error-First Dashboard and Chronological Timeline

## Task 5: Build a canonical task timeline and primary-error projection

**Files:**
- Modify: `src/playwright_auto/dashboard.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_cdpa_cli_dashboard.py`

**Interfaces:**
- Consumes: task errors, hop errors/timestamps, route timeline, controls, maintenance incidents, dependency events, and lifecycle timestamps.
- Produces:

```python
def build_task_timeline(raw: Mapping[str, Any]) -> list[dict[str, Any]]: ...
def primary_task_problem(raw: Mapping[str, Any]) -> dict[str, Any] | None: ...
def effective_activity_at(raw: Mapping[str, Any]) -> str: ...
```

- [ ] **Step 1: Write failing ordering tests**

```python
def test_timeline_orders_all_sources_by_real_timestamp_descending():
    timeline = build_task_timeline(task_with_mixed_events())
    assert [item["at"] for item in timeline] == sorted(
        [item["at"] for item in timeline], reverse=True
    )
    assert timeline[0]["level"] == "ERROR"


def test_primary_problem_prefers_active_block_over_old_errors():
    problem = primary_task_problem(blocked_task_with_old_history())
    assert problem["code"] == "route_validation_exhausted"
    assert problem["role"] == "PLAN"
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_dashboard.py tests/test_cdpa_cli_dashboard.py -q
```

- [ ] **Step 3: Implement projections without synthetic timestamps**

Do not stamp active state with generic `task.updated_at` when a real event time exists. Normalize event rows to:

```python
{
    "key": "error:4",
    "at": "2026-07-23T...+00:00",
    "level": "ERROR|WARN|STATE|ROUTE|CONTROL|MAINTENANCE|DEPENDENCY|EVENT",
    "source": "PLAN|DEV|task|maintainers|dependency",
    "message": "...",
}
```

`effective_activity_at` chooses:

```text
BLOCKED  -> newest active error/incident timestamp
WAITING  -> newest dependency/team-queue timestamp
DONE     -> completed_at
STOPPED  -> stopped_at
otherwise -> last_role_activity_at or updated_at
```

- [ ] **Step 4: Expose projections in `build_task_payload`**

Add `timeline`, `primary_problem`, `effective_activity_at`, and `latest_maintenance_report`.

- [ ] **Step 5: Run tests GREEN**

```bash
uv run pytest tests/test_dashboard.py tests/test_cdpa_cli_dashboard.py -q
```

## Task 6: Reshape the selected-task UI

**Files:**
- Modify: `src/playwright_auto/dashboard.html`
- Modify: `tests/test_cdpa_cli_dashboard.py`

- [ ] **Step 1: Write failing browser assertions**

Assert the selected task renders in this order:

```text
1. task heading/status
2. primary error or waiting reason
3. active maintenance incident and report link
4. task controls
5. dependency/queue summary
6. role table
7. chronological timeline
8. reports
```

Assert task cards within every lane are sorted by `effective_activity_at DESC` and DONE/STOPPED are never mixed into the selected task’s operational error list.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_cli_dashboard.py -q
```

- [ ] **Step 3: Implement keyed in-place updates**

Preserve current polling, selection, and scroll behavior. Add no frontend framework and no WebSocket.

Primary error panel example:

```text
BLOCKED · route_validation_exhausted
Report file does not exist
PLAN · hop 4 · 02:09:04
Recommended: Open PLAN tab / Retry hop / Restart role
```

- [ ] **Step 4: Integrate Phase-2 failures with Maintainers**

Dashboard/API projection failures and malformed maintenance data must surface as explicit errors. They must not create recursive Maintainers incidents. Operational task blocks shown by the new panel must link to the active maintenance incident when one exists.

- [ ] **Step 5: Run focused and full tests**

```bash
uv run pytest tests/test_dashboard.py tests/test_cdpa_cli_dashboard.py -q
uv run pytest -q
```

- [ ] **Step 6: Desktop/mobile runtime smoke and commit**

Verify `1440x900`, `1280x800`, `768x1024`, and `390x844`. Commit:

```bash
git add src/playwright_auto/dashboard.py src/playwright_auto/dashboard.html tests

git commit -m "feat: surface CDPA errors and timeline"
```

---

# Phase 3 — Worker-Written Inline Role Reports

## Task 7: Parse inline role reports without weakening file mode

**Files:**
- Modify: `src/playwright_auto/cdpa_routes.py`
- Modify: `src/playwright_auto/cdpa_prompts.py`
- Modify: `src/playwright_auto/cdpa_worker.py`
- Modify: `src/playwright_auto/cdpa_store.py`
- Modify: `src/playwright_auto/cdpa_cli.py`
- Modify: `tests/test_cdpa_response.py`
- Modify: `tests/test_cdpa_worker.py`
- Modify: `tests/test_cdpa_core.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ParsedRoleResponse:
    decision: RouteDecision
    inline_report: str | None


def parse_role_response(
    text: str,
    *,
    source_role: str,
    report_mode: str,
) -> ParsedRoleResponse: ...
```

- [ ] **Step 1: Write failing file/inline compatibility tests**

```python
def test_inline_mode_extracts_markdown_before_terminal_json():
    parsed = parse_role_response(INLINE_RESPONSE, source_role="PLAN", report_mode="inline")
    assert parsed.inline_report == "# Final report\n\nEvidence."
    assert parsed.decision.route == "DONE"
    assert parsed.decision.handoff == "INLINE"


def test_file_mode_rejects_inline_handoff():
    with pytest.raises(RouteContractError, match="file report"):
        parse_role_response(INLINE_RESPONSE, source_role="PLAN", report_mode="file")
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_response.py tests/test_cdpa_worker.py -q
```

- [ ] **Step 3: Add `--inline-report` and persist `options.report_mode`**

CLI payload:

```python
"report_mode": "inline" if args.inline_report else "file"
```

Existing callers default to `file`.

- [ ] **Step 4: Update prompt guidance**

Inline guide requires:

````markdown
Write the complete Markdown role report in this response, followed by exactly one terminal JSON object:

```json
{"route":"PLAN|DEV|TEST|REVIEW|AUDIT|DONE","handoff":"INLINE"}
```
````

The worker-computed exact output path remains internal.

- [ ] **Step 5: Materialize inline reports before route validation**

For inline mode:

```python
- parse stable assistant output;
- require non-empty Markdown body;
- atomically write it to `hop["expected_report_path"]`;
- fsync and validate the exact path/hash/size with existing `validate_report`;
- store the real report path on the hop and report ledger;
- treat the parsed route as if its handoff were the materialized path.
```

Route repair in inline mode must request missing/invalid inline Markdown, not a local file write.

- [ ] **Step 6: Integrate failures with Maintainers**

An exhausted inline parse/materialization failure produces the normal operational block and therefore one Maintainers incident. Maintainers itself always uses inline reports independently of task `report_mode`.

- [ ] **Step 7: Run tests and commit**

```bash
uv run pytest tests/test_cdpa_response.py tests/test_cdpa_worker.py tests/test_cdpa_core.py -q
uv run pytest -q

git add src/playwright_auto tests AGENTS.md README.md

git commit -m "feat: add inline CDPA role reports"
```

---

# Phase 4 — Durable Dependency DAG and Maintainer Replacement

## Task 8: Add DAG validation and WAITING readiness

**Files:**
- Create: `src/playwright_auto/cdpa_dependencies.py`
- Create: `tests/test_cdpa_dependencies.py`
- Modify: `src/playwright_auto/cdpa_store.py`
- Modify: `src/playwright_auto/cdpa_cli.py`
- Modify: `src/playwright_auto/dashboard.py`
- Modify: `src/playwright_auto/cdpa_worker.py`

- [ ] **Step 1: Write failing DAG tests**

```python
def test_child_waits_until_all_parents_are_done():
    readiness = dependency_readiness(child(depends_on=["a", "b"]), [done("a"), running("b")])
    assert readiness.ready is False
    assert readiness.waiting_on == ("b",)


def test_stopped_parent_keeps_child_waiting():
    readiness = dependency_readiness(child(depends_on=["a"]), [stopped("a")])
    assert readiness.ready is False
    assert readiness.stopped == ("a",)


def test_cycle_is_rejected_before_write():
    with pytest.raises(ValueError, match="cycle"):
        validate_new_dependencies("a", ["c"], [task("b", ["a"]), task("c", ["b"])])
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_dependencies.py -q
```

- [ ] **Step 3: Implement pure graph helpers**

Use an iterative DFS or color-marked DFS from stdlib collections. Return canonical de-duplicated tuples in user-supplied order. Do not persist child lists.

- [ ] **Step 4: Add CLI and manifest fields**

CLI accepts both forms and de-duplicates:

```bash
cdpa "child" --depends-on task-a --depends-on task-b
cdpa "child" --depends-on task-a,task-b
```

New manifest fields:

```python
"depends_on_task_ids": ["task-a", "task-b"],
"replaces_task_id": None,
"dependency_events": [],
"waiting": {
    "reason": "dependency" | None,
    "waiting_on": [],
    "stopped": [],
    "missing": [],
    "since": None,
},
```

A not-ready task starts as:

```python
status="WAITING"
kanban_column="WAITING"
active_action="waiting_dependency"
```

It retains its unsent PLAN hop but the worker must not acquire/open a role tab until ready.

- [ ] **Step 5: Release dependencies before normal worker advancement**

At the beginning of each task advance:

```python
readiness = dependency_readiness(state, all_tasks)
if not readiness.ready:
    persist WAITING projection and return
if state.status == "WAITING" and readiness.ready:
    persist dependency_released event
    move to INBOX/queued
    continue normal hop processing
```

- [ ] **Step 6: Create Maintainers incidents for STOPPED/missing dependency evidence**

The child remains WAITING. The incident belongs to the affected parent task when it exists; otherwise it belongs to the child with trigger code `dependency_missing`. Dashboard must show the active Maintainers state.

- [ ] **Step 7: Run focused tests GREEN**

```bash
uv run pytest tests/test_cdpa_dependencies.py tests/test_cdpa_core.py tests/test_cdpa_worker.py -q
```

## Task 9: Authorize atomic replacement task creation and dependency rewiring

**Files:**
- Modify: `src/playwright_auto/cdpa_maintenance.py`
- Modify: `src/playwright_auto/cdpa_dependencies.py`
- Modify: `src/playwright_auto/cdpa_store.py`
- Modify: `tests/test_cdpa_maintenance.py`
- Modify: `tests/test_cdpa_dependencies.py`
- Modify: `tests/test_cdpa_worker.py`

- [ ] **Step 1: Write failing replacement tests**

Maintainers Phase-4 response:

```json
{
  "action": "REPLACE_TASK",
  "reason": "The stopped parent has no resumable accepted hop.",
  "role": null,
  "lesson": "Create a replacement only after preserving the stopped parent as immutable history.",
  "replacement": {
    "target_task_id": "parent-old",
    "task": "Continue the original parent outcome from its retained reports.",
    "reuse_team": true,
    "rewire_children": true
  }
}
```

Tests must prove:

```python
- old parent remains STOPPED and unchanged;
- replacement has a new task ID and `replaces_task_id == old_id`;
- replacement receives the old parent’s own dependencies;
- every canonical child dependency swaps old_id -> new_id exactly once;
- unrelated tasks are unchanged;
- all writes happen under the allocation lock;
- a validation failure writes nothing.
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_maintenance.py tests/test_cdpa_dependencies.py -q
```

- [ ] **Step 3: Implement one atomic graph mutation**

Add:

```python
TaskStore.replace_task_and_rewire(
    target_task_id: str,
    replacement_task: str,
    *,
    reuse_team: bool,
    rewire_children: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]
```

Inside one allocation lock:

```python
- load and validate all canonical manifests involved;
- create the replacement state in memory;
- validate resulting DAG;
- write replacement and each changed child with their file locks;
- update catalog entries;
- append explicit dependency_events with incident ID and old/new task IDs.
```

No silent mutation and no deletion of the original task.

- [ ] **Step 4: Apply through Maintainers only after worker validation**

Require an active incident whose target task matches `replacement.target_task_id`. Require the original task to be STOPPED/BLOCKED and not DONE. The worker, not the model, chooses/generated the new task ID.

- [ ] **Step 5: Run phase tests, live smoke, and commit**

```bash
uv run pytest tests/test_cdpa_dependencies.py tests/test_cdpa_maintenance.py tests/test_cdpa_worker.py tests/test_cdpa_cli_dashboard.py -q
uv run pytest -q

git add src/playwright_auto tests AGENTS.md README.md

git commit -m "feat: add CDPA task dependencies"
```

---

# Phase 5 — Multiple Tasks per Exact Team

## Task 10: Add explicit same-team queue creation

**Files:**
- Modify: `src/playwright_auto/cdpa_cli.py`
- Modify: `src/playwright_auto/cdpa_store.py`
- Modify: `src/playwright_auto/cdpa_team.py`
- Modify: `src/playwright_auto/cdpa_worker.py`
- Modify: `tests/test_cdpa_core.py`
- Modify: `tests/test_cdpa_worker.py`

- [ ] **Step 1: Write failing queue tests**

```python
def test_reuse_team_creates_second_task_without_allocating_suffix():
    first = store.create_task("one", requested_team="alpha")
    second = store.create_task("two", reuse_team="alpha")
    assert second["team"] == "alpha"
    assert second["team_suffix"] == first["team_suffix"]
    assert second["status"] == "WAITING"
    assert second["waiting"]["reason"] == "team_busy"


def test_only_oldest_ready_task_owns_exact_team():
    selected = select_team_runnable([running_task("a"), waiting_task("b")])
    assert selected.task_id == "a"
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_core.py tests/test_cdpa_worker.py -q
```

- [ ] **Step 3: Add CLI `--reuse-team <EXACT_TEAM>`**

Rules:

```text
--team with task text keeps current allocation semantics.
--reuse-team requires an exact existing team and is mutually exclusive with --team.
--reuse-team never allocates a numeric suffix.
--reuse-team is invalid for taskless resume.
```

- [ ] **Step 4: Persist queue state**

Add:

```python
"queue": {
    "reuse_team": True,
    "position": None,
    "blocked_by_task_id": None,
    "enqueued_at": "...",
    "released_at": None,
}
```

Runnable predicate:

```python
dependency_ready and no other task for exact team is INBOX/RUNNING/PAUSED/BLOCKED
```

Queue order is `created_at`, then `task_id` as deterministic tie-breaker. Do not add priority in this feature.

- [ ] **Step 5: Update exact-team resume semantics**

With multiple manifests for one exact team:

```text
- fail if more than one active owner exists;
- resume the one active owner when present;
- otherwise release/resume the oldest dependency-ready queued task;
- do not treat queued WAITING tasks as duplicate active manifests.
```

- [ ] **Step 6: Reuse role conversations safely**

When a terminal task finishes and a queued task becomes runnable:

```python
- preserve the same physical role names;
- use existing task preflight and atomic task/team rebind;
- never rebind across accepted in-flight sends;
- block on manual draft/attachments;
- do not auto-clean terminal tabs while a queued task exists;
- use New Chat only when task options explicitly request reset.
```

- [ ] **Step 7: Integrate queue failures with Maintainers**

`team_owner_conflict`, `queue_release_failed`, and safe rebind blocks create normal incidents. Maintainers may resume/restart or replace/rewire, but must not bypass duplicate active-owner validation.

- [ ] **Step 8: Run tests and commit**

```bash
uv run pytest tests/test_cdpa_core.py tests/test_cdpa_worker.py tests/test_cdpa_actions.py -q
uv run pytest -q

git add src/playwright_auto tests AGENTS.md README.md

git commit -m "feat: queue multiple tasks per CDPA team"
```

## Task 11: Show WAITING dependencies and queue positions

**Files:**
- Modify: `src/playwright_auto/dashboard.py`
- Modify: `src/playwright_auto/dashboard.html`
- Modify: `tests/test_cdpa_cli_dashboard.py`

- [ ] **Step 1: Write failing dashboard tests**

Assert:

```text
WAITING lane exists.
Dependency cards list each parent and status.
Stopped parent is shown as WAITING · dependency stopped, not BLOCKED.
Same-team queue card shows queued-behind task and position.
Selected task shows derived parents, children, and current queue position.
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_cdpa_cli_dashboard.py -q
```

- [ ] **Step 3: Add derived graph/queue payload and keyed UI**

Do not persist children or queue position. Derive both from the current canonical task set for every `/api/tasks` response.

- [ ] **Step 4: Run responsive smoke and full suite**

```bash
uv run pytest tests/test_cdpa_cli_dashboard.py tests/test_dashboard.py -q
uv run pytest -q
```

Commit UI with the Phase-5 commit or a separate accepted UI commit if REVIEW requires isolation.

---

# Phase 6 — Durable File Upload Context

## Task 12: Add task-level attachments and durable per-role upload

**Files:**
- Modify: `src/playwright_auto/cdpa_cli.py`
- Modify: `src/playwright_auto/cdpa_store.py`
- Modify: `src/playwright_auto/cdpa_worker.py`
- Modify: `src/playwright_auto/durable_blocks.py` only if an existing interface gap is proven by a failing test
- Modify: `tests/test_upload.py`
- Modify: `tests/test_cdpa_worker.py`
- Modify: `tests/test_cdpa_core.py`

- [ ] **Step 1: Write failing CLI/store tests**

```python
def test_upload_paths_are_hashed_when_task_is_created(tmp_path):
    context = tmp_path / "context.md"
    context.write_text("evidence", encoding="utf-8")
    state = store.create_task("analyze", upload_paths=[context])
    assert state["attachments"][0]["name"] == "context.md"
    assert state["attachments"][0]["sha256"]


def test_changed_upload_file_blocks_before_send(tmp_path):
    state = task_with_attachment(tmp_path)
    Path(state["attachments"][0]["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="identity changed"):
        worker.validate_task_attachments(state)
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_upload.py tests/test_cdpa_worker.py tests/test_cdpa_core.py -q
```

- [ ] **Step 3: Add repeatable `--upload`**

Examples:

```bash
cdpa "Analyze these sources" --upload architecture.md --upload logs.txt
```

At creation, call existing `collect_file_identities` and persist absolute path, name, size, SHA-256, and MIME type. Preserve the existing 20 MiB default total unless config already exposes a lower bound.

- [ ] **Step 4: Upload once per role conversation generation**

Add role record field:

```python
"attachments_uploaded_generation": None
```

For the first send in each role conversation generation:

```python
files = task attachment paths
DurableSendBlock(..., files=files)
```

After durable upload readiness and send acceptance, record the generation. Do not upload again on later turns in the same role conversation. Upload again after explicit New Chat/restart because that generation lacks the files.

- [ ] **Step 5: Fail closed on identity or composer conflict**

A changed/missing file, partial attachment set, manual attachment, or ambiguous upload recovery must block before send and create a Maintainers incident. Maintainers may restart/reopen/wait, but it cannot fabricate or modify the source file.

- [ ] **Step 6: Expose attachment metadata in dashboard**

Show file name, size, hash prefix, and per-role uploaded generation. Never expose raw file contents through the dashboard API.

- [ ] **Step 7: Run tests and commit**

```bash
uv run pytest tests/test_upload.py tests/test_durable.py tests/test_cdpa_worker.py tests/test_cdpa_cli_dashboard.py -q
uv run pytest -q

git add src/playwright_auto tests AGENTS.md README.md

git commit -m "feat: upload durable CDPA task context"
```

---

# Phase 7 — Contract Documentation, Packaging, and End-to-End Acceptance

## Task 13: Update repository contracts and packaged defaults

**Files:**
- Modify: `AGENTS.md`
- Modify: `LEARNING.md` only with evidence-backed lessons from accepted phases
- Modify: `README.md`
- Modify: `src/playwright_auto/cdpa_defaults/cdpa.json`
- Modify: packaged prompt files under `src/playwright_auto/cdpa_defaults/prompts/cdpa/`
- Modify: `tests/test_cdpa_cli_dashboard.py`
- Modify: `tests/test_cdpa_core.py`

- [ ] **Step 1: Add exact CLI examples**

```bash
cdpa "Build parent" --team alpha
cdpa "Build child" --team beta --depends-on <parent-task-id>
cdpa "Continue with same context" --reuse-team alpha --depends-on <other-task-id>
cdpa "Analyze uploaded sources" --team analysis --inline-report --upload design.md
```

- [ ] **Step 2: Document exact invariants**

`AGENTS.md` must state:

```text
- MAINTAINERS is global to 9222 and never a normal route.
- Main teams retain PLAN as the only DONE authority.
- Maintainers returns one inline report plus one allowlisted recovery action.
- Maintainers does not edit code/tests/deliverables; worker applies operations.
- Parent STOPPED keeps child WAITING.
- Maintainers may atomically replace a failed task and rewire dependencies.
- One active task per exact team; queued tasks reuse conversations safely.
- Inline report and upload semantics.
```

- [ ] **Step 3: Verify wheel contents**

```bash
uv build
python - <<'PY'
from pathlib import Path
from zipfile import ZipFile
wheel = sorted(Path('dist').glob('*.whl'))[-1]
with ZipFile(wheel) as archive:
    names = set(archive.namelist())
assert any(name.endswith('/cdpa_defaults/prompts/cdpa/MAINTAINERS.md') for name in names)
assert any(name.endswith('/dashboard.html') for name in names)
print(wheel)
PY
```

Expected: assertions pass.

## Task 14: Final integrated pipeline proof

- [ ] **Step 1: Full tests**

```bash
uv run pytest -q
```

Expected: zero failures.

- [ ] **Step 2: Compile/build**

```bash
uv run python -m compileall -q src tests
uv build
```

Expected: exit code 0.

- [ ] **Step 3: Live dependency and same-team queue scenario**

Run this pipeline:

```text
Task A / team alpha / RUNNING
Task B / team beta / depends_on A / WAITING
Task C / reuse team alpha / depends_on B / WAITING team+dependency
```

Verify:

```text
A DONE -> B releases and runs.
Alpha remains reserved for C but C still waits for B.
B DONE -> C releases and safely rebinds alpha role tabs.
No duplicate physical-role ownership.
Dashboard parents/children/queue/timeline are correct.
```

- [ ] **Step 4: Live STOPPED-parent replacement scenario**

Stop a disposable parent before completion. Verify:

```text
child remains WAITING;
one global Maintainers incident opens;
Maintainers chooses REPLACE_TASK only after smaller resume/restart is unsafe;
worker creates replacement and rewires child atomically;
old parent remains immutable STOPPED history;
replacement finishes;
child releases;
maintenance report and optional LEARNING lesson are retained.
```

- [ ] **Step 5: Live inline/upload scenario**

Create an `--inline-report --upload` task. Verify every used role receives attachments once per generation, reports are worker-written, restart recovery does not duplicate upload/send, and the task reaches PLAN DONE.

- [ ] **Step 6: Independent REVIEW and AUDIT**

REVIEW checks correctness and data-loss boundaries. AUDIT checks:

```text
- no second orchestration engine;
- no mirrored dependency child state;
- no Maintainers normal route;
- no source/test writes from Maintainers protocol;
- no browser close of persistent Chromium;
- no unbounded identical recovery loop;
- no cleanup while queued team work remains;
- no raw attachment content in dashboard payload.
```

- [ ] **Step 7: Final PLAN closeout**

PLAN verifies all phase commits, exact tests/evidence, updated docs, package contents, and branch status. PLAN routes DONE only after all acceptance scenarios pass. Do not push or merge.

---

## Plan Self-Review

- **Coverage:** Includes global Maintainers first, error-first dashboard, inline reports, dependency DAG, STOPPED parent behavior, autonomous replacement/rewire, same-team queue, upload, docs, packaging, and end-to-end evidence.
- **No mirrored graph state:** Only child `depends_on_task_ids` is persisted; children and positions are derived.
- **No Maintainers recursion:** Maintainer failures use dedicated global error state and never generate incidents.
- **No second runtime:** Maintainer coordinator executes inside the existing `CDPAWorker` process and uses existing browser/durable primitives.
- **Operational boundary:** Maintainers supplies structured decisions/reports/lessons; worker performs all filesystem, manifest, graph, and control mutations.
- **Compatibility:** Existing manifests remain schema version 1 and valid when optional feature fields are absent.
- **Phase order:** Every later feature explicitly integrates its failures with the Phase-1 Maintainers sidecar before acceptance.


## Maintainers capability upgrade addendum (2026-07-25)

- Maintainers is the default recovery authority for every nonterminal non-operator BLOCKED or STOPPED incident. Operator-origin controls remain authoritative and are persisted separately from worker/Maintainers provenance.
- The worker executes immutable commands against exact task/team/hop/request/conversation/receipt snapshots. An action is applied only when its postcondition is verified; an ineffective primitive remains explicit and may advance to the next of at most three bounded recovery steps.
- A dedicated repair proposal is allowed only for CDPA/Maintainers defects or evidence-backed systemic defects that can disrupt teams. The worker creates or deduplicates the repository-bounded urgent repair task and carries exact evidence, bounded source areas, required tests, and a LEARNING.md proposal. One canonical validator is used at v2 response parsing, `RepairRequest.create`, and `RepairRequest.from_dict`: root cause/reason ≤1200 characters, reproduction ≤2400, 1–8 allowlisted source areas, 1–16 one-line tests ≤300 characters, and optional one-paragraph lesson ≤600; oversized and corrupted durable values fail closed before mutation. Every declared textual repair field must already be a JSON/string value; optional identity and lesson fields are null or strings. Source areas and required tests must be concrete arrays at the model boundary and list/tuple collections in worker code. The worker checks raw entry count before trimming or other normalization, rejects non-string items, rejects exact duplicates, and rejects values that become duplicates after trimming; it never applies `str()` coercion or silent duplicate collapse. Reusing an active repair still atomically attaches the current affected task; repair metadata retains one idempotent operation per affected task, incident, hop/request snapshot, and disposition. A later durable decision may supersede the disposition without creating another repair task.
- `CONTINUE_IN_PARALLEL` leaves the safe affected task independent. `HOLD_FOR_REPAIR` uses one crash-recoverable TaskStore transaction to create/deduplicate the repair, validate the DAG, append the repair ID to the same affected task's existing `depends_on_task_ids`, and move that task to WAITING without changing its task/team/hop/request/report/receipt identity.
- Repair DONE releases the same preserved hop through the existing dependency scheduler. Accepted waiting work resumes observation with zero resend; pre-send work continues the same request. Dashboard payloads, the selected-task Repair Relationships panel, and timeline events project both sides of every repair relationship, disposition, priority, gate, and release.
- Reusable lessons are locked and normalized-deduplicated. A repair lesson is appended only after repair DONE and, for `HOLD_FOR_REPAIR`, after the preserved affected task has been released.
- Credential safety is a worker boundary, not a prompt convention: URL userinfo/fragments and sensitive query/auth values are removed or redacted before incident/global-state/report persistence; prompt/dashboard/timeline projections use explicit allowlists and exclude stored full prompts plus raw evidence arrays. URL path credentials are also secret material: JWT-like segments and directly credential-bearing segments are redacted, while exact or tokenized compound high-risk route markers such as webhooks, OAuth, reset, capability, signed-url, magic-link, or token start a fail-closed context. Before matching, percent-decoded camelCase/acronym boundaries are canonicalized. Separator-free labels are classified by a bounded exact credential-operation grammar: explicit qualifier+noun pairs cover access/refresh/id/api/bearer/auth/session/CSRF tokens, client secrets or credentials, API keys, session IDs, and verification/activation/invite/reset codes; explicit operation+suffix rules cover password-reset links and OAuth, authorization, magic-link, signed-URL, and webhook callback, redirect, or incoming routes. The grammar materializes exact compact identities only; generic prefix, suffix, and substring matching are forbidden. The marker segment itself and every remaining non-empty path segment are redacted, including bare markers and marker-plus-payload forms; static intermediary labels, version segments, callbacks, status names, and completion routes cannot end that context. Any URL containing the context is unprobeable unless the worker has an explicit secret-free descriptor/auth profile. Normal resource identifiers and near-match names outside exact high-risk contexts remain intact. The same worker-owned boundary applies before exception-derived operational text enters task/hop/role errors, block or waiting reasons, refresh state, cleanup state, route-repair evidence, or control results; dashboard task, hop, role, cleanup, route, error, and control projections sanitize those fields again as defense in depth. A credential-bearing network URL without a secret-free endpoint and explicit auth profile is not probed and remains suspended.
- Environmental failures receive at most three durable attempts. The incident then becomes SUSPENDED with exact prerequisite evidence. Browser/CDP reopening requires `browser.is_connected()` plus a bounded live `context.cookies()` round trip; network reopening requires a bounded no-redirect `HEAD` to the exact failed endpoint when present, otherwise the current ChatGPT origin for ChatGPT transport failures; filesystem reopening requires a real temporary write/fsync/delete under `.plan`. MCP/tooling is a separate prerequisite. The production Maintainers path may configure one worker-owned preflight dependency. Before browser acquisition or Send, the worker builds a typed immutable descriptor containing only dependency, canonical loopback endpoint, auth-profile reference, method, and required tool names. It resolves credentials only from worker runtime environment, disables redirects, performs the authenticated MCP Streamable HTTP lifecycle (`initialize`; when a session ID is returned, session-bound `notifications/initialized` with an empty successful response; `tools/list`; required successful session DELETE for that session), and reopens the same incident only when every required capability is present. Stateless servers use `initialize` followed directly by `tools/list`. Durable evidence contains sanitized statuses, protocol/session mode, required/matched/missing tools, catalog/session hashes, and cleanup result—never bearer material or raw session ID. Missing credentials, malformed/non-loopback/unallowlisted descriptors, protocol errors, or missing required tools remain suspended. Browser page-set stability and unrelated HTTP success are diagnostic context only.


Repair creation is accepted only through the version-2 top-level `repair` object; legacy `CREATE_REPAIR_TASK` actions and recovery-list repair actions are rejected before report/control/task mutation.
