from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from playwright_auto.cdpa_maintenance import (
    MAINTAINER_ROLE,
    MaintenanceDecision,
    MaintenanceStateStore,
    append_resolved_lesson,
    ensure_maintenance_incident,
    maintenance_incident_key,
    maintenance_report_relative,
    parse_maintenance_response,
    write_maintenance_report,
)


def task_state(
    *,
    status: str = "BLOCKED",
    block_code: str | None = "role_offline",
    updated_at: str = "2026-07-23T01:02:03+00:00",
    terminal_state: str | None = None,
) -> dict[str, object]:
    return {
        "task_id": "task-a",
        "team": "alpha",
        "status": status,
        "terminal_state": terminal_state,
        "block_code": block_code,
        "block_reason": "owned alpha-dev tab is offline",
        "stop_reason": "operator stopped task",
        "active_hop_id": 3,
        "active_role": "DEV",
        "updated_at": updated_at,
    }


def test_blocked_task_gets_one_open_incident_for_same_snapshot():
    state = task_state()
    first = ensure_maintenance_incident(state)
    second = ensure_maintenance_incident(state)
    assert first is second
    assert first is not None
    assert len(state["maintenance"]["incidents"]) == 1
    assert first["state"] == "OPEN"
    assert state["maintenance"]["active_incident_id"] == first["incident_id"]


def test_same_failure_after_recovery_creates_new_incident_after_escalation():
    state = task_state()
    first = ensure_maintenance_incident(state)
    assert first is not None
    first["state"] = "ESCALATED"
    state["maintenance"]["active_incident_id"] = None

    state["status"] = "RUNNING"
    state["block_code"] = None
    state["block_reason"] = None
    state["updated_at"] = "2026-07-23T01:03:00+00:00"
    assert ensure_maintenance_incident(state) is None

    state["status"] = "BLOCKED"
    state["block_code"] = "role_offline"
    state["block_reason"] = "owned alpha-dev tab is offline"
    state["updated_at"] = "2026-07-23T01:04:00+00:00"
    second = ensure_maintenance_incident(state)

    assert second is not None
    assert second is not first
    assert second["state"] == "OPEN"
    assert len(state["maintenance"]["incidents"]) == 2
    assert state["maintenance"]["active_incident_id"] == second["incident_id"]


def test_different_blocked_episode_retires_stale_suppression_before_a_recurs():
    state = task_state()
    first = ensure_maintenance_incident(state)
    assert first is not None
    first["state"] = "ESCALATED"
    state["maintenance"]["active_incident_id"] = None
    state["maintenance"]["suppressed_operational_key"] = first["operational_key"]

    state["block_code"] = "send_failed"
    state["block_reason"] = "send transport failed"
    state["updated_at"] = "2026-07-23T01:03:00+00:00"
    second = ensure_maintenance_incident(state)
    assert second is not None
    assert second is not first
    assert "suppressed_operational_key" not in state["maintenance"]

    second["state"] = "RESOLVED"
    state["maintenance"]["active_incident_id"] = None
    state["block_code"] = "role_offline"
    state["block_reason"] = "owned alpha-dev tab is offline"
    state["updated_at"] = "2026-07-23T01:04:00+00:00"
    third = ensure_maintenance_incident(state)

    assert third is not None
    assert third is not first
    assert third["state"] == "OPEN"
    assert len(state["maintenance"]["incidents"]) == 3
    assert state["maintenance"]["active_incident_id"] == third["incident_id"]


def test_changed_snapshot_creates_a_new_incident_after_resolution():
    state = task_state()
    first = ensure_maintenance_incident(state)
    assert first is not None
    first["state"] = "RESOLVED"
    state["maintenance"]["active_incident_id"] = None
    state["updated_at"] = "2026-07-23T01:03:04+00:00"
    second = ensure_maintenance_incident(state)
    assert second is not None
    assert second is not first
    assert len(state["maintenance"]["incidents"]) == 2


def test_maintainer_failure_never_creates_an_incident_for_itself():
    state = task_state(block_code="maintainer_failed")
    assert maintenance_incident_key(state) is None
    assert ensure_maintenance_incident(state) is None
    assert "maintenance" not in state


def test_stopped_task_creates_incident_but_done_does_not():
    stopped = task_state(status="STOPPED", block_code=None, terminal_state="STOPPED")
    done = task_state(status="DONE", block_code=None, terminal_state="DONE")
    assert ensure_maintenance_incident(stopped)["trigger_status"] == "STOPPED"
    assert ensure_maintenance_incident(done) is None


def test_cleanup_in_progress_never_resumes_old_tabs():
    state = task_state(status="STOPPED", block_code=None, terminal_state="STOPPED")
    state["cleanup"] = {"state": "CLEARING"}

    assert maintenance_incident_key(state) is None
    assert ensure_maintenance_incident(state) is None


VALID_RESPONSE = """# Maintenance report

The owned DEV tab is offline before send acceptance. Reopening the exact
conversation is the smallest safe recovery.

```json
{"action":"OPEN_ROLE_TAB","reason":"Restore the exact owned role tab.","role":"DEV","lesson":"Prefer exact role reopen before discarding a durable hop.","replacement":null}
```
"""

WAIT_RESPONSE = """# Maintenance report

No safe mutation is justified until new task evidence appears.

```json
{"action":"WAIT","reason":"Wait for changed operational evidence.","role":null,"lesson":null,"replacement":null}
```
"""


def test_parse_maintenance_response_requires_report_and_one_action():
    report, decision = parse_maintenance_response(
        VALID_RESPONSE,
        configured_roles=("PLAN", "DEV", "REVIEW", "TEST", "AUDIT"),
    )
    assert report.startswith("# Maintenance report")
    assert decision == MaintenanceDecision(
        action="OPEN_ROLE_TAB",
        reason="Restore the exact owned role tab.",
        role="DEV",
        lesson="Prefer exact role reopen before discarding a durable hop.",
        replacement=None,
    )


def test_parse_maintenance_response_accepts_live_rendered_json_label():
    response = (
        "Maintenance report: the PLAN tab is offline. "
        'JSON {"action":"OPEN_ROLE_TAB","reason":"Restore it.",'
        '"role":"PLAN","lesson":null,"replacement":null}'
    )

    report, decision = parse_maintenance_response(response)

    assert report == "Maintenance report: the PLAN tab is offline.\n"
    assert decision.action == "OPEN_ROLE_TAB"
    assert decision.role == "PLAN"


def test_parse_maintenance_response_accepts_lowercase_or_missing_json_label():
    decision = (
        '{"action":"WAIT","reason":"Observe once more.",'
        '"role":null,"lesson":null,"replacement":null}'
    )

    lower_report, lower = parse_maintenance_response(f"# Report\n\njson {decision}")
    bare_report, bare = parse_maintenance_response(f"# Report\n\n{decision}")

    assert lower_report == bare_report == "# Report\n"
    assert lower == bare


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            '# Report\n\nJSON {"action":"WAIT","reason":"x","role":null,'
            '"lesson":null,"replacement":null} trailing prose',
            "end with one JSON decision",
        ),
        (
            '# Report\n\nJSON {"action":"WAIT","reason":"first","role":null,'
            '"lesson":null,"replacement":null}\nJSON '
            '{"action":"WAIT","reason":"second","role":null,'
            '"lesson":null,"replacement":null}',
            "exactly one terminal JSON decision",
        ),
        (
            '# Report\n\nJSON {"action":"WAIT","action":"RESUME_TASK",'
            '"reason":"x","role":null,"lesson":null,"replacement":null}',
            "duplicate maintenance field",
        ),
    ],
)
def test_parse_maintenance_response_rejects_ambiguous_rendered_json(
    response: str, message: str
):
    with pytest.raises(ValueError, match=message):
        parse_maintenance_response(response)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            '{"action":"WAIT","reason":"x","role":null,"lesson":null,"replacement":null}',
            "non-empty Markdown report",
        ),
        (
            "# Report\n\n```json\n"
            '{"action":"RESTART_ROLE","reason":"x","role":"MAINTAINERS","lesson":null,"replacement":null}'
            "\n```",
            "normal configured role",
        ),
        (
            "# Report\n\n```json\n"
            '{"action":"WAIT","reason":"x","role":null,"lesson":null,"replacement":{"task":"x"}}'
            "\n```",
            "replacement must be null",
        ),
        (
            "# Report\n\n```json\n"
            '{"action":"WAIT","reason":"x","role":null,"lesson":"one\\ntwo","replacement":null}'
            "\n```",
            "one paragraph",
        ),
    ],
)
def test_parse_maintenance_response_rejects_invalid_contract(response: str, message: str):
    with pytest.raises(ValueError, match=message):
        parse_maintenance_response(response, configured_roles=("PLAN", "DEV"))


def test_report_name_is_fixed_and_colon_free():
    value = maintenance_report_relative(
        "alpha",
        2,
        datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
    )
    assert value == ".plan/maintainers/alpha_turn2_20260723T010203Z.md"
    assert ":" not in value


def test_report_is_written_atomically_with_evidence(tmp_path: Path):
    evidence = write_maintenance_report(
        tmp_path,
        team="alpha",
        turn=2,
        report="# Maintenance report\n\nEvidence.\n",
        at=datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
    )
    path = Path(evidence.path)
    assert path == tmp_path / ".plan" / "maintainers" / "alpha_turn2_20260723T010203Z.md"
    assert path.read_text(encoding="utf-8") == "# Maintenance report\n\nEvidence.\n"
    assert evidence.size > 0
    assert len(evidence.sha256) == 64


def test_global_state_store_round_trips_and_is_not_a_task_manifest(tmp_path: Path):
    store = MaintenanceStateStore(tmp_path / ".plan")
    initial = store.load()
    initial["page_id"] = "maint-page"
    initial["turn"] = 4
    saved = store.save(initial)
    assert saved["physical_role"] == MAINTAINER_ROLE
    assert saved["page_id"] == "maint-page"
    assert saved["history"] == []
    assert store.load()["turn"] == 4
    assert "schema_version" not in saved


def test_resolved_lesson_is_appended_once_under_existing_document(tmp_path: Path):
    learning = tmp_path / "LEARNING.md"
    learning.write_text("# LEARNING.md\n\nExisting lesson.\n", encoding="utf-8")
    incident = {"state": "RESOLVED"}
    decision = MaintenanceDecision(
        action="WAIT",
        reason="resolved",
        lesson="Prefer exact role reopen before discarding a durable hop.",
    )
    assert append_resolved_lesson(learning, incident, decision) is True
    assert append_resolved_lesson(learning, incident, decision) is False
    text = learning.read_text(encoding="utf-8")
    assert text.count(decision.lesson) == 1


def test_coordinator_writes_report_and_queues_one_recovery_control(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Recover a role",
        requested_team="alpha",
        task_id="task-maint-coordinator",
    )
    path = Path(state["manifest_path"])

    def block(current):
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "owned alpha-dev tab is offline"
        current["active_role"] = "PLAN"
        return current

    state = store.update(path, block)
    sends: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            assert role == MAINTAINER_ROLE
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=True,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **kwargs):
            sends.append(prompt)
            assert kwargs["request_id"].startswith("maint-")
            assert kwargs["minimum_samples"] == 2
            assert kwargs["invalid_grace_ms"] == 1_000
            validator = kwargs["candidate_validator"]
            validator(SimpleNamespace(text=VALID_RESPONSE))
            with pytest.raises(ValueError, match="end with one JSON decision"):
                validator(SimpleNamespace(text=VALID_RESPONSE.rstrip() + " ``"))

        async def run(self, _context):
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)

    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    changed = asyncio.run(coordinator.advance([(path, state)], SimpleNamespace()))
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]

    assert changed is True
    assert len(sends) == 1
    assert incident["state"] == "RUNNING"
    assert incident["decision"]["action"] == "OPEN_ROLE_TAB"
    assert Path(incident["report_path"]).is_file()
    assert incident["report_sha256"]
    assert current["controls"][-1]["action"] == "open_tab"
    assert current["controls"][-1]["role"] == "DEV"
    global_state = coordinator.state_store.load()
    assert global_state["active_incident"]["incident_id"] == incident["incident_id"]
    assert global_state["history"][-1]["report_path"] == incident["report_path"]

    unchanged = asyncio.run(
        coordinator.advance([(path, current)], SimpleNamespace())
    )
    assert unchanged is False
    assert len(sends) == 1


def test_coordinator_processes_oldest_of_multiple_open_incidents(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    older = store.create_task(
        "Older incident", requested_team="alpha", task_id="task-older"
    )
    newer = store.create_task(
        "Newer incident", requested_team="beta", task_id="task-newer"
    )
    paths = [Path(older["manifest_path"]), Path(newer["manifest_path"])]
    states = []
    for index, path in enumerate(paths):
        state = store.load(path)
        state.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="offline",
        )
        state = store.save(path, state)
        incident = ensure_maintenance_incident(state)
        assert incident is not None
        incident["created_at"] = f"2026-07-23T00:00:0{index}+00:00"
        states.append(store.save_maintenance(path, state))

    sends: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=True,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            sends.append(prompt)

        async def run(self, _context):
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(
        coordinator.advance(
            list(zip(reversed(paths), reversed(states), strict=True)),
            SimpleNamespace(),
        )
    ) is True

    older_state = store.load(paths[0])
    newer_state = store.load(paths[1])
    older_incident = older_state["maintenance"]["incidents"][0]
    newer_incident = newer_state["maintenance"]["incidents"][0]
    assert len(sends) == 1
    assert "task-older" in sends[0]
    assert older_incident["decision"]["action"] == "OPEN_ROLE_TAB"
    assert newer_incident["state"] == "OPEN"
    assert newer_incident["decision"] is None


def test_coordinator_persists_a_b_a_as_three_operational_episodes(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Recurring operational episode",
        requested_team="alpha",
        task_id="task-maint-a-b-a",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="episode A",
    )
    state = store.save(path, state)
    first = ensure_maintenance_incident(state)
    assert first is not None
    first["state"] = "ESCALATED"
    state["maintenance"]["active_incident_id"] = None
    state["maintenance"]["suppressed_operational_key"] = first["operational_key"]
    state = store.save_maintenance(path, state)
    prompts: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            prompts.append(prompt)

        async def run(self, _context):
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    def block_b(current):
        current.update(
            block_code="send_failed",
            block_reason="episode B",
        )
        return current

    blocked_b = store.update(path, block_b)
    assert asyncio.run(
        coordinator.advance([(path, blocked_b)], SimpleNamespace())
    ) is True
    after_b = store.load(path)
    assert "suppressed_operational_key" not in after_b["maintenance"]
    assert len(after_b["maintenance"]["incidents"]) == 2
    second = after_b["maintenance"]["incidents"][1]
    assert second["trigger_code"] == "send_failed"

    second["state"] = "RESOLVED"
    after_b["maintenance"]["active_incident_id"] = None
    after_b = store.save_maintenance(path, after_b)
    global_state = coordinator.state_store.load()
    global_state["active_incident"] = None
    coordinator.state_store.save(global_state)

    def block_a_again(current):
        current.update(
            block_code="role_offline",
            block_reason="episode A",
        )
        return current

    blocked_a_again = store.update(path, block_a_again)
    assert asyncio.run(
        coordinator.advance([(path, blocked_a_again)], SimpleNamespace())
    ) is True

    final = store.load(path)
    assert len(prompts) == 2
    assert len(final["maintenance"]["incidents"]) == 3
    third = final["maintenance"]["incidents"][2]
    assert third["incident_id"] != first["incident_id"]
    assert third["state"] == "RUNNING"
    assert third["trigger_code"] == "role_offline"
    assert final["maintenance"]["active_incident_id"] == third["incident_id"]


def test_same_page_new_conversation_includes_constructor_and_learning(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text(
        "# LEARNING.md\n\nKeep exact durable provenance.\n", encoding="utf-8"
    )
    store = TaskStore(config)
    state = store.create_task(
        "Recover in reset conversation",
        requested_team="alpha",
        task_id="task-maint-new-conversation",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    state = store.save(path, state)
    prompts: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/new-conversation",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            prompts.append(prompt)

        async def run(self, _context):
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state.update(
        page_id="maint-page",
        page_url="https://chatgpt.com/c/old-conversation",
        conversation_generation=0,
        constructor_sent_generation=0,
    )
    coordinator.state_store.save(global_state)

    assert asyncio.run(
        coordinator.advance([(path, state)], SimpleNamespace())
    ) is True

    assert len(prompts) == 1
    constructor = config.maintainers_constructor_path.read_text(encoding="utf-8").strip()
    assert prompts[0].count(constructor) == 1
    assert prompts[0].count("CURRENT LEARNING.md") == 1
    assert "Keep exact durable provenance." in prompts[0]
    saved = coordinator.state_store.load()
    assert saved["conversation_generation"] == 1
    assert saved["constructor_sent_generation"] == 1


def test_root_to_conversation_url_does_not_advance_generation(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Continue first conversation",
        requested_team="alpha",
        task_id="task-maint-root-transition",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    state = store.save(path, state)

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/first-conversation",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            assert "CURRENT LEARNING.md" not in prompt

        async def run(self, _context):
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state.update(
        page_id="maint-page",
        page_url="https://chatgpt.com/",
        conversation_generation=0,
        constructor_sent_generation=0,
    )
    coordinator.state_store.save(global_state)

    assert asyncio.run(
        coordinator.advance([(path, state)], SimpleNamespace())
    ) is True
    assert coordinator.state_store.load()["conversation_generation"] == 0


def test_generation_reset_rebuilds_proven_presend_prompt_with_new_request(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.durable import RequestLedger, RequestStatus

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text(
        "# LEARNING.md\n\nUse exact request provenance.\n", encoding="utf-8"
    )
    store = TaskStore(config)
    state = store.create_task(
        "Reset before send",
        requested_team="alpha",
        task_id="task-maint-presend-reset",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    state = store.save(path, state)
    urls = iter(("https://chatgpt.com/c/old", "https://chatgpt.com/c/new"))
    attempts: list[tuple[str, dict[str, object]]] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url=next(urls),
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **kwargs):
            attempts.append((prompt, kwargs))

        async def run(self, _context):
            prompt, kwargs = attempts[-1]
            if len(attempts) == 1:
                ledger = RequestLedger(kwargs["ledger_path"])
                record = ledger.begin(
                    role=MAINTAINER_ROLE,
                    prompt=prompt,
                    source_context=kwargs["source_context"],
                    role_prompt_hash=kwargs["role_prompt_hash"],
                    request_id=kwargs["request_id"],
                    render_request_marker=False,
                )
                ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
                raise RuntimeError("transient failure before send")
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state.update(
        page_id="maint-page",
        page_url="https://chatgpt.com/c/old",
        conversation_generation=0,
        constructor_sent_generation=0,
    )
    coordinator.state_store.save(global_state)

    assert asyncio.run(
        coordinator.advance([(path, state)], SimpleNamespace())
    ) is True
    first_state = store.load(path)
    first_incident = first_state["maintenance"]["incidents"][0]
    first_request_id = attempts[0][1]["request_id"]
    assert first_incident["prompt_generation"] == 0
    assert first_incident["constructor_included"] is False

    assert asyncio.run(
        coordinator.advance([(path, first_state)], SimpleNamespace())
    ) is True

    assert len(attempts) == 2
    first_prompt, first_kwargs = attempts[0]
    second_prompt, second_kwargs = attempts[1]
    constructor = config.maintainers_constructor_path.read_text(encoding="utf-8").strip()
    assert first_prompt != second_prompt
    assert constructor not in first_prompt
    assert second_prompt.count(constructor) == 1
    assert second_prompt.count("CURRENT LEARNING.md") == 1
    assert "Use exact request provenance." in second_prompt
    assert second_kwargs["request_id"] != first_request_id
    assert str(second_kwargs["request_id"]).endswith("-g1")
    old_record = RequestLedger(first_kwargs["ledger_path"]).get(str(first_request_id))
    assert old_record is not None
    assert old_record.status is RequestStatus.PROMPT_SET
    assert old_record.attempts == 0
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert incident["prompt_generation"] == 1
    assert incident["constructor_included"] is True
    saved_global = coordinator.state_store.load()
    assert saved_global["conversation_generation"] == 1
    assert saved_global["constructor_sent_generation"] == 1


def test_generation_reset_with_sending_request_escalates_without_resend(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.durable import RequestLedger, RequestStatus

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Reset after send boundary",
        requested_team="alpha",
        task_id="task-maint-sending-reset",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    state = store.save(path, state)
    urls = iter(("https://chatgpt.com/c/old", "https://chatgpt.com/c/new"))
    attempts: list[tuple[str, dict[str, object]]] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url=next(urls),
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **kwargs):
            if attempts:
                raise AssertionError("ambiguous old-generation request must not resend")
            attempts.append((prompt, kwargs))

        async def run(self, _context):
            prompt, kwargs = attempts[0]
            ledger = RequestLedger(kwargs["ledger_path"])
            record = ledger.begin(
                role=MAINTAINER_ROLE,
                prompt=prompt,
                source_context=kwargs["source_context"],
                role_prompt_hash=kwargs["role_prompt_hash"],
                request_id=kwargs["request_id"],
                render_request_marker=False,
            )
            ledger.update(record.request_id, status=RequestStatus.SENDING, attempts=1)
            raise RuntimeError("ambiguous failure after send boundary")

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state.update(
        page_id="maint-page",
        page_url="https://chatgpt.com/c/old",
        conversation_generation=0,
        constructor_sent_generation=0,
    )
    coordinator.state_store.save(global_state)

    assert asyncio.run(
        coordinator.advance([(path, state)], SimpleNamespace())
    ) is True
    after_failure = store.load(path)
    assert asyncio.run(
        coordinator.advance([(path, after_failure)], SimpleNamespace())
    ) is True

    assert len(attempts) == 1
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert incident["state"] == "ESCALATED"
    assert "conversation generation changed" in incident["last_error"]
    assert "sending" in incident["last_error"]
    assert current["maintenance"]["active_incident_id"] is None
    assert not current["controls"]
    assert incident["report_path"] is None
    saved_global = coordinator.state_store.load()
    assert saved_global["conversation_generation"] == 1
    assert saved_global["constructor_sent_generation"] == 0
    assert saved_global["active_incident"] is None


def test_coordinator_reuses_exact_prompt_and_request_after_transient_failure(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Recover durably",
        requested_team="alpha",
        task_id="task-maint-durable",
    )
    path = Path(state["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="offline",
        )
        return current

    state = store.update(path, block)
    prompts: list[str] = []
    request_ids: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=not prompts,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **kwargs):
            prompts.append(prompt)
            request_ids.append(kwargs["request_id"])

        async def run(self, _context):
            if len(prompts) == 1:
                raise RuntimeError("temporary transport error")
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(coordinator.advance([(path, state)], SimpleNamespace())) is True
    after_failure = store.load(path)
    assert after_failure["maintenance"]["incidents"][0]["state"] == "RUNNING"

    assert asyncio.run(
        coordinator.advance([(path, after_failure)], SimpleNamespace())
    ) is True

    assert len(prompts) == 2
    assert prompts[0] == prompts[1]
    assert request_ids[0] == request_ids[1]


@pytest.mark.parametrize(
    ("action", "role", "expected"),
    [
        ("RESUME_TASK", None, "resume"),
        ("RETRY_HOP", None, "retry"),
        ("RESTART_ROLE", "DEV", "restart_role"),
        ("NEW_CHAT_ROLE", "DEV", "new_chat"),
        ("OPEN_ROLE_TAB", "DEV", "open_tab"),
        ("ROUTE_PLAN", None, "route_plan"),
    ],
)
def test_phase1_actions_dispatch_only_through_existing_controls(
    tmp_path: Path, action: str, role: str | None, expected: str
):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Dispatch recovery",
        requested_team="alpha",
        task_id=f"task-{expected.replace('_', '-')}",
    )
    path = Path(state["manifest_path"])
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_retryable"] = True
    state = store.save(path, state)
    coordinator = __import__(
        "playwright_auto.cdpa_maintenance", fromlist=["MaintainerCoordinator"]
    ).MaintainerCoordinator(config, store=store)

    incident = {
        "incident_id": f"maint-{expected}",
        "request_id": f"maint-{expected}-request",
    }
    result = coordinator._dispatch(
        path,
        state,
        MaintenanceDecision(action=action, reason="recover", role=role),
        incident=incident,
    )

    assert result is not None
    assert result["controls"][-1]["action"] == expected
    assert result["controls"][-1]["status"] == "requested"
    assert result["controls"][-1]["maintenance_incident_id"] == incident["incident_id"]
    assert result["controls"][-1]["maintenance_request_id"] == incident["request_id"]

    repeated = coordinator._dispatch(
        path,
        result,
        MaintenanceDecision(action=action, reason="recover", role=role),
        incident=incident,
    )
    assert repeated is not None
    assert len(repeated["controls"]) == 1


def test_saved_decision_recovers_missing_control_without_resending(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Recover control write",
        requested_team="alpha",
        task_id="task-control-recovery",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        request_id="maint-control-recovery-request",
        decision={
            "action": "OPEN_ROLE_TAB",
            "reason": "recover exact tab",
            "role": "DEV",
            "lesson": None,
            "replacement": None,
        },
        applied_snapshot_key=maintenance_incident_key(state),
    )
    state = store.save(path, state)

    class NoSend:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("decision recovery must not resend")

    monkeypatch.setattr(maintenance_module, "DurableSendBlock", NoSend)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(
        coordinator.advance([(path, state)], SimpleNamespace())
    ) is True
    current = store.load(path)
    recovered = current["maintenance"]["incidents"][0]
    assert recovered["control_id"] == current["controls"][-1]["control_id"]
    assert current["controls"][-1]["action"] == "open_tab"
    assert len(current["controls"]) == 1


def test_saved_decision_ignores_historical_matching_control_and_recovers_exact_current_control(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Recover current incident only",
        requested_team="alpha",
        task_id="task-control-provenance",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    state = store.save(path, state)
    historical = store.request_control(
        path,
        "open_tab",
        role="DEV",
        reason="recover exact tab",
    )

    def mark_historical_applied(current):
        current["controls"][-1].update(
            status="applied",
            applied_at="2026-07-23T00:00:00+00:00",
            result="historical role open",
        )
        return current

    state = store.update(path, mark_historical_applied)
    assert historical["controls"][-1]["control_id"] == 1
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        request_id="maint-current-request",
        decision={
            "action": "OPEN_ROLE_TAB",
            "reason": "recover exact tab",
            "role": "DEV",
            "lesson": None,
            "replacement": None,
        },
        applied_snapshot_key=incident["operational_key"],
    )
    state = store.save_maintenance(path, state)

    class NoSend:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("saved decision recovery must not send Maintainers again")

    monkeypatch.setattr(maintenance_module, "DurableSendBlock", NoSend)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(
        coordinator.advance([(path, state)], SimpleNamespace())
    ) is True
    current = store.load(path)
    recovered = current["maintenance"]["incidents"][0]
    assert len(current["controls"]) == 2
    assert recovered["control_id"] == 2
    assert current["controls"][0]["status"] == "applied"
    assert current["controls"][1]["status"] == "requested"
    assert current["controls"][1]["maintenance_incident_id"] == incident["incident_id"]
    assert current["controls"][1]["maintenance_request_id"] == "maint-current-request"

    recovered.pop("control_id")
    current = store.save_maintenance(path, current)
    assert asyncio.run(
        coordinator.advance([(path, current)], SimpleNamespace())
    ) is True
    after_crash_recovery = store.load(path)
    recovered = after_crash_recovery["maintenance"]["incidents"][0]
    assert len(after_crash_recovery["controls"]) == 2
    assert recovered["control_id"] == 2


def test_applied_control_with_unchanged_block_escalates_without_new_incident(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    learning = tmp_path / "LEARNING.md"
    learning.write_text("# LEARNING.md\n", encoding="utf-8")
    state = store.create_task(
        "Ineffective recovery",
        requested_team="alpha",
        task_id="task-ineffective-recovery",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="owned alpha-dev tab is offline",
        active_role="PLAN",
    )
    state = store.save(path, state)
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        request_id="maint-ineffective-recovery-request",
        decision={
            "action": "OPEN_ROLE_TAB",
            "reason": "recover exact tab",
            "role": "DEV",
            "lesson": "Do not record lessons for ineffective recovery.",
            "replacement": None,
        },
        applied_snapshot_key=incident["operational_key"],
    )
    state = store.save_maintenance(path, state)
    controlled = store.request_control(
        path,
        "open_tab",
        role="DEV",
        reason="recover exact tab",
        maintenance_incident_id=incident["incident_id"],
        maintenance_request_id=incident["request_id"],
    )
    state = store.load(path)
    incident = state["maintenance"]["incidents"][0]
    incident["control_id"] = controlled["controls"][-1]["control_id"]
    state = store.save_maintenance(path, state)

    def mark_applied(current):
        current["controls"][-1].update(
            status="applied",
            applied_at="2026-07-23T00:00:00+00:00",
            result="opened requested role",
        )
        return current

    state = store.update(path, mark_applied)

    class NoSend:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("unchanged incident must not be sent again")

    monkeypatch.setattr(maintenance_module, "DurableSendBlock", NoSend)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(
        coordinator.advance([(path, state)], SimpleNamespace())
    ) is True
    escalated = store.load(path)
    incident = escalated["maintenance"]["incidents"][0]
    assert incident["state"] == "ESCALATED"
    assert escalated["maintenance"]["active_incident_id"] is None
    assert "unchanged" in incident["last_error"]
    assert "ineffective recovery" not in learning.read_text(encoding="utf-8")

    assert asyncio.run(
        coordinator.advance([(path, escalated)], SimpleNamespace())
    ) is False
    final = store.load(path)
    assert len(final["maintenance"]["incidents"]) == 1
    assert final["maintenance"]["active_incident_id"] is None
    assert final["maintenance"]["suppressed_operational_key"]

    def recover(current):
        current.update(
            status="RUNNING",
            kanban_column="WORKING",
            block_code=None,
            block_reason=None,
        )
        return current

    recovered = store.update(path, recover)
    assert asyncio.run(
        coordinator.advance([(path, recovered)], SimpleNamespace())
    ) is True
    recovered = store.load(path)
    assert "suppressed_operational_key" not in recovered["maintenance"]

    def recur(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="owned alpha-dev tab is offline",
        )
        return current

    recurred = store.update(path, recur)
    new_incident = ensure_maintenance_incident(recurred)
    assert new_incident is not None
    assert new_incident["state"] == "OPEN"
    assert len(recurred["maintenance"]["incidents"]) == 2


def test_wait_response_releases_ownership_and_is_not_resent_unchanged(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Wait for evidence",
        requested_team="alpha",
        task_id="task-wait-release",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    state = store.save(path, state)
    sends: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            sends.append(prompt)

        async def run(self, _context):
            return {"response": {"text": WAIT_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(coordinator.advance([(path, state)], SimpleNamespace())) is True
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert len(sends) == 1
    assert incident["state"] == "OPEN"
    assert incident["decision"]["action"] == "WAIT"
    assert incident["wait_snapshot_key"] == maintenance_incident_key(current)
    assert current["maintenance"]["active_incident_id"] is None
    assert current["controls"] == []
    assert coordinator.state_store.load()["active_incident"] is None

    assert asyncio.run(coordinator.advance([(path, current)], SimpleNamespace())) is False
    assert len(sends) == 1


def test_wait_releases_global_sidecar_and_later_evidence_gets_new_turn(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("First", requested_team="alpha", task_id="task-first")
    second = store.create_task("Second", requested_team="beta", task_id="task-second")
    paths = [Path(first["manifest_path"]), Path(second["manifest_path"])]
    states = []
    for path in paths:
        state = store.load(path)
        state.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="offline",
        )
        states.append(store.save(path, state))
    incident = ensure_maintenance_incident(states[0])
    assert incident is not None
    incident.update(
        state="OPEN",
        decision={
            "action": "WAIT",
            "reason": "wait for operator evidence",
            "role": None,
            "lesson": None,
            "replacement": None,
        },
        wait_snapshot_key=maintenance_incident_key(states[0]),
    )
    states[0] = store.save_maintenance(paths[0], states[0])
    sends: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            sends.append(prompt)

        async def run(self, _context):
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state["active_incident"] = {
        "task_id": states[0]["task_id"],
        "incident_id": incident["incident_id"],
        "turn": 1,
        "request_id": "wait-request",
    }
    coordinator.state_store.save(global_state)

    assert asyncio.run(
        coordinator.advance(list(zip(paths, states, strict=True)), SimpleNamespace())
    ) is True
    released = store.load(paths[0])
    assert released["maintenance"]["active_incident_id"] is None
    assert released["maintenance"]["incidents"][0]["state"] == "OPEN"
    assert released["maintenance"]["incidents"][0]["decision"]["action"] == "WAIT"
    assert coordinator.state_store.load()["active_incident"] is None
    assert sends == []

    assert asyncio.run(
        coordinator.advance(
            [(paths[0], released), (paths[1], store.load(paths[1]))],
            SimpleNamespace(),
        )
    ) is True
    second_state = store.load(paths[1])
    assert len(sends) == 1
    assert "task-second" in sends[0]
    assert second_state["maintenance"]["incidents"][0]["decision"] is not None

    second_incident = second_state["maintenance"]["incidents"][0]
    second_incident["state"] = "RESOLVED"
    second_state["maintenance"]["active_incident_id"] = None
    store.save_maintenance(paths[1], second_state)
    global_state = coordinator.state_store.load()
    global_state["active_incident"] = None
    coordinator.state_store.save(global_state)

    def touch_first(current):
        current["last_role_activity_at"] = "2026-07-23T02:00:00+00:00"
        return current

    changed_first = store.update(paths[0], touch_first)
    assert maintenance_incident_key(changed_first) != incident["wait_snapshot_key"]
    assert asyncio.run(
        coordinator.advance(
            [(paths[0], changed_first), (paths[1], store.load(paths[1]))],
            SimpleNamespace(),
        )
    ) is True
    revisited = store.load(paths[0])
    assert len(sends) == 2
    assert "task-first" in sends[1]
    assert len(revisited["maintenance"]["incidents"]) == 2
    assert revisited["maintenance"]["active_incident_id"] == revisited["maintenance"]["incidents"][1]["incident_id"]


def test_stale_response_is_discarded_when_task_recovers_while_waiting(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    learning = tmp_path / "LEARNING.md"
    learning.write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    state = store.create_task(
        "Recover while Maintainers waits",
        requested_team="alpha",
        task_id="task-stale-response-recovered",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state = store.save(path, state)
    sends: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            sends.append(prompt)

        async def run(self, _context):
            def recover(current):
                current.update(
                    status="RUNNING",
                    kanban_column="WORKING",
                    block_code=None,
                    block_reason=None,
                )
                return current

            store.update(path, recover)
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(coordinator.advance([(path, state)], SimpleNamespace())) is True
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert len(sends) == 1
    assert current["status"] == "RUNNING"
    assert current["block_code"] is None
    assert current["controls"] == []
    assert current["maintenance"]["active_incident_id"] is None
    assert incident["state"] == "RESOLVED"
    assert incident["decision"] is None
    assert incident["report_path"] is None
    assert incident["superseded_by_key"] is None
    assert incident["superseded_response_request_id"] == incident["request_id"]
    assert coordinator.state_store.load()["active_incident"] is None
    assert list((tmp_path / ".plan" / "maintainers").glob("*.md")) == []
    assert learning.read_text(encoding="utf-8") == "# LEARNING.md\n"

    assert asyncio.run(coordinator.advance([(path, current)], SimpleNamespace())) is False
    assert len(sends) == 1


def test_stale_response_for_a_is_not_applied_to_b_and_b_gets_new_request(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Incident changes while Maintainers waits",
        requested_team="alpha",
        task_id="task-stale-response-changed",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state = store.save(path, state)
    prompts: list[str] = []
    request_ids: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **kwargs):
            prompts.append(prompt)
            request_ids.append(kwargs["request_id"])

        async def run(self, _context):
            if len(prompts) == 1:
                def change_incident(current):
                    current.update(
                        block_code="send_failed",
                        block_reason="failure B",
                    )
                    return current

                store.update(path, change_incident)
            return {"response": {"text": VALID_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert asyncio.run(coordinator.advance([(path, state)], SimpleNamespace())) is True
    after_a = store.load(path)
    incident_a = after_a["maintenance"]["incidents"][0]
    assert after_a["block_code"] == "send_failed"
    assert after_a["controls"] == []
    assert after_a["maintenance"]["active_incident_id"] is None
    assert incident_a["state"] == "RESOLVED"
    assert incident_a["decision"] is None
    assert incident_a["superseded_by_key"] == maintenance_incident_key(after_a)

    assert asyncio.run(coordinator.advance([(path, after_a)], SimpleNamespace())) is True
    final = store.load(path)
    assert len(prompts) == 2
    assert "failure A" in prompts[0]
    assert "failure B" in prompts[1]
    assert request_ids[0] != request_ids[1]
    assert len(final["maintenance"]["incidents"]) == 2
    incident_a, incident_b = final["maintenance"]["incidents"]
    assert incident_a["decision"] is None
    assert incident_b["trigger_code"] == "send_failed"
    assert incident_b["trigger_reason"] == "failure B"
    assert incident_b["decision"]["action"] == "OPEN_ROLE_TAB"
    assert incident_b["request_id"] == request_ids[1]
    assert len(final["controls"]) == 1
    assert final["controls"][0]["maintenance_incident_id"] == incident_b["incident_id"]
    assert final["controls"][0]["maintenance_request_id"] == incident_b["request_id"]
    assert len(list((tmp_path / ".plan" / "maintainers").glob("*.md"))) == 1


@pytest.mark.parametrize(
    ("action", "role"),
    [("WAIT", None), ("OPEN_ROLE_TAB", "DEV")],
)
def test_locked_response_commit_rechecks_incident_after_outer_precheck(
    tmp_path: Path, action: str, role: str | None
):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Locked stale response guard",
        requested_team="alpha",
        task_id="task-locked-stale-response",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state = store.save(path, state)
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        turn=1,
        request_id="outer-precheck-request",
        report_at="2026-07-23T01:02:03+00:00",
    )
    state = store.save_maintenance(path, state)
    outer_key = str(incident["key"])

    def change_after_precheck(current):
        current.update(
            block_code="send_failed",
            block_reason="failure B",
        )
        return current

    store.update(path, change_after_precheck)
    coordinator = __import__(
        "playwright_auto.cdpa_maintenance", fromlist=["MaintainerCoordinator"]
    ).MaintainerCoordinator(config, store=store)
    saved, committed_incident, evidence, control, stale = coordinator._commit_response(
        path,
        incident_id=str(incident["incident_id"]),
        expected_incident_key=outer_key,
        request_id="outer-precheck-request",
        turn=1,
        report="# Stale report\n",
        report_at=datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
        decision=MaintenanceDecision(
            action=action,
            reason="recover failure A",
            role=role,
        ),
    )

    assert stale is True
    assert evidence is None
    assert control is None
    assert saved["controls"] == []
    assert saved["maintenance"]["active_incident_id"] is None
    assert committed_incident["state"] == "RESOLVED"
    assert committed_incident["decision"] is None
    assert committed_incident["report_path"] is None
    assert committed_incident["superseded_by_key"] == maintenance_incident_key(saved)
    assert list((tmp_path / ".plan" / "maintainers").glob("*.md")) == []


def test_restart_clears_global_active_after_stale_response_task_commit(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    learning = tmp_path / "LEARNING.md"
    learning.write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    state = store.create_task(
        "Crash after stale task commit",
        requested_team="alpha",
        task_id="task-crash-stale-global",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state = store.save(path, state)
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        turn=1,
        request_id="request-stale-a",
        report_at="2026-07-23T01:02:03+00:00",
    )
    state = store.save_maintenance(path, state)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state["active_incident"] = {
        "task_id": state["task_id"],
        "incident_id": incident["incident_id"],
        "turn": 1,
        "request_id": incident["request_id"],
    }
    coordinator.state_store.save(global_state)

    def recover(current):
        current.update(
            status="RUNNING",
            kanban_column="WORKING",
            block_code=None,
            block_reason=None,
        )
        return current

    store.update(path, recover)
    committed, retired, evidence, control, stale = coordinator._commit_response(
        path,
        incident_id=str(incident["incident_id"]),
        expected_incident_key=str(incident["key"]),
        request_id=str(incident["request_id"]),
        turn=1,
        report="# Stale report\n",
        report_at=datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
        decision=MaintenanceDecision(
            action="OPEN_ROLE_TAB",
            reason="recover failure A",
            role="DEV",
            lesson="This stale lesson must not be appended.",
        ),
    )
    assert stale is True
    assert evidence is None
    assert control is None
    assert retired["state"] == "RESOLVED"

    class NoBrowserWork:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("global-state reconciliation must not touch the browser")

    monkeypatch.setattr(maintenance_module, "CDPATabActions", NoBrowserWork)
    restarted = maintenance_module.MaintainerCoordinator(config, store=TaskStore(config))
    assert asyncio.run(restarted.advance([(path, committed)], SimpleNamespace())) is True
    repaired = restarted.state_store.load()
    assert repaired["active_incident"] is None
    assert repaired["history"] == []
    assert store.load(path)["controls"] == []
    assert list((tmp_path / ".plan" / "maintainers").glob("*.md")) == []
    assert learning.read_text(encoding="utf-8") == "# LEARNING.md\n"

    assert asyncio.run(
        restarted.advance([(path, store.load(path))], SimpleNamespace())
    ) is False


def test_restart_reconstructs_non_wait_global_projection_after_task_commit(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Crash after valid task commit",
        requested_team="alpha",
        task_id="task-crash-valid-global",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state = store.save(path, state)
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        turn=1,
        request_id="request-valid-a",
        report_at="2026-07-23T01:02:03+00:00",
    )
    state = store.save_maintenance(path, state)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state["active_incident"] = {
        "task_id": state["task_id"],
        "incident_id": incident["incident_id"],
        "turn": 1,
        "request_id": incident["request_id"],
    }
    coordinator.state_store.save(global_state)
    committed, committed_incident, evidence, control, stale = coordinator._commit_response(
        path,
        incident_id=str(incident["incident_id"]),
        expected_incident_key=str(incident["key"]),
        request_id=str(incident["request_id"]),
        turn=1,
        report="# Valid report\n",
        report_at=datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
        decision=MaintenanceDecision(
            action="OPEN_ROLE_TAB",
            reason="recover failure A",
            role="DEV",
        ),
    )
    assert stale is False
    assert evidence is not None
    assert control is not None

    class NoBrowserWork:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("global-state reconciliation must not touch the browser")

    monkeypatch.setattr(maintenance_module, "CDPATabActions", NoBrowserWork)
    restarted = maintenance_module.MaintainerCoordinator(config, store=TaskStore(config))
    assert asyncio.run(restarted.advance([(path, committed)], SimpleNamespace())) is True
    repaired = restarted.state_store.load()
    assert repaired["active_incident"] == {
        "task_id": committed["task_id"],
        "team": committed["team"],
        "incident_id": committed_incident["incident_id"],
        "turn": 1,
        "request_id": committed_incident["request_id"],
        "action": "OPEN_ROLE_TAB",
        "report_path": evidence.path,
        "report_sha256": evidence.sha256,
    }
    assert len(repaired["history"]) == 1
    assert repaired["history"][0]["request_id"] == committed_incident["request_id"]
    assert repaired["history"][0]["report_path"] == evidence.path
    assert repaired["history"][0]["report_sha256"] == evidence.sha256
    assert len(store.load(path)["controls"]) == 1
    assert len(list((tmp_path / ".plan" / "maintainers").glob("*.md"))) == 1

    assert asyncio.run(
        restarted.advance([(path, store.load(path))], SimpleNamespace())
    ) is False
    unchanged = restarted.state_store.load()
    assert unchanged["active_incident"] == repaired["active_incident"]
    assert unchanged["history"] == repaired["history"]


def test_restart_reconstructs_wait_history_and_keeps_global_inactive(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Crash after WAIT task commit",
        requested_team="alpha",
        task_id="task-crash-wait-global",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state = store.save(path, state)
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        turn=1,
        request_id="request-wait-a",
        report_at="2026-07-23T01:02:03+00:00",
    )
    state = store.save_maintenance(path, state)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state["active_incident"] = {
        "task_id": state["task_id"],
        "incident_id": incident["incident_id"],
        "turn": 1,
        "request_id": incident["request_id"],
    }
    coordinator.state_store.save(global_state)
    committed, committed_incident, evidence, control, stale = coordinator._commit_response(
        path,
        incident_id=str(incident["incident_id"]),
        expected_incident_key=str(incident["key"]),
        request_id=str(incident["request_id"]),
        turn=1,
        report="# WAIT report\n",
        report_at=datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
        decision=MaintenanceDecision(
            action="WAIT",
            reason="wait for changed evidence",
        ),
    )
    assert stale is False
    assert evidence is not None
    assert control is None
    assert committed["maintenance"]["active_incident_id"] is None

    class NoBrowserWork:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("global-state reconciliation must not touch the browser")

    monkeypatch.setattr(maintenance_module, "CDPATabActions", NoBrowserWork)
    restarted = maintenance_module.MaintainerCoordinator(config, store=TaskStore(config))
    assert asyncio.run(restarted.advance([(path, committed)], SimpleNamespace())) is True
    repaired = restarted.state_store.load()
    assert repaired["active_incident"] is None
    assert len(repaired["history"]) == 1
    assert repaired["history"][0]["request_id"] == committed_incident["request_id"]
    assert repaired["history"][0]["action"] == "WAIT"
    assert repaired["history"][0]["report_path"] == evidence.path
    assert len(store.load(path)["controls"]) == 0
    assert len(list((tmp_path / ".plan" / "maintainers").glob("*.md"))) == 1

    assert asyncio.run(
        restarted.advance([(path, store.load(path))], SimpleNamespace())
    ) is False
    unchanged = restarted.state_store.load()
    assert unchanged["active_incident"] is None
    assert unchanged["history"] == repaired["history"]


def test_restart_recovers_turn_and_constructor_watermarks_before_next_incident(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text(
        "# LEARNING.md\n\nDo not repeat this constructor payload.\n",
        encoding="utf-8",
    )
    store = TaskStore(config)
    state_a = store.create_task(
        "Committed incident A",
        requested_team="alpha",
        task_id="task-watermark-a",
    )
    path_a = Path(state_a["manifest_path"])
    state_a.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state_a = store.save(path_a, state_a)
    incident_a = ensure_maintenance_incident(state_a)
    assert incident_a is not None
    incident_a.update(
        state="RUNNING",
        turn=3,
        request_id="request-watermark-a",
        report_at="2026-07-23T01:02:03+00:00",
        prompt_generation=0,
        constructor_included=True,
    )
    state_a = store.save_maintenance(path_a, state_a)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    committed_a, committed_incident, evidence, control, stale = coordinator._commit_response(
        path_a,
        incident_id=str(incident_a["incident_id"]),
        expected_incident_key=str(incident_a["key"]),
        request_id=str(incident_a["request_id"]),
        turn=3,
        report="# Committed A report\n",
        report_at=datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc),
        decision=MaintenanceDecision(
            action="WAIT",
            reason="wait after committed A",
        ),
    )
    assert stale is False
    assert evidence is not None
    assert control is None
    assert committed_incident["turn"] == 3

    state_b = store.create_task(
        "Next incident B",
        requested_team="beta",
        task_id="task-watermark-b",
    )
    path_b = Path(state_b["manifest_path"])
    state_b.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="send_failed",
        block_reason="failure B",
    )
    state_b = store.save(path_b, state_b)

    global_state = coordinator.state_store.load()
    global_state.update(
        page_id="maint-page",
        page_url="https://chatgpt.com/c/maint-watermark",
        conversation_generation=0,
        constructor_sent_generation=None,
        turn=0,
        active_incident=None,
        history=[],
    )
    coordinator.state_store.save(global_state)
    prompts: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint-watermark",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            prompts.append(prompt)

        async def run(self, _context):
            return {"response": {"text": WAIT_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)
    restarted = maintenance_module.MaintainerCoordinator(config, store=TaskStore(config))

    assert asyncio.run(
        restarted.advance(
            [(path_a, committed_a), (path_b, state_b)],
            SimpleNamespace(),
        )
    ) is True
    repaired = restarted.state_store.load()
    assert repaired["turn"] == 3
    assert repaired["constructor_sent_generation"] == 0
    assert len(repaired["history"]) == 1
    assert prompts == []

    assert asyncio.run(
        restarted.advance(
            [(path_a, store.load(path_a)), (path_b, store.load(path_b))],
            SimpleNamespace(),
        )
    ) is True
    current_b = store.load(path_b)
    incident_b = current_b["maintenance"]["incidents"][0]
    constructor = config.maintainers_constructor_path.read_text(encoding="utf-8").strip()
    assert incident_b["turn"] == 4
    assert len(prompts) == 1
    assert constructor not in prompts[0]
    assert "CURRENT LEARNING.md" not in prompts[0]
    final_global = restarted.state_store.load()
    assert final_global["turn"] == 4
    assert final_global["constructor_sent_generation"] == 0


def test_same_wait_response_commit_is_idempotent_but_changed_key_is_stale(
    tmp_path: Path,
):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Idempotent WAIT commit",
        requested_team="alpha",
        task_id="task-idempotent-wait",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state = store.save(path, state)
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident.update(
        state="RUNNING",
        turn=1,
        request_id="request-idempotent-wait",
        report_at="2026-07-23T00:10:00+00:00",
    )
    state = store.save_maintenance(path, state)
    coordinator = __import__(
        "playwright_auto.cdpa_maintenance", fromlist=["MaintainerCoordinator"]
    ).MaintainerCoordinator(config, store=store)
    kwargs = dict(
        incident_id=str(incident["incident_id"]),
        expected_incident_key=str(incident["key"]),
        request_id=str(incident["request_id"]),
        turn=1,
        report="# WAIT report\n\nNo safe action yet.\n",
        report_at=datetime(2026, 7, 23, 0, 10, 0, tzinfo=timezone.utc),
        decision=MaintenanceDecision(action="WAIT", reason="wait"),
    )

    first = coordinator._commit_response(path, **kwargs)
    second = coordinator._commit_response(path, **kwargs)

    assert first[4] is False
    assert second[4] is False
    assert first[2] == second[2]
    assert first[3] is None
    assert second[3] is None
    accepted = store.load(path)
    accepted_incident = accepted["maintenance"]["incidents"][0]
    assert accepted_incident["state"] == "OPEN"
    assert accepted_incident["decision"] == {
        "action": "WAIT",
        "reason": "wait",
        "role": None,
        "lesson": None,
        "replacement": None,
    }
    assert accepted_incident["report_path"] == first[2].path
    assert accepted_incident["report_sha256"] == first[2].sha256
    assert accepted_incident["report_size"] == first[2].size
    assert accepted_incident.get("superseded_reason") is None
    assert len(list((tmp_path / ".plan" / "maintainers").glob("*.md"))) == 1

    def change_key(current):
        current.update(block_code="send_failed", block_reason="failure B")
        return current

    store.update(path, change_key)
    third = coordinator._commit_response(path, **kwargs)
    assert third[4] is True
    assert third[2] is None
    assert third[3] is None
    stale_incident = store.load(path)["maintenance"]["incidents"][0]
    assert stale_incident["state"] == "RESOLVED"
    assert stale_incident["decision"] is None
    assert stale_incident["report_path"] is None
    assert stale_incident["superseded_by_key"] == maintenance_incident_key(store.load(path))


def test_global_operation_lock_skips_overlapping_coordinator_and_task_b(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    states = []
    for team, task_id, reason in (
        ("alpha", "task-global-lock-a", "failure A"),
        ("beta", "task-global-lock-b", "failure B"),
    ):
        state = store.create_task(
            f"Global lock {team}", requested_team=team, task_id=task_id
        )
        path = Path(state["manifest_path"])
        state.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason=reason,
        )
        states.append((path, store.save(path, state)))

    entered = asyncio.Event()
    release = asyncio.Event()
    prompts: list[str] = []
    acquisitions: list[str] = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            acquisitions.append(role)
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint-global-lock",
                created=False,
                new_chat=False,
            )

    class BlockingSendBlock:
        def __init__(self, prompt, **_kwargs):
            prompts.append(prompt)

        async def run(self, _context):
            entered.set()
            await release.wait()
            return {"response": {"text": WAIT_RESPONSE}}

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", BlockingSendBlock)
    first = maintenance_module.MaintainerCoordinator(config, store=store)
    second = maintenance_module.MaintainerCoordinator(config, store=TaskStore(config))

    async def run_both():
        first_task = asyncio.create_task(first.advance(states, SimpleNamespace()))
        await entered.wait()
        second_result = await second.advance(states, SimpleNamespace())
        assert second_result is False
        assert acquisitions == [MAINTAINER_ROLE]
        assert len(prompts) == 1
        assert "failure A" in prompts[0]
        assert "failure B" not in prompts[0]
        release.set()
        return await first_task

    assert asyncio.run(run_both()) is True
    final_a = store.load(states[0][0])
    final_b = store.load(states[1][0])
    incident_a = final_a["maintenance"]["incidents"][0]
    incident_b = final_b["maintenance"]["incidents"][0]
    assert incident_a["state"] == "OPEN"
    assert incident_a["decision"]["action"] == "WAIT"
    assert incident_a["report_path"]
    assert incident_a["report_sha256"]
    assert incident_a["report_size"] > 0
    assert incident_b["decision"] is None
    assert len(prompts) == 1
    assert len(acquisitions) == 1
    reports = list((tmp_path / ".plan" / "maintainers").glob("*.md"))
    assert len(reports) == 1
    global_state = first.state_store.load()
    assert global_state["active_incident"] is None
    assert len(global_state["history"]) == 1
    assert global_state["history"][0]["request_id"] == incident_a["request_id"]


def test_coordinator_propagates_cdp_disconnect_for_worker_reconnect(
    tmp_path: Path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Reconnect Maintainers",
        requested_team="alpha",
        task_id="task-maint-disconnect",
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="offline",
    )
    state = store.save(path, state)
    TargetClosedError = type("TargetClosedError", (RuntimeError,), {})

    class DisconnectedActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, _role):
            raise TargetClosedError("Target page, context or browser has been closed")

    monkeypatch.setattr(
        maintenance_module, "CDPATabActions", DisconnectedActions
    )
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    with pytest.raises(TargetClosedError):
        asyncio.run(coordinator.advance([(path, state)], SimpleNamespace()))
