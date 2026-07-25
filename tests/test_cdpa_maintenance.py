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


def replacement_response(**overrides):
    replacement = {
        "target_task_id": "task-parent",
        "task": "Continue the parent outcome safely",
        "reuse_team": True,
        "rewire_children": True,
    }
    replacement.update(overrides.pop("replacement", {}))
    value = {
        "action": "REPLACE_TASK",
        "reason": "The stopped parent cannot safely continue.",
        "role": None,
        "lesson": None,
        "replacement": replacement,
    }
    value.update(overrides)
    return "# Maintenance report\n\nReplacement is the smallest safe recovery.\n\n```json\n" + __import__("json").dumps(value) + "\n```"


def test_parse_replace_task_requires_exact_replacement_contract():
    _report, decision = parse_maintenance_response(replacement_response())
    assert decision.action == "REPLACE_TASK"
    assert decision.role is None
    assert decision.replacement == {
        "target_task_id": "task-parent",
        "task": "Continue the parent outcome safely",
        "reuse_team": True,
        "rewire_children": True,
    }

    invalid = [
        replacement_response(role="DEV"),
        replacement_response(replacement={"target_task_id": ""}),
        replacement_response(replacement={"task": ""}),
        replacement_response(replacement={"reuse_team": "yes"}),
        replacement_response(replacement={"rewire_children": 1}),
        replacement_response(replacement={"extra": True}),
    ]
    for value in invalid:
        with pytest.raises(ValueError):
            parse_maintenance_response(value)


def test_waiting_missing_dependency_creates_one_maintenance_incident():
    state = task_state(status="WAITING", block_code=None, terminal_state=None)
    state["waiting_code"] = "dependency_missing"
    state["waiting_reason"] = "Waiting for dependencies: task-missing"
    state["waiting"] = {
        "reason": "dependency",
        "waiting_on": [],
        "stopped": [],
        "missing": ["task-missing"],
        "since": "2026-07-23T01:00:00+00:00",
    }
    first = ensure_maintenance_incident(state)
    second = ensure_maintenance_incident(state)
    assert first is second
    assert first is not None
    assert first["trigger_status"] == "WAITING"
    assert first["trigger_code"] == "dependency_missing"
    assert len(state["maintenance"]["incidents"]) == 1


def test_reconcile_replace_task_ignores_catalog_invalid_recorded_replacement(
    tmp_path: Path,
    monkeypatch,
):
    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore, utc_now
    from test_cdpa_core import poison_catalog_identity_entry, write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "unsafe to continue",
        },
    )
    incident = ensure_maintenance_incident(parent)
    assert incident is not None
    incident.update({
        "state": "RUNNING",
        "turn": 1,
        "request_id": "maint-filtered-replace-turn1",
        "report_path": str(tmp_path / ".plan" / "maintainers" / "report.md"),
        "report_sha256": "a" * 64,
        "report_size": 10,
        "decision": {
            "action": "REPLACE_TASK",
            "reason": "stopped parent cannot continue",
            "role": None,
            "lesson": None,
            "replacement": {
                "target_task_id": "task-parent",
                "task": "Continue parent safely",
                "reuse_team": True,
                "rewire_children": True,
            },
        },
    })
    parent = store.save_maintenance(parent["manifest_path"], parent)
    invalid_replacement = store.create_task(
        "Invalid recorded replacement",
        requested_team="invalid-replacement",
        task_id="task-invalid-replacement",
        replaces_task_id=parent["task_id"],
        replacement_incident_id=incident["incident_id"],
    )
    poison_catalog_identity_entry(store, invalid_replacement)
    tasks, _errors = store.discover_with_errors()
    assert invalid_replacement["task_id"] not in {
        task["task_id"] for task in tasks
    }
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    global_state = coordinator.state_store.load()
    global_state["history"] = [{
        "incident_id": incident["incident_id"],
        "request_id": incident["request_id"],
        "application_state": "RESOLVED",
        "replacement_task_id": invalid_replacement["task_id"],
    }]
    coordinator.state_store.save(global_state)
    calls: list[str] = []

    def replace_task_and_rewire(*_args, **_kwargs):
        calls.append("replace")
        return {
            "replacement": {"task_id": "task-valid-replacement"},
            "rewired_children": [],
        }

    monkeypatch.setattr(store, "replace_task_and_rewire", replace_task_and_rewire)

    assert coordinator._reconcile_active(Path(parent["manifest_path"]), parent) is True
    assert calls == ["replace"]


def test_reconcile_replace_task_applies_atomic_rewire_once(tmp_path: Path):
    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore, utc_now
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child", requested_team="child", task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "unsafe to continue",
        },
    )
    incident = ensure_maintenance_incident(parent)
    assert incident is not None
    incident.update({
        "state": "RUNNING",
        "turn": 1,
        "request_id": "maint-replace-turn1",
        "report_path": str(tmp_path / ".plan" / "maintainers" / "report.md"),
        "report_sha256": "a" * 64,
        "report_size": 10,
        "decision": {
            "action": "REPLACE_TASK",
            "reason": "stopped parent cannot continue",
            "role": None,
            "lesson": None,
            "replacement": {
                "target_task_id": "task-parent",
                "task": "Continue parent safely",
                "reuse_team": True,
                "rewire_children": True,
            },
        },
    })
    parent = store.save_maintenance(parent["manifest_path"], parent)
    parent_path = Path(parent["manifest_path"])
    parent_before = parent_path.read_bytes()
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)

    assert coordinator._reconcile_active(parent_path, parent) is True
    assert parent_path.read_bytes() == parent_before
    current_parent = store.load(parent_path)
    current_incident = current_parent["maintenance"]["incidents"][0]
    history = coordinator.state_store.load()["history"]
    applied = next(item for item in history if item["incident_id"] == incident["incident_id"])
    replacement_id = applied["replacement_task_id"]
    replacement = next(task for task in store.discover() if task["task_id"] == replacement_id)
    current_child = store.load(child["manifest_path"])

    assert current_incident["state"] == "RUNNING"
    assert current_parent["maintenance"]["active_incident_id"] == incident["incident_id"]
    assert applied["application_state"] == "RESOLVED"
    assert replacement["replaces_task_id"] == "task-parent"
    assert current_child["depends_on_task_ids"] == [replacement_id]
    assert current_parent["controls"] == []
    assert coordinator._reconcile_active(parent_path, current_parent) is False
    assert parent_path.read_bytes() == parent_before
    loaded = [
        (Path(task["manifest_path"]), task)
        for task in store.discover()
    ]
    coordinator._reconcile_global_state(loaded)
    reconciled_global = coordinator.state_store.load()
    reconciled_applied = next(
        item for item in reconciled_global["history"]
        if item["incident_id"] == incident["incident_id"]
    )
    assert reconciled_global["active_incident"] is None
    assert reconciled_applied["application_state"] == "RESOLVED"
    assert reconciled_applied["replacement_task_id"] == replacement_id
    assert parent_path.read_bytes() == parent_before
    assert len([
        task for task in store.discover()
        if task.get("replacement_incident_id") == incident["incident_id"]
    ]) == 1


def test_maintainers_prompt_includes_derived_dependency_context_without_mirrored_children(tmp_path: Path):
    import json
    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore, utc_now
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child", requested_team="child", task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "unsafe parent",
        },
    )
    incident = ensure_maintenance_incident(parent)
    assert incident is not None
    prompt = maintenance_module.MaintainerCoordinator(config, store=store)._prompt(
        parent,
        incident,
        include_constructor=True,
        tasks=[parent, child],
    )
    envelope = json.loads(prompt.split("\n\n", 1)[0].split("\n", 1)[1])

    assert envelope["dependencies"]["parents"] == []
    assert envelope["dependencies"]["children"] == [
        {"task_id": "task-child", "status": "WAITING", "team": "child"}
    ]
    assert envelope["dependencies"]["waiting"] == parent.get("waiting")
    assert "child_task_ids" not in envelope
    repository = Path(__file__).resolve().parents[1]
    for constructor in (
        repository / "prompts" / "cdpa" / "MAINTAINERS.md",
        repository / "src" / "playwright_auto" / "cdpa_defaults" / "prompts" / "cdpa" / "MAINTAINERS.md",
    ):
        contract = constructor.read_text(encoding="utf-8")
        assert "REPLACE_TASK" in contract
        assert '"rewire_children":true' in contract


def test_reconcile_replace_task_does_not_resolve_before_crash_recovery(
    tmp_path: Path, monkeypatch
):
    import playwright_auto.cdpa_maintenance as maintenance_module
    import playwright_auto.cdpa_store as store_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore, utc_now
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team="aaa", task_id="task-parent"
    )
    child = store.create_task(
        "Child",
        requested_team="zzz",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "unsafe to continue",
        },
    )
    incident = ensure_maintenance_incident(parent)
    assert incident is not None
    incident.update(
        {
            "state": "RUNNING",
            "turn": 1,
            "request_id": "maint-crash-turn1",
            "report_path": str(
                tmp_path / ".plan" / "maintainers" / "crash-report.md"
            ),
            "report_sha256": "a" * 64,
            "report_size": 10,
            "decision": {
                "action": "REPLACE_TASK",
                "reason": "stopped parent cannot continue",
                "role": None,
                "lesson": None,
                "replacement": {
                    "target_task_id": "task-parent",
                    "task": "Continue parent safely",
                    "reuse_team": True,
                    "rewire_children": True,
                },
            },
        }
    )
    parent = store.save_maintenance(parent["manifest_path"], parent)
    parent_path = Path(parent["manifest_path"])
    original_replace = store_module.os.replace
    manifest_installs = 0

    def interrupt_before_second_manifest(source, target):
        nonlocal manifest_installs
        if Path(source).name.endswith(".json.phase4.tmp"):
            manifest_installs += 1
            if manifest_installs == 2:
                raise SystemExit("simulated coordinator interruption")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", interrupt_before_second_manifest)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    with pytest.raises(SystemExit, match="coordinator interruption"):
        coordinator._reconcile_active(parent_path, parent)

    global_after_crash = coordinator.state_store.load()
    assert not any(
        item.get("application_state") == "RESOLVED"
        and item.get("incident_id") == incident["incident_id"]
        for item in global_after_crash.get("history") or []
    )
    assert store.phase4_journal_path.exists()

    monkeypatch.setattr(store_module.os, "replace", original_replace)
    restarted_store = TaskStore(config)
    restarted = maintenance_module.MaintainerCoordinator(
        config, store=restarted_store
    )
    current_parent = restarted_store.load(parent_path)
    assert restarted._reconcile_active(parent_path, current_parent) is True

    final_global = restarted.state_store.load()
    applied = next(
        item
        for item in final_global["history"]
        if item["incident_id"] == incident["incident_id"]
    )
    replacement_id = applied["replacement_task_id"]
    assert applied["application_state"] == "RESOLVED"
    assert restarted_store.load(child["manifest_path"])["depends_on_task_ids"] == [
        replacement_id
    ]
    assert not restarted_store.phase4_journal_path.exists()


def test_stopped_maintainers_prompt_includes_original_outcome_repository_and_reports(
    tmp_path: Path,
):
    import hashlib
    import json

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore, utc_now
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    unique_goal = "ORIGINAL UNIQUE GOAL: migrate customer records and preserve checksum 7f4a"
    parent = store.create_task(
        unique_goal,
        requested_team="parent",
        task_id="task-parent-context",
    )
    report_path = (
        tmp_path
        / ".plan"
        / "parent"
        / "parent-plan_turn1_task-parent-context.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = b"# PLAN checkpoint\n\nValidated 73 of 100 records.\n"
    report_path.write_bytes(report_bytes)
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "reports": [
                {
                    "report_id": 1,
                    "physical_role": "parent-plan",
                    "turn": 1,
                    "path": str(report_path),
                    "sha256": hashlib.sha256(report_bytes).hexdigest(),
                    "size": len(report_bytes),
                }
            ],
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "parent cannot safely continue",
        },
    )
    incident = ensure_maintenance_incident(parent)
    assert incident is not None

    prompt = maintenance_module.MaintainerCoordinator(config, store=store)._prompt(
        parent,
        incident,
        include_constructor=False,
        tasks=[parent],
    )
    snapshot = json.loads(prompt.split("\n\n", 1)[0].split("\n", 1)[1])

    assert snapshot["task_text"] == unique_goal
    assert snapshot["repository"] == str(tmp_path)
    assert snapshot["retained_reports"] == [
        {
            "report_id": 1,
            "physical_role": "parent-plan",
            "turn": 1,
            "path": str(report_path),
        }
    ]


def test_parse_v2_maintenance_response_accepts_bounded_recovery_and_repair():
    response = """# Recovery report

OPEN_ROLE_TAB may restore the active request; the permanent repair prevents recurrence.

```json
{"version":2,"recovery":[{"action":"OPEN_ROLE_TAB","reason":"Restore exact DEV conversation.","role":"DEV"},{"action":"RESUME_TASK","reason":"Continue the preserved hop.","role":null}],"repair":{"root_cause":"OPEN_ROLE_TAB could report success while role_offline remained","reason":"Make recovery postconditions authoritative.","disposition":"CONTINUE_IN_PARALLEL","reproduction":"Reopen a mismatched conversation during a role_offline block.","source_areas":["cdpa_worker","cdpa_store","tests","prompts"],"required_tests":["focused role-offline regression","controlled live recovery"]},"lesson":"Recovery is applied only after its operational postcondition passes."}
```
"""

    report, decision = parse_maintenance_response(
        response,
        configured_roles=("PLAN", "DEV", "REVIEW", "TEST", "AUDIT"),
    )

    assert report.startswith("# Recovery report")
    assert decision.version == 2
    assert [step.action for step in decision.recovery] == ["OPEN_ROLE_TAB", "RESUME_TASK"]
    assert decision.recovery[0].role == "DEV"
    assert decision.repair["disposition"] == "CONTINUE_IN_PARALLEL"


def test_parse_v2_rejects_more_than_three_recovery_steps():
    steps = ",".join(
        '{"action":"RESUME_TASK","reason":"step","role":null}' for _ in range(4)
    )
    response = (
        "# Report\n\n"
        f'{{"version":2,"recovery":[{steps}],"repair":null,"lesson":null}}'
    )
    with pytest.raises(ValueError, match="at most three"):
        parse_maintenance_response(response)


def test_resolved_lesson_deduplicates_normalized_markdown_text(tmp_path: Path):
    learning = tmp_path / "LEARNING.md"
    learning.write_text(
        "# LEARNING.md\n\n- Recovery is applied only after its operational postcondition passes!\n",
        encoding="utf-8",
    )
    incident = {"state": "RESOLVED"}
    decision = MaintenanceDecision(
        action="WAIT",
        reason="resolved",
        lesson="  recovery   is applied only after its operational postcondition passes. ",
    )

    assert append_resolved_lesson(learning, incident, decision) is False
    assert learning.read_text(encoding="utf-8").count("Recovery is applied") == 1


def test_repair_lesson_finalizes_only_after_done_and_preserved_task_release(tmp_path: Path):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    learning = tmp_path / "LEARNING.md"
    learning.write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    affected = store.create_task(
        "affected",
        requested_team="alpha",
        task_id="task-affected-lesson",
    )
    repair = store.create_task(
        "repair",
        requested_team="repair-alpha",
        task_id="task-repair-lesson",
    )
    affected_path = Path(affected["manifest_path"])
    repair_path = Path(repair["manifest_path"])
    lesson = "Only finalize a repair lesson after repair DONE and preserved-task release."

    def hold(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="fixture",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident.update(
            state="RESOLVED",
            pending_lesson=lesson,
            repair_task_id=repair["task_id"],
            repair_disposition="HOLD_FOR_REPAIR",
            decision={
                "action": "CREATE_REPAIR_TASK",
                "reason": "repair",
                "role": None,
                "lesson": lesson,
                "replacement": None,
                "version": 2,
                "recovery": [],
                "repair": {
                    "root_cause": "repair lesson gate",
                    "reason": "repair",
                    "disposition": "HOLD_FOR_REPAIR",
                    "reproduction": "fixture",
                    "source_areas": ["tests"],
                    "required_tests": ["fixture"],
                },
            },
        )
        current["maintenance"]["active_incident_id"] = None
        current["maintenance"]["last_resolved_at"] = current["updated_at"]
        current.update(
            status="WAITING",
            kanban_column="WAITING",
            depends_on_task_ids=[repair["task_id"]],
            repair_wait={
                "repair_task_id": repair["task_id"],
                "state": "WAITING",
                "disposition": "HOLD_FOR_REPAIR",
            },
            block_code=None,
            block_reason=None,
        )
        return current

    affected = store.update(affected_path, hold)
    coordinator = MaintainerCoordinator(config, store=store)
    assert coordinator._finalize_repair_lessons(
        [(affected_path, affected), (repair_path, repair)]
    ) is False
    assert "Only finalize" not in learning.read_text(encoding="utf-8")

    def finish(current):
        current.update(
            status="DONE",
            terminal_state="DONE",
            kanban_column="DONE_STOPPED",
            active_action="done",
            active_role=None,
            active_hop_id=None,
        )
        return current

    repair = store.update(repair_path, finish)
    affected = store.load(affected_path)
    assert coordinator._finalize_repair_lessons(
        [(affected_path, affected), (repair_path, repair)]
    ) is False

    def release(current):
        current["repair_wait"]["state"] = "RELEASED"
        return current

    affected = store.update(affected_path, release)
    assert coordinator._finalize_repair_lessons(
        [(affected_path, affected), (repair_path, repair)]
    ) is True
    assert learning.read_text(encoding="utf-8").count("Only finalize") == 1
    saved = store.load(affected_path)
    saved_incident = saved["maintenance"]["incidents"][0]
    assert saved_incident["pending_lesson"] is None
    assert saved_incident["lesson_append_state"] == "appended"

    assert coordinator._finalize_repair_lessons(
        [(affected_path, saved), (repair_path, repair)]
    ) is False
    assert learning.read_text(encoding="utf-8").count("Only finalize") == 1



def test_environment_failure_suspends_after_three_attempts_and_resumes_same_incident(
    tmp_path: Path,
):
    import asyncio
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task("blocked", requested_team="alpha", task_id="task-env")
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="CDP unavailable",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident["request_id"] = f"{incident['incident_id']}-turn1"
        incident["prompt"] = "durable prompt"
        return current

    task = store.update(path, block)
    incident = task["maintenance"]["incidents"][0]
    coordinator = MaintainerCoordinator(config, store=store)

    class OfflineContext:
        @property
        def pages(self):
            raise ConnectionError("CDP offline")

    global_state = coordinator.state_store.load()
    for expected in (1, 2, 3):
        assert coordinator._record_environment_failure(
            path,
            incident_id=incident["incident_id"],
            error=ConnectionError("CDP unavailable"),
            browser_context=OfflineContext(),
            global_state=global_state,
        ) is True
        current = store.load(path)
        incident = current["maintenance"]["incidents"][0]
        assert incident["environment_attempts"] == expected

    suspended = store.load(path)
    suspended_incident = suspended["maintenance"]["incidents"][0]
    assert suspended_incident["state"] == "SUSPENDED"
    assert suspended["maintenance"]["active_incident_id"] is None
    assert suspended_incident["request_id"].endswith("-turn1")
    assert suspended_incident["prompt"] == "durable prompt"
    active_hop_id = suspended["active_hop_id"]

    class OnlineBrowser:
        def is_connected(self):
            return True

    class OnlineContext:
        browser = OnlineBrowser()
        pages = [SimpleNamespace(page_id="maint-page", url="https://chatgpt.com/c/maint")]

        async def cookies(self):
            return []

    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], OnlineContext())
    ) is True
    resumed = store.load(path)
    resumed_incident = resumed["maintenance"]["incidents"][0]
    assert resumed_incident["state"] == "OPEN"
    assert resumed["maintenance"]["active_incident_id"] == resumed_incident["incident_id"]
    assert resumed["active_hop_id"] == active_hop_id
    assert resumed_incident["request_id"].endswith("-turn1")
    assert resumed_incident["prompt"] == "durable prompt"


def test_maintainers_prompt_contains_complete_operational_snapshot(tmp_path: Path):
    import json

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    task = store.create_task("Recover exact send", requested_team="alpha", task_id="task-snapshot")
    path = Path(task["manifest_path"])

    def block(current):
        hop = next(item for item in current["hops"] if item["hop_id"] == current["active_hop_id"])
        hop.update(
            state="waiting",
            request_id="task-snapshot-hop1",
            conversation_url="https://chatgpt.com/c/exact",
            conversation_generation=3,
            receipt={"user_turn_id": "turn-1", "binding": {"page_id": "page-1"}},
        )
        current["roles"]["PLAN"].update(
            online=False,
            page_id="page-1",
            page_url="https://chatgpt.com/c/exact",
            conversation_generation=3,
        )
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="exact role tab offline",
            repair_links=[{"repair_task_id": "repair-1", "disposition": "CONTINUE_IN_PARALLEL"}],
        )
        store._queue_control(
            current,
            "open_tab",
            role="PLAN",
            reason="operator requested exact reopen",
            origin="operator",
        )
        return current

    task = store.update(path, block)
    incident = ensure_maintenance_incident(task)
    assert incident is not None
    prompt = MaintainerCoordinator(config, store=store)._prompt(
        task,
        incident,
        include_constructor=False,
        tasks=[task],
    )
    snapshot = json.loads(prompt.split("\n\n", 1)[0].split("\n", 1)[1])

    assert snapshot["durable_send_boundary"]["request_id"] == "task-snapshot-hop1"
    assert snapshot["durable_send_boundary"]["receipt"]["user_turn_id"] == "turn-1"
    assert snapshot["roles"]["PLAN"]["page_id"] == "page-1"
    assert snapshot["controls"][-1]["origin"] == "operator"
    assert snapshot["repair_links"][0]["repair_task_id"] == "repair-1"
    assert snapshot["runtime_availability"]["roles_online"]["PLAN"] is False
    assert "version, recovery, repair, lesson" in prompt


def test_v2_ineffective_primitive_queues_next_bounded_recovery_step(tmp_path: Path):
    from datetime import datetime, timezone

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import (
        MaintainerCoordinator,
        MaintenanceDecision,
        MaintenanceStep,
    )
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("recover", requested_team="alpha", task_id="task-steps")
    path = Path(state["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="exact role unavailable",
        )
        return current

    state = store.update(path, block)
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident["turn"] = 1
    incident["request_id"] = f"{incident['incident_id']}-turn1"
    state = store.save_maintenance(path, state)
    coordinator = MaintainerCoordinator(config, store=store)
    decision = MaintenanceDecision(
        action="OPEN_ROLE_TAB",
        reason="reopen",
        recovery=(
            MaintenanceStep("OPEN_ROLE_TAB", "reopen exact tab", "PLAN"),
            MaintenanceStep("RESTART_ROLE", "restart role after ineffective reopen", "PLAN"),
        ),
        version=2,
    )
    committed, active, _evidence, first, stale = coordinator._commit_response(
        path,
        incident_id=incident["incident_id"],
        expected_incident_key=incident["key"],
        request_id=incident["request_id"],
        turn=1,
        report="# Recovery\n\nTry exact reopen, then bounded restart.\n",
        report_at=datetime.now(timezone.utc),
        decision=decision,
    )
    assert stale is False
    assert first["action"] == "open_tab"

    def ineffective(current):
        control = next(item for item in current["controls"] if item["control_id"] == first["control_id"])
        control.update(
            status="ineffective",
            command_state="INEFFECTIVE",
            result="exact conversation unavailable",
        )
        return current

    committed = store.update(path, ineffective)
    assert coordinator._reconcile_active(path, committed) is True
    queued = store.load(path)
    assert [item["action"] for item in queued["controls"]] == ["open_tab", "restart_role"]
    assert queued["controls"][-1]["origin"] == "maintainers"
    assert queued["controls"][-1]["maintenance_request_id"].endswith("-step2")


def test_parse_v2_rejects_repair_creation_inside_recovery_list():
    response = """# Invalid recovery

Repair creation belongs in the dedicated repair object.

```json
{"version":2,"recovery":[{"action":"CREATE_REPAIR_TASK","reason":"Create repair.","role":null}],"repair":null,"lesson":null}
```
"""

    with pytest.raises(ValueError, match="unsupported recovery action"):
        parse_maintenance_response(response)


def test_network_tooling_suspension_resumes_with_same_browser_after_exact_probe_recovers(
    tmp_path: Path,
    monkeypatch,
):
    import asyncio
    import urllib.error

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from playwright_auto.cdpa_store import TaskStore

    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task(
        "network recovery",
        requested_team="alpha",
        task_id="task-network-recovery",
    )
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="network timeout",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident["request_id"] = f"{incident['incident_id']}-turn1"
        incident["prompt"] = "same durable prompt"
        return current

    task = store.update(path, block)
    incident = task["maintenance"]["incidents"][0]
    coordinator = MaintainerCoordinator(config, store=store)

    class StablePage:
        page_id = "maint-page"
        url = "https://chatgpt.com/c/maint"
        healthy = False

    page = StablePage()
    network_calls: list[tuple[str, str, float]] = []

    class HealthyResponse:
        status = 204

        def getcode(self):
            return self.status

        def close(self):
            return None

    def fake_urlopen(request, timeout):
        network_calls.append((request.full_url, request.get_method(), timeout))
        if not page.healthy:
            raise urllib.error.URLError("network unavailable")
        return HealthyResponse()

    monkeypatch.setattr(maintenance_module, "_open_no_redirect", fake_urlopen)

    class StableContext:
        pages = [page]

    global_state = coordinator.state_store.load()
    for _ in range(3):
        assert coordinator._record_environment_failure(
            path,
            incident_id=incident["incident_id"],
            error=TimeoutError("network timed out"),
            browser_context=StableContext(),
            global_state=global_state,
        ) is True

    suspended = store.load(path)
    suspended_incident = suspended["maintenance"]["incidents"][0]
    assert suspended_incident["state"] == "SUSPENDED"
    assert suspended_incident["environment_signature"]["available"] is False
    before_pages = suspended_incident["environment_signature"]["browser_pages"]

    page.healthy = True
    assert asyncio.run(
        coordinator._resume_suspended_environments(
            [(path, suspended)], StableContext()
        )
    ) is True
    resumed = store.load(path)
    resumed_incident = resumed["maintenance"]["incidents"][0]
    assert resumed_incident["state"] == "OPEN"
    assert resumed_incident["environment_resume_signature"]["available"] is True
    assert resumed_incident["environment_resume_signature"]["browser_pages"] == before_pages
    assert resumed_incident["environment_resume_signature"]["health_probe"] == (
        "network_exact_endpoint_head"
    )
    assert network_calls
    assert all(method == "HEAD" and timeout == 2.5 for _url, method, timeout in network_calls)


def test_filesystem_environment_probe_requires_real_write_fsync_and_delete(
    tmp_path: Path,
    monkeypatch,
):
    import errno
    import os

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    coordinator = MaintainerCoordinator(config)
    context = type("Context", (), {"pages": []})()
    original_open = os.open

    def no_space(path, flags, mode=0o777, *, dir_fd=None):
        if str(path).endswith(".cdpa-maintainers-filesystem-probe"):
            raise OSError(errno.ENOSPC, "No space left on device")
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", no_space)
        failed = coordinator._environment_signature(context, "filesystem")

    recovered = coordinator._environment_signature(context, "filesystem")

    assert failed["available"] is False
    assert failed["health_probe"] == "filesystem_write_fsync_delete"
    assert "No space left" in str(failed["filesystem_error"])
    assert recovered["available"] is True
    assert recovered["filesystem_write_fsync_delete"] is True
    assert not (config.plans_root / ".cdpa-maintainers-filesystem-probe").exists()


def test_environment_prerequisite_distinguishes_network_connection_from_cdp():
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator

    assert MaintainerCoordinator._environment_prerequisite(
        ConnectionError("network unavailable")
    ) == "network"
    assert MaintainerCoordinator._environment_prerequisite(
        ConnectionError("CDP unavailable")
    ) == "browser_cdp"


def test_filesystem_suspension_resumes_after_real_probe_write_recovers(
    tmp_path: Path,
    monkeypatch,
):
    import asyncio
    import errno
    import os

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task(
        "filesystem recovery",
        requested_team="alpha",
        task_id="task-filesystem-recovery",
    )
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="No space left on device",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        return current

    task = store.update(path, block)
    incident = task["maintenance"]["incidents"][0]
    coordinator = MaintainerCoordinator(config, store=store)
    context = type("Context", (), {"pages": []})()
    original_open = os.open

    def no_space(probe_path, flags, mode=0o777, *, dir_fd=None):
        if str(probe_path).endswith(".cdpa-maintainers-filesystem-probe"):
            raise OSError(errno.ENOSPC, "No space left on device")
        if dir_fd is None:
            return original_open(probe_path, flags, mode)
        return original_open(probe_path, flags, mode, dir_fd=dir_fd)

    global_state = coordinator.state_store.load()
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", no_space)
        for _ in range(3):
            coordinator._record_environment_failure(
                path,
                incident_id=incident["incident_id"],
                error=OSError(errno.ENOSPC, "No space left on device"),
                browser_context=context,
                global_state=global_state,
            )

    suspended = store.load(path)
    assert suspended["maintenance"]["incidents"][0]["state"] == "SUSPENDED"
    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], context)
    ) is True
    resumed = store.load(path)
    resumed_incident = resumed["maintenance"]["incidents"][0]
    assert resumed_incident["state"] == "OPEN"
    assert resumed_incident["environment_resume_signature"][
        "filesystem_write_fsync_delete"
    ] is True


def test_cached_pages_do_not_resume_disconnected_cdp_incident(tmp_path: Path):
    import asyncio
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator, ensure_maintenance_incident
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task("cached CDP", requested_team="alpha", task_id="task-cached-cdp")
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="CDP unavailable",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident.update(
            state="SUSPENDED",
            environment_attempts=3,
            environment_prerequisite="browser_cdp",
            environment_last_error="ConnectionError: CDP unavailable",
            environment_signature={"available": False, "prerequisite": "browser_cdp"},
        )
        current["maintenance"]["active_incident_id"] = None
        return current

    suspended = store.update(path, block)
    coordinator = MaintainerCoordinator(config, store=store)

    class Browser:
        def is_connected(self):
            return False

    class CachedContext:
        browser = Browser()
        pages = [SimpleNamespace(page_id="cached", url="https://chatgpt.com/c/cached")]

        async def cookies(self):
            raise AssertionError("live probe must not run after disconnected state")

    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], CachedContext())
    ) is False
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert incident["state"] == "SUSPENDED"
    assert current["maintenance"]["active_incident_id"] is None


def test_live_cdp_probe_resumes_same_suspended_incident(tmp_path: Path):
    import asyncio
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator, ensure_maintenance_incident
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task("live CDP", requested_team="alpha", task_id="task-live-cdp")
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="CDP unavailable",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident.update(
            state="SUSPENDED",
            environment_attempts=3,
            environment_prerequisite="browser_cdp",
            environment_last_error="ConnectionError: CDP unavailable",
            environment_signature={"available": False, "prerequisite": "browser_cdp"},
        )
        current["maintenance"]["active_incident_id"] = None
        return current

    suspended = store.update(path, block)
    coordinator = MaintainerCoordinator(config, store=store)
    calls = []

    class Browser:
        def is_connected(self):
            return True

    class LiveContext:
        browser = Browser()
        pages = [SimpleNamespace(page_id="live", url="https://chatgpt.com/c/live")]

        async def cookies(self):
            calls.append("cookies")
            return []

    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], LiveContext())
    ) is True
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert calls == ["cookies"]
    assert incident["state"] == "OPEN"
    assert incident["environment_resume_signature"]["available"] is True
    assert incident["environment_resume_signature"]["browser_live_operation"] == "context.cookies"


def test_chatgpt_head_does_not_resume_tooling_incident(tmp_path: Path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator, ensure_maintenance_incident
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task("tooling", requested_team="alpha", task_id="task-tooling")
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="MCP tooling unavailable",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident.update(
            state="SUSPENDED",
            environment_attempts=3,
            environment_prerequisite="tooling",
            environment_last_error="RuntimeError: MCP tooling unavailable",
            environment_signature={"available": False, "prerequisite": "tooling"},
        )
        current["maintenance"]["active_incident_id"] = None
        return current

    suspended = store.update(path, block)
    coordinator = MaintainerCoordinator(config, store=store)

    class Response:
        status = 200
        def getcode(self): return 200
        def close(self): return None

    monkeypatch.setattr(maintenance_module.urllib.request, "urlopen", lambda *_a, **_k: Response())
    context = SimpleNamespace(
        pages=[SimpleNamespace(page_id="page", url="https://chatgpt.com/c/tooling")]
    )

    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], context)
    ) is False
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert incident["state"] == "SUSPENDED"
    assert current["maintenance"]["active_incident_id"] is None


def test_non_cdp_connection_refused_is_not_classified_as_browser_cdp():
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator

    assert MaintainerCoordinator._environment_prerequisite(
        ConnectionRefusedError("connection refused to MCP endpoint 127.0.0.1:8101")
    ) == "tooling"
    assert MaintainerCoordinator._environment_prerequisite(
        ConnectionRefusedError("connection refused to https://example.invalid/api")
    ) == "network"
    assert MaintainerCoordinator._environment_prerequisite(
        RuntimeError("tooling timeout while listing capabilities")
    ) == "tooling"


def test_tooling_failure_records_exact_dependency_and_never_uses_http_as_recovery(
    tmp_path: Path,
    monkeypatch,
):
    import asyncio
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator, ensure_maintenance_incident
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task("tooling evidence", requested_team="alpha", task_id="task-tooling-evidence")
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="MCP endpoint unavailable",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        return current

    task = store.update(path, block)
    incident = task["maintenance"]["incidents"][0]
    coordinator = MaintainerCoordinator(config, store=store)
    context = SimpleNamespace(
        pages=[SimpleNamespace(page_id="page", url="https://chatgpt.com/c/tooling")]
    )

    class Response:
        status = 200
        def getcode(self): return 200
        def close(self): return None

    http_calls = []
    monkeypatch.setattr(
        maintenance_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: http_calls.append((args, kwargs)) or Response(),
    )

    error = ConnectionRefusedError(
        "connection refused to MCP endpoint 127.0.0.1:8101"
    )
    global_state = coordinator.state_store.load()
    for _ in range(3):
        assert coordinator._record_environment_failure(
            path,
            incident_id=incident["incident_id"],
            error=error,
            browser_context=context,
            global_state=global_state,
        ) is True

    suspended = store.load(path)
    suspended_incident = suspended["maintenance"]["incidents"][0]
    signature = suspended_incident["environment_signature"]
    assert suspended_incident["state"] == "SUSPENDED"
    assert suspended_incident["environment_prerequisite"] == "tooling"
    assert signature["tooling_probe_supported"] is False
    assert signature["tooling_dependency_identity"].casefold() == "mcp"
    assert signature["tooling_endpoint"] == "127.0.0.1:8101"
    assert signature["available"] is False
    assert http_calls == []

    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], context)
    ) is False
    assert http_calls == []
    assert store.load(path)["maintenance"]["incidents"][0]["state"] == "SUSPENDED"


def test_network_probe_prefers_exact_failure_endpoint(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    coordinator = MaintainerCoordinator(config)
    requested = []

    class Response:
        status = 204
        def getcode(self): return 204
        def close(self): return None

    def fake_urlopen(request, timeout):
        requested.append((request.full_url, request.get_method(), timeout))
        return Response()

    monkeypatch.setattr(maintenance_module, "_open_no_redirect", fake_urlopen)
    context = SimpleNamespace(
        pages=[SimpleNamespace(page_id="chatgpt", url="https://chatgpt.com/c/example")]
    )
    signature = coordinator._environment_signature(
        context,
        "network",
        failure_detail="ConnectionRefusedError: https://example.invalid/api refused",
    )

    assert signature["available"] is True
    assert signature["network_evidence"]["endpoint"] == "https://example.invalid/api"
    assert requested == [("https://example.invalid/api", "HEAD", 2.5)]


def test_exact_mcp_tools_list_probe_resumes_same_suspended_incident(
    tmp_path: Path,
):
    import asyncio
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import (
        MaintainerCoordinator,
        ToolingProbeDescriptor,
        ToolingUnavailableError,
        ensure_maintenance_incident,
    )
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task(
        "exact MCP recovery",
        requested_team="alpha",
        task_id="task-exact-mcp-recovery",
    )
    path = Path(task["manifest_path"])

    def block(current):
        hop = next(
            item
            for item in current["hops"]
            if item["hop_id"] == current["active_hop_id"]
        )
        hop.update(
            state="waiting",
            request_id="task-exact-mcp-recovery-hop1",
            conversation_url="https://chatgpt.com/c/exact-mcp",
            conversation_generation=2,
            receipt={
                "user_turn_id": "tooling-turn-1",
                "binding": {"page_id": "tooling-page-1"},
            },
        )
        current["roles"]["PLAN"].update(
            online=False,
            page_id="tooling-page-1",
            page_url="https://chatgpt.com/c/exact-mcp",
            conversation_generation=2,
        )
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="mcp-g8 tools/list unavailable",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident["request_id"] = f"{incident['incident_id']}-turn1"
        incident["prompt"] = "same tooling request"
        return current

    task = store.update(path, block)
    incident = task["maintenance"]["incidents"][0]
    coordinator = MaintainerCoordinator(config, store=store)
    context = SimpleNamespace(pages=[])

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/mcp"
    descriptor = ToolingProbeDescriptor.mcp_tools_list(
        dependency="mcp-g8",
        endpoint=endpoint,
    )
    error = ToolingUnavailableError(
        "mcp-g8 tools/list unavailable",
        probe=descriptor,
    )
    global_state = coordinator.state_store.load()
    for _ in range(3):
        assert coordinator._record_environment_failure(
            path,
            incident_id=incident["incident_id"],
            error=error,
            browser_context=context,
            global_state=global_state,
        ) is True

    suspended = store.load(path)
    suspended_incident = suspended["maintenance"]["incidents"][0]
    before = {
        "task_id": suspended["task_id"],
        "team": suspended["team"],
        "active_hop_id": suspended["active_hop_id"],
        "active_role": suspended["active_role"],
        "hop": next(
            item
            for item in suspended["hops"]
            if item["hop_id"] == suspended["active_hop_id"]
        ),
        "request_id": suspended_incident["request_id"],
        "prompt": suspended_incident["prompt"],
    }
    assert suspended_incident["state"] == "SUSPENDED"
    assert suspended_incident["environment_probe_descriptor"] == descriptor.to_dict()
    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], context)
    ) is False
    failed_probe_state = store.load(path)
    failed_probe_incident = failed_probe_state["maintenance"]["incidents"][0]
    failed_signature = failed_probe_incident["environment_last_probe_signature"]
    assert failed_signature["available"] is False
    assert failed_signature["tooling_probe_supported"] is True
    assert failed_signature["tooling_capability_succeeded"] is False
    assert failed_signature["tooling_evidence"]["executed"] is True
    assert failed_signature["tooling_evidence"]["error"]

    calls: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            calls.append(payload)
            if payload.get("method") == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stateless-fixture", "version": "1"},
                }
            elif payload.get("method") == "tools/list":
                result = {"tools": [{"name": "shell_execute"}]}
            else:
                self.send_response(400)
                self.end_headers()
                return
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": result,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert asyncio.run(
            coordinator._resume_suspended_environments([(path, suspended)], context)
        ) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    resumed = store.load(path)
    resumed_incident = resumed["maintenance"]["incidents"][0]
    after = {
        "task_id": resumed["task_id"],
        "team": resumed["team"],
        "active_hop_id": resumed["active_hop_id"],
        "active_role": resumed["active_role"],
        "hop": next(
            item
            for item in resumed["hops"]
            if item["hop_id"] == resumed["active_hop_id"]
        ),
        "request_id": resumed_incident["request_id"],
        "prompt": resumed_incident["prompt"],
    }
    assert after == before
    assert resumed_incident["state"] == "OPEN"
    assert resumed_incident["environment_resume_signature"]["available"] is True
    assert resumed_incident["environment_resume_signature"]["tooling_probe_supported"] is True
    assert resumed_incident["environment_resume_signature"]["tooling_capability_succeeded"] is True
    assert resumed_incident["environment_resume_signature"]["tooling_tool_count"] == 1
    assert [item["method"] for item in calls] == ["initialize", "tools/list"]
    assert calls[0]["params"]["protocolVersion"] == "2025-03-26"
    assert calls[1]["params"] == {}


def test_mcp_endpoint_without_tools_list_capability_remains_suspended(tmp_path: Path):
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import (
        MaintainerCoordinator,
        ToolingProbeDescriptor,
        ensure_maintenance_incident,
    )
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], "result": {"healthy": True}}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
        (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
        store = TaskStore(config)
        task = store.create_task("bad MCP capability", requested_team="alpha", task_id="task-bad-mcp")
        path = Path(task["manifest_path"])

        def block(current):
            current.update(
                status="BLOCKED",
                kanban_column="BLOCKED",
                block_code="unexpected_error",
                block_reason="MCP capability unavailable",
            )
            incident = ensure_maintenance_incident(current)
            assert incident is not None
            incident.update(
                state="SUSPENDED",
                environment_attempts=3,
                environment_prerequisite="tooling",
                environment_last_error="ToolingUnavailableError: MCP capability unavailable",
                environment_probe_descriptor=ToolingProbeDescriptor.mcp_tools_list(
                    dependency="mcp-g8",
                    endpoint=endpoint,
                ).to_dict(),
                environment_signature={"available": False, "prerequisite": "tooling"},
            )
            current["maintenance"]["active_incident_id"] = None
            return current

        suspended = store.update(path, block)
        coordinator = MaintainerCoordinator(config, store=store)
        context = SimpleNamespace(pages=[])
        assert asyncio.run(
            coordinator._resume_suspended_environments([(path, suspended)], context)
        ) is False
        current = store.load(path)
        assert current["maintenance"]["incidents"][0]["state"] == "SUSPENDED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_unallowlisted_tooling_probe_descriptor_fails_closed(tmp_path: Path):
    import asyncio
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator, ensure_maintenance_incident
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    task = store.create_task("invalid tooling probe", requested_team="alpha", task_id="task-invalid-tooling")
    path = Path(task["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="tooling unavailable",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident.update(
            state="SUSPENDED",
            environment_attempts=3,
            environment_prerequisite="tooling",
            environment_last_error="RuntimeError: tooling unavailable",
            environment_probe_descriptor={
                "version": 1,
                "kind": "shell_command",
                "dependency": "mcp-g8",
                "endpoint": "http://127.0.0.1:8101/mcp",
                "method": "rm -rf /",
            },
            environment_signature={"available": False, "prerequisite": "tooling"},
        )
        current["maintenance"]["active_incident_id"] = None
        return current

    suspended = store.update(path, block)
    coordinator = MaintainerCoordinator(config, store=store)
    context = SimpleNamespace(pages=[])
    assert asyncio.run(
        coordinator._resume_suspended_environments([(path, suspended)], context)
    ) is False
    current = store.load(path)
    incident = current["maintenance"]["incidents"][0]
    assert incident["state"] == "SUSPENDED"
    assert current["maintenance"]["active_incident_id"] is None



def test_tooling_probe_descriptor_rejects_non_loopback_and_redirectable_shapes():
    import pytest

    from playwright_auto.cdpa_maintenance import ToolingProbeDescriptor

    invalid_endpoints = (
        "https://127.0.0.1:8101/mcp",
        "http://example.com:8101/mcp",
        "http://127.0.0.1/mcp",
        "http://user@127.0.0.1:8101/mcp",
        "http://127.0.0.1:8101/mcp?redirect=https://example.com",
    )
    for endpoint in invalid_endpoints:
        with pytest.raises(ValueError):
            ToolingProbeDescriptor.mcp_tools_list(
                dependency="mcp-g8",
                endpoint=endpoint,
            )


def test_mcp_tools_list_probe_does_not_follow_redirects(tmp_path: Path):
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import (
        MaintainerCoordinator,
        ToolingProbeDescriptor,
        ensure_maintenance_incident,
    )
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://example.com:80/tools")
            self.end_headers()

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
        (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
        store = TaskStore(config)
        task = store.create_task("redirect tooling", requested_team="alpha", task_id="task-tooling-redirect")
        path = Path(task["manifest_path"])

        def block(current):
            current.update(
                status="BLOCKED",
                kanban_column="BLOCKED",
                block_code="unexpected_error",
                block_reason="MCP tools/list unavailable",
            )
            incident = ensure_maintenance_incident(current)
            assert incident is not None
            incident.update(
                state="SUSPENDED",
                environment_attempts=3,
                environment_prerequisite="tooling",
                environment_last_error="ToolingUnavailableError: MCP tools/list unavailable",
                environment_probe_descriptor=ToolingProbeDescriptor.mcp_tools_list(
                    dependency="mcp-g8",
                    endpoint=endpoint,
                ).to_dict(),
                environment_signature={"available": False, "prerequisite": "tooling"},
            )
            current["maintenance"]["active_incident_id"] = None
            return current

        suspended = store.update(path, block)
        coordinator = MaintainerCoordinator(config, store=store)
        assert asyncio.run(
            coordinator._resume_suspended_environments(
                [(path, suspended)],
                SimpleNamespace(pages=[]),
            )
        ) is False
        current = store.load(path)
        incident = current["maintenance"]["incidents"][0]
        signature = incident["environment_last_probe_signature"]
        assert incident["state"] == "SUSPENDED"
        assert signature["tooling_capability_succeeded"] is False
        assert "MCP initialize HTTP 302" in signature["tooling_error"]
        assert calls == ["/mcp"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_production_maintenance_preflight_suspends_and_resumes_via_authenticated_stateful_mcp(
    tmp_path: Path,
    monkeypatch,
):
    import asyncio
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import ensure_maintenance_incident
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    token = "test-only-static-bearer-never-persist"
    monkeypatch.setenv("MCP_BEARER_TOKEN", token)
    monkeypatch.delenv("MCP_AUTH_PASSWORD", raising=False)

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/mcp"

    config_path = write_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["maintenance"]["tooling_probe"] = {
        "dependency": "mcp-g8",
        "endpoint": endpoint,
        "auth_profile": "local_mcp_static_bearer",
        "required_tools": ["shell_execute"],
    }
    config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    config = load_cdpa_config(config_path, repository_root=tmp_path)
    store = TaskStore(config)
    task = store.create_task(
        "production tooling preflight",
        requested_team="alpha",
        task_id="task-production-tooling-preflight",
    )
    path = Path(task["manifest_path"])

    def block(current):
        hop = next(
            item
            for item in current["hops"]
            if item["hop_id"] == current["active_hop_id"]
        )
        hop.update(
            state="waiting",
            request_id="task-production-tooling-preflight-hop1",
            receipt={
                "user_turn_id": "accepted-turn-1",
                "binding": {"page_id": "accepted-page-1"},
            },
        )
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="exact role tab offline",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        incident["request_id"] = "maint-production-tooling-turn1"
        incident["prompt"] = "same durable maintenance prompt"
        return current

    task = store.update(path, block)
    incident = task["maintenance"]["incidents"][0]
    original = {
        "task_id": task["task_id"],
        "team": task["team"],
        "active_hop_id": task["active_hop_id"],
        "active_role": task["active_role"],
        "hop": next(
            item
            for item in task["hops"]
            if item["hop_id"] == task["active_hop_id"]
        ),
        "maintenance_request_id": incident["request_id"],
        "prompt": incident["prompt"],
    }

    browser_calls = []

    class NoBrowserWork:
        def __init__(self, *_args, **_kwargs):
            pass

        async def acquire_global_role(self, *_args, **_kwargs):
            browser_calls.append("acquire")
            raise AssertionError("tooling preflight must run before browser work")

    monkeypatch.setattr(maintenance_module, "CDPATabActions", NoBrowserWork)
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    context = SimpleNamespace(pages=[])

    for _ in range(3):
        assert asyncio.run(coordinator.advance([(path, store.load(path))], context)) is True

    suspended = store.load(path)
    suspended_incident = suspended["maintenance"]["incidents"][0]
    assert suspended_incident["state"] == "SUSPENDED"
    assert suspended_incident["environment_prerequisite"] == "tooling"
    descriptor = suspended_incident["environment_probe_descriptor"]
    assert descriptor["dependency"] == "mcp-g8"
    assert descriptor["endpoint"] == endpoint
    assert descriptor["auth_profile"] == "local_mcp_static_bearer"
    assert descriptor["required_tools"] == ["shell_execute"]
    assert token not in path.read_text(encoding="utf-8")
    assert all(
        token not in candidate.read_text(encoding="utf-8")
        for candidate in config.plans_root.rglob("*.json")
    )
    assert browser_calls == []

    session_id = "stateful-test-session"
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self):
            return self.headers.get("Authorization") == f"Bearer {token}"

        def _json(self, status, payload, *, session=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if session:
                self.send_header("MCP-Session-Id", session)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            calls.append(
                {
                    "http_method": "POST",
                    "rpc_method": payload.get("method"),
                    "session": self.headers.get("MCP-Session-Id"),
                    "accept": self.headers.get("Accept"),
                    "protocol": self.headers.get("MCP-Protocol-Version"),
                    "authorized": self._authorized(),
                }
            )
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            method = payload.get("method")
            if method == "initialize":
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "stateful-fixture", "version": "1"},
                        },
                    },
                    session=session_id,
                )
                return
            if self.headers.get("MCP-Session-Id") != session_id:
                self._json(400, {"error": "missing session"})
                return
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "tools/list":
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"tools": [{"name": "shell_execute"}]},
                    },
                )
                return
            self._json(400, {"error": "unsupported"})

        def do_DELETE(self):
            calls.append(
                {
                    "http_method": "DELETE",
                    "rpc_method": None,
                    "session": self.headers.get("MCP-Session-Id"),
                    "accept": self.headers.get("Accept"),
                    "protocol": self.headers.get("MCP-Protocol-Version"),
                    "authorized": self._authorized(),
                }
            )
            if not self._authorized() or self.headers.get("MCP-Session-Id") != session_id:
                self._json(400, {"error": "bad delete"})
                return
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert asyncio.run(
            coordinator.advance([(path, suspended)], context)
        ) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    resumed = store.load(path)
    resumed_incident = resumed["maintenance"]["incidents"][0]
    current = {
        "task_id": resumed["task_id"],
        "team": resumed["team"],
        "active_hop_id": resumed["active_hop_id"],
        "active_role": resumed["active_role"],
        "hop": next(
            item
            for item in resumed["hops"]
            if item["hop_id"] == resumed["active_hop_id"]
        ),
        "maintenance_request_id": resumed_incident["request_id"],
        "prompt": resumed_incident["prompt"],
    }
    assert current == original
    assert resumed_incident["state"] == "OPEN"
    signature = resumed_incident["environment_resume_signature"]
    assert signature["available"] is True
    assert signature["tooling_capability_succeeded"] is True
    assert signature["tooling_required_tools"] == ["shell_execute"]
    assert signature["tooling_matched_tools"] == ["shell_execute"]
    assert signature["tooling_missing_tools"] == []
    assert signature["tooling_session_mode"] == "stateful"
    assert signature["tooling_session_id_sha256"]
    assert signature["tooling_cleanup_attempted"] is True
    assert signature["tooling_cleanup_succeeded"] is True
    assert browser_calls == []
    assert [item["rpc_method"] for item in calls if item["http_method"] == "POST"] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert calls[-1]["http_method"] == "DELETE"
    assert all(item["authorized"] is True for item in calls)
    assert all(item["accept"] == "application/json, text/event-stream" for item in calls)
    assert all(item["protocol"] == "2025-03-26" for item in calls)
    assert token not in json.dumps(signature)
    assert all(
        token not in candidate.read_text(encoding="utf-8")
        for candidate in config.plans_root.rglob("*.json")
    )


def test_mcp_sse_parser_selects_matching_event_without_combining_messages():
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator

    raw = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":"other","result":{"tools":[]}}\n\n'
        b": keepalive\n\n"
        b"event: message\r\n"
        b'data: {"jsonrpc":"2.0","id":"wanted",\r\n'
        b'data: "result":{"tools":[{"name":"shell_execute"}]}}\r\n\r\n'
    )

    result = MaintainerCoordinator._decode_mcp_response(raw, expected_id="wanted")

    assert result["id"] == "wanted"
    assert result["result"]["tools"] == [{"name": "shell_execute"}]


def test_stateful_mcp_probe_requires_successful_session_cleanup(tmp_path: Path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from playwright_auto.cdpa_maintenance import (
        MaintainerCoordinator,
        ToolingProbeDescriptor,
    )

    session_id = "cleanup-failure-session"

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, payload, *, session=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if session:
                self.send_header("MCP-Session-Id", session)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if payload.get("method") == "initialize":
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "cleanup-fixture", "version": "1"},
                        },
                    },
                    session=session_id,
                )
            elif payload.get("method") == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
            elif payload.get("method") == "tools/list":
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"tools": [{"name": "shell_execute"}]},
                    },
                )
            else:
                self._json(400, {"error": "unsupported"})

        def do_DELETE(self):
            self._json(500, {"error": "cleanup failed"})

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        descriptor = ToolingProbeDescriptor.mcp_tools_list(
            dependency="mcp-g8",
            endpoint=f"http://127.0.0.1:{server.server_port}/mcp",
        )
        result = MaintainerCoordinator._tooling_environment_probe(
            "stateful cleanup acceptance",
            descriptor.to_dict(),
            execute=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["available"] is False
    assert result["capability_succeeded"] is False
    assert result["cleanup_attempted"] is True
    assert result["cleanup_succeeded"] is False
    assert "session cleanup failed" in result["error"]


@pytest.mark.parametrize(
    ("initialized_content_type", "initialized_body", "expected_error"),
    [
        (
            "application/json",
            b'{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"initialized rejected"}}',
            "JSON-RPC error",
        ),
        (
            "application/json",
            b"{malformed-json",
            "JSONDecodeError",
        ),
        (
            "application/json",
            b'{"jsonrpc":"2.0","id":null,"result":{}}',
            "unexpected non-empty response",
        ),
        (
            "text/event-stream",
            b'data: {"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"initialized rejected"}}\n\n',
            "JSON-RPC error",
        ),
    ],
)
def test_stateful_mcp_probe_rejects_nonempty_initialized_protocol_body(
    initialized_content_type: str,
    initialized_body: bytes,
    expected_error: str,
):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from playwright_auto.cdpa_maintenance import (
        MaintainerCoordinator,
        ToolingProbeDescriptor,
    )

    session_id = "initialized-rejection-session"
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, payload, *, session=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if session:
                self.send_header("MCP-Session-Id", session)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            method = str(payload.get("method") or "")
            calls.append(method)
            if method == "initialize":
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "initialized-fixture", "version": "1"},
                        },
                    },
                    session=session_id,
                )
                return
            if method == "notifications/initialized":
                self.send_response(200)
                self.send_header("Content-Type", initialized_content_type)
                self.send_header("Content-Length", str(len(initialized_body)))
                self.end_headers()
                self.wfile.write(initialized_body)
                return
            if method == "tools/list":
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"tools": [{"name": "shell_execute"}]},
                    },
                )
                return
            self._json(400, {"error": "unsupported"})

        def do_DELETE(self):
            calls.append("DELETE")
            assert self.headers.get("MCP-Session-Id") == session_id
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        descriptor = ToolingProbeDescriptor.mcp_tools_list(
            dependency="mcp-g8",
            endpoint=f"http://127.0.0.1:{server.server_port}/mcp",
        )
        result = MaintainerCoordinator._tooling_environment_probe(
            "initialized protocol rejection",
            descriptor.to_dict(),
            execute=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["available"] is False
    assert result["capability_succeeded"] is False
    assert result["cleanup_attempted"] is True
    assert result["cleanup_succeeded"] is True
    assert expected_error in result["error"]
    assert calls == ["initialize", "notifications/initialized", "DELETE"]
    initialized_step = next(
        item for item in result["evidence"]["steps"] if item["stage"] == "initialized"
    )
    assert initialized_step["status"] == 200
    assert expected_error in initialized_step["error"]


def test_network_probe_records_exact_redirect_without_following_target(tmp_path: Path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from test_cdpa_core import write_config

    target_hits: list[str] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            target_hits.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_port}/redirect-target",
            )
            self.end_headers()

        def log_message(self, *_args):
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        endpoint = f"http://127.0.0.1:{redirect.server_port}/exact-endpoint"
        config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
        coordinator = MaintainerCoordinator(config)
        signature = coordinator._environment_signature(
            SimpleNamespace(pages=[]),
            "network",
            failure_detail=f"TimeoutError: {endpoint} timed out",
        )
    finally:
        redirect.shutdown()
        redirect.server_close()
        redirect_thread.join(timeout=5)
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=5)

    assert signature["available"] is True
    assert signature["network_evidence"] == {
        "endpoint": endpoint,
        "method": "HEAD",
        "timeout_seconds": 2.5,
        "status": 302,
        "error": None,
    }
    assert target_hits == []


def test_legacy_repair_action_is_rejected_before_report_control_or_manifest_mutation(
    tmp_path: Path,
):
    import json

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    task = store.create_task(
        "legacy repair rejection",
        requested_team="alpha",
        task_id="task-legacy-repair-rejection",
    )
    path = Path(task["manifest_path"])
    before = path.read_bytes()
    reports_before = tuple(config.plans_root.rglob("*.md"))
    response = """# Legacy repair

This legacy form cannot carry the required repair contract.

```json
{"action":"CREATE_REPAIR_TASK","reason":"Create repair.","role":null,"lesson":null,"replacement":null}
```
"""

    with pytest.raises(ValueError, match="unsupported maintenance action"):
        parse_maintenance_response(response)

    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))["controls"] == []
    assert tuple(config.plans_root.rglob("*.md")) == reports_before


def test_network_failure_credentials_are_redacted_before_manifest_global_prompt_and_report(
    tmp_path: Path,
    monkeypatch,
):
    import json
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from test_cdpa_core import write_config

    secret_query = "review-secret-token-should-not-persist"
    secret_user = "review-basic-user"
    secret_password = "review-basic-password"
    secret_bearer = "review-bearer-secret"
    sensitive_url = (
        f"https://{secret_user}:{secret_password}@api.example.invalid/run"
        f"?access_token={secret_query}&mode=check#review-fragment-secret"
    )
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    state = store.create_task(
        "credential sanitization",
        requested_team="alpha",
        task_id="task-credential-sanitization",
    )
    path = Path(state["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="network timeout",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        return current

    state = store.update(path, block)
    incident = state["maintenance"]["incidents"][0]
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    network_calls: list[str] = []

    def forbidden_probe(request, timeout):
        network_calls.append(request.full_url)
        raise AssertionError("credential-bearing endpoint must not be probed without auth profile")

    monkeypatch.setattr(maintenance_module, "_open_no_redirect", forbidden_probe)
    global_state = coordinator.state_store.load()
    error = TimeoutError(
        f"GET {sensitive_url} Authorization: Bearer {secret_bearer} timed out"
    )

    assert coordinator._record_environment_failure(
        path,
        incident_id=incident["incident_id"],
        error=error,
        browser_context=SimpleNamespace(pages=[]),
        global_state=global_state,
    ) is True

    current = store.load(path)
    current_incident = current["maintenance"]["incidents"][0]
    prompt = coordinator._prompt(
        current,
        current_incident,
        include_constructor=False,
        tasks=[current],
    )
    store.update_maintenance(
        path,
        lambda value: (
            value["maintenance"]["incidents"][0].update(prompt=prompt) or value
        ),
    )
    evidence = write_maintenance_report(
        tmp_path,
        team="alpha",
        turn=1,
        report=(
            "# Sanitized report\n\n"
            f"Observed {sensitive_url} Authorization: Bearer {secret_bearer}.\n"
        ),
        at=datetime(2026, 7, 25, 5, 0, 0, tzinfo=timezone.utc),
    )

    durable_text = "\n".join(
        candidate.read_text(encoding="utf-8", errors="replace")
        for candidate in config.plans_root.rglob("*")
        if candidate.is_file() and candidate.stat().st_size < 2_000_000
    )
    global_text = coordinator.state_store.path.read_text(encoding="utf-8")
    report_text = Path(evidence.path).read_text(encoding="utf-8")
    serialized_incident = json.dumps(store.load(path)["maintenance"], ensure_ascii=False)
    for secret in (
        secret_query,
        secret_user,
        secret_password,
        secret_bearer,
        "review-fragment-secret",
    ):
        assert secret not in durable_text
        assert secret not in global_text
        assert secret not in prompt
        assert secret not in report_text
        assert secret not in serialized_incident
    assert network_calls == []
    assert "[REDACTED]" in serialized_incident
    assert "[REDACTED]" in report_text
    assert current_incident["environment_signature"]["network_evidence"] is None
    assert current_incident["environment_signature"]["network_available"] is False


def test_prompt_projects_allowlisted_maintenance_summary_without_raw_prompt_or_evidence(
    tmp_path: Path,
):
    import json

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_maintenance import MaintainerCoordinator
    from test_cdpa_core import write_config

    secret = "review-secret-prompt-token"
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    coordinator = MaintainerCoordinator(config)
    incident = {
        "incident_id": "maint-secret",
        "state": "SUSPENDED",
        "turn": 3,
        "trigger_status": "BLOCKED",
        "trigger_code": "unexpected_error",
        "trigger_reason": f"failed token={secret}",
        "source_hop_id": 7,
        "source_role": "DEV",
        "created_at": "2026-07-25T05:00:00+00:00",
        "updated_at": "2026-07-25T05:01:00+00:00",
        "environment_attempts": 3,
        "environment_prerequisite": "network",
        "environment_last_error": f"TimeoutError: access_token={secret}",
        "environment_signature": {
            "version": 3,
            "prerequisite": "network",
            "available": False,
            "network_available": False,
            "network_evidence": {
                "endpoint": f"https://api.invalid/?access_token={secret}",
                "method": "HEAD",
                "status": None,
                "error": f"token={secret}",
            },
        },
        "environment_evidence": [{"raw": secret}],
        "prompt": f"full maintainer prompt with {secret}",
        "request_id": "maint-secret-turn3",
        "last_error": f"Bearer {secret}",
    }
    task = {
        "task_id": "task-secret",
        "team": "alpha",
        "task_text": "safe task",
        "repository": str(tmp_path),
        "status": "BLOCKED",
        "block_code": "unexpected_error",
        "block_reason": "safe block",
        "active_hop_id": None,
        "active_role": None,
        "roles": {},
        "hops": [],
        "controls": [],
        "errors": [],
        "route_timeline": [],
        "dependency_events": [],
        "maintenance": {"active_incident_id": "maint-secret", "incidents": [incident]},
    }

    prompt = coordinator._prompt(task, incident, include_constructor=False, tasks=[task])
    payload = json.loads(prompt.split("CDPA_MAINTENANCE_INCIDENT\n", 1)[1].split("\n\nReturn", 1)[0])

    assert secret not in prompt
    summary = payload["recent_maintenance_incidents"][0]
    assert "prompt" not in summary
    assert "environment_evidence" not in summary
    assert "environment_probe_checks" not in summary
    assert summary["environment_last_error"] == "TimeoutError: access_token=[REDACTED]"
    assert summary["environment_signature"]["network_evidence"]["endpoint"].endswith(
        "access_token=%5BREDACTED%5D"
    )


@pytest.mark.parametrize(
    ("repair_override", "message"),
    [
        ({"root_cause": "r" * 1201}, "root cause"),
        ({"reason": "q" * 1201}, "reason"),
        ({"reproduction": "p" * 2401}, "reproduction"),
        ({"required_tests": [f"test-{index}" for index in range(17)]}, "required tests"),
        ({"required_tests": ["t" * 301]}, "required test"),
        (
            {
                "source_areas": [
                    "cdpa_worker",
                    "cdpa_store",
                    "cdpa_maintenance",
                    "cdpa_actions",
                    "dashboard",
                    "dependencies",
                    "queue",
                    "transport",
                    "tests",
                ]
            },
            "source areas",
        ),
    ],
)
def test_parse_v2_repair_uses_canonical_worker_bounds(
    repair_override: dict[str, object],
    message: str,
):
    import json

    repair = {
        "root_cause": "bounded root cause",
        "reason": "bounded reason",
        "disposition": "HOLD_FOR_REPAIR",
        "reproduction": "bounded reproduction",
        "source_areas": ["cdpa_worker", "tests"],
        "required_tests": ["focused regression"],
    }
    repair.update(repair_override)
    response = (
        "# Bounded repair\n\n"
        "```json\n"
        + json.dumps(
            {
                "version": 2,
                "recovery": [],
                "repair": repair,
                "lesson": None,
            }
        )
        + "\n```"
    )

    with pytest.raises(ValueError, match=message):
        parse_maintenance_response(response)


def _strict_v2_repair_response(repair_override: dict[str, object]) -> str:
    import json

    repair: dict[str, object] = {
        "root_cause": "strict root cause",
        "reason": "strict reason",
        "disposition": "CONTINUE_IN_PARALLEL",
        "reproduction": "strict reproduction",
        "source_areas": ["tests"],
        "required_tests": ["focused regression"],
    }
    repair.update(repair_override)
    return (
        "# Strict repair\n\n```json\n"
        + json.dumps(
            {
                "version": 2,
                "recovery": [],
                "repair": repair,
                "lesson": None,
            }
        )
        + "\n```"
    )


@pytest.mark.parametrize(
    ("repair_override", "message"),
    [
        ({"required_tests": ["same"] * 17}, "required tests may contain at most 16 raw entries"),
        ({"source_areas": ["tests"] * 9}, "source areas may contain at most 8 raw entries"),
        ({"required_tests": ["same", "same"]}, "required tests must not contain duplicates"),
        ({"source_areas": ["tests", "tests"]}, "source areas must not contain duplicates"),
        ({"required_tests": [" same ", "same"]}, "required tests must not normalize to duplicates"),
        ({"source_areas": [" tests ", "tests"]}, "source areas must not normalize to duplicates"),
    ],
)
def test_parse_v2_repair_rejects_raw_count_and_duplicate_bypass(
    repair_override: dict[str, object],
    message: str,
):
    with pytest.raises(ValueError, match=message):
        parse_maintenance_response(_strict_v2_repair_response(repair_override))


@pytest.mark.parametrize("field", ["root_cause", "reason", "disposition", "reproduction"])
@pytest.mark.parametrize("value", [123, True, {"bad": "type"}, None])
def test_parse_v2_repair_rejects_non_string_required_fields(field: str, value: object):
    with pytest.raises(ValueError, match="string"):
        parse_maintenance_response(_strict_v2_repair_response({field: value}))


@pytest.mark.parametrize("field", ["source_areas", "required_tests"])
@pytest.mark.parametrize("value", [123, True, {"bad": "type"}, None])
def test_parse_v2_repair_rejects_non_list_collections(field: str, value: object):
    with pytest.raises(ValueError, match="list"):
        parse_maintenance_response(_strict_v2_repair_response({field: value}))


@pytest.mark.parametrize("field", ["source_areas", "required_tests"])
@pytest.mark.parametrize("value", [123, True, {"bad": "type"}, None])
def test_parse_v2_repair_rejects_non_string_collection_items(field: str, value: object):
    message = "source area item" if field == "source_areas" else "required test item"
    with pytest.raises(ValueError, match=message):
        parse_maintenance_response(_strict_v2_repair_response({field: [value]}))


def test_path_embedded_credentials_are_redacted_and_never_probed_across_maintenance_surfaces(
    tmp_path: Path,
    monkeypatch,
):
    import json
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_safety import (
        extract_probeable_url,
        probeable_url,
        sanitize_text,
        sanitize_url,
    )
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.dashboard import build_task_payload
    from test_cdpa_core import write_config

    secret = "review-webhook-path-secret-token"
    sensitive_url = f"https://api.example.invalid/webhooks/{secret}/status"
    sanitized_url = sanitize_url(sensitive_url)

    assert sanitized_url is not None
    assert secret not in sanitized_url
    assert "[REDACTED]" in sanitized_url
    assert secret not in sanitize_text(f"GET {sensitive_url} failed")
    assert probeable_url(sensitive_url) is None
    assert extract_probeable_url(f"GET {sensitive_url} failed") is None

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    state = store.create_task(
        "path credential sanitization",
        requested_team="alpha",
        task_id="task-path-credential-sanitization",
    )
    path = Path(state["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="network timeout",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        return current

    state = store.update(path, block)
    incident = state["maintenance"]["incidents"][0]
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    probe_calls: list[str] = []

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def record_probe(request, timeout):
        probe_calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(maintenance_module, "_open_no_redirect", record_probe)
    global_state = coordinator.state_store.load()
    error = TimeoutError(f"GET {sensitive_url} timed out")

    assert coordinator._record_environment_failure(
        path,
        incident_id=incident["incident_id"],
        error=error,
        browser_context=SimpleNamespace(pages=[]),
        global_state=global_state,
    ) is True

    current = store.load(path)
    current_incident = current["maintenance"]["incidents"][0]
    prompt = coordinator._prompt(
        current,
        current_incident,
        include_constructor=False,
        tasks=[current],
    )
    store.update_maintenance(
        path,
        lambda value: (
            value["maintenance"]["incidents"][0].update(prompt=prompt) or value
        ),
    )
    evidence = write_maintenance_report(
        tmp_path,
        team="alpha",
        turn=1,
        report=f"# Path credential report\n\nObserved {sensitive_url}.\n",
        at=datetime(2026, 7, 25, 6, 35, 0, tzinfo=timezone.utc),
    )
    dashboard = build_task_payload(store.load(path), tasks=[store.load(path)])

    surfaces = {
        "manifest": path.read_text(encoding="utf-8"),
        "global": coordinator.state_store.path.read_text(encoding="utf-8"),
        "prompt": prompt,
        "report": Path(evidence.path).read_text(encoding="utf-8"),
        "dashboard": json.dumps(dashboard, ensure_ascii=False),
    }
    assert probe_calls == []
    assert current_incident["environment_signature"]["network_evidence"] is None
    assert current_incident["environment_signature"]["network_available"] is False
    for text in surfaces.values():
        assert secret not in text
        assert "[REDACTED]" in text


@pytest.mark.parametrize(
    "sensitive_url",
    [
        "https://api.example.invalid/webhooks/A9b8C7d6E5f4G3h2J1k0/status",
        "https://api.example.invalid/reset/550e8400-e29b-41d4-a716-446655440000",
        "https://api.example.invalid/capability/opaque-value-12345/run",
        "https://api.example.invalid/signed/opaque-signature-value/result",
        "https://api.example.invalid/run/secret-token-value/status",
        "https://api.example.invalid/run/aaaaaaaa.bbbbbbbb.cccccccc/status",
    ],
)
def test_high_risk_path_credentials_are_redacted_and_unprobeable(sensitive_url: str):
    from playwright_auto.cdpa_safety import probeable_url, sanitize_url

    sanitized = sanitize_url(sensitive_url)
    assert sanitized is not None
    assert sanitized != sensitive_url
    assert "[REDACTED]" in sanitized
    assert probeable_url(sensitive_url) is None


def test_normal_resource_identifier_path_remains_probeable():
    from playwright_auto.cdpa_safety import probeable_url, sanitize_url

    url = "https://api.example.invalid/v1/resources/550e8400-e29b-41d4-a716-446655440000/status"
    assert sanitize_url(url) == url
    assert probeable_url(url) == url


_MULTI_SEGMENT_HIGH_RISK_PATHS = (
    "/webhooks/incoming/{secret}/status",
    "/oauth/callback/{secret}/complete",
    "/password-reset/confirm/{secret}",
    "/capability/v1/{secret}/run",
    "/magic-link/callback/{secret}",
    "/password-reset-link/{secret}",
    "/signed-url/{secret}/result",
)


@pytest.mark.parametrize("path_template", _MULTI_SEGMENT_HIGH_RISK_PATHS)
def test_multi_segment_and_compound_high_risk_paths_are_fully_redacted_and_unprobeable(
    path_template: str,
):
    from playwright_auto.cdpa_safety import (
        extract_probeable_url,
        probeable_url,
        sanitize_text,
        sanitize_url,
    )

    secret = "K7p4Q9Lm3Vx8"
    url = "https://api.example.invalid" + path_template.format(secret=secret)
    sanitized = sanitize_url(url)

    assert sanitized is not None
    assert secret not in sanitized
    assert "[REDACTED]" in sanitized
    assert secret not in sanitize_text(f"GET {url} failed")
    assert probeable_url(url) is None
    assert extract_probeable_url(f"GET {url} failed") is None


@pytest.mark.parametrize("path_template", _MULTI_SEGMENT_HIGH_RISK_PATHS)
def test_multi_segment_high_risk_paths_never_probe_or_persist_on_maintenance_surfaces(
    tmp_path: Path,
    monkeypatch,
    path_template: str,
):
    import json
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.dashboard import build_task_payload
    from test_cdpa_core import write_config

    secret = "K7p4Q9Lm3Vx8"
    sensitive_url = "https://api.example.invalid" + path_template.format(secret=secret)
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    state = store.create_task(
        "multi segment path credential sanitization",
        requested_team="alpha",
        task_id="task-multi-segment-path-credential",
    )
    manifest_path = Path(state["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="network timeout",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        return current

    state = store.update(manifest_path, block)
    incident = state["maintenance"]["incidents"][0]
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    probe_calls: list[str] = []

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def record_probe(request, timeout):
        probe_calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(maintenance_module, "_open_no_redirect", record_probe)
    global_state = coordinator.state_store.load()
    error = TimeoutError(f"GET {sensitive_url} timed out")

    assert coordinator._record_environment_failure(
        manifest_path,
        incident_id=incident["incident_id"],
        error=error,
        browser_context=SimpleNamespace(pages=[]),
        global_state=global_state,
    ) is True

    current = store.load(manifest_path)
    current_incident = current["maintenance"]["incidents"][0]
    prompt = coordinator._prompt(
        current,
        current_incident,
        include_constructor=False,
        tasks=[current],
    )
    store.update_maintenance(
        manifest_path,
        lambda value: (
            value["maintenance"]["incidents"][0].update(prompt=prompt) or value
        ),
    )
    report = write_maintenance_report(
        tmp_path,
        team="alpha",
        turn=1,
        report=f"# Multi-segment path report\n\nObserved {sensitive_url}.\n",
        at=datetime(2026, 7, 25, 7, 15, 0, tzinfo=timezone.utc),
    )
    current = store.load(manifest_path)
    dashboard = build_task_payload(current, tasks=[current])
    surfaces = {
        "manifest": manifest_path.read_text(encoding="utf-8"),
        "global": coordinator.state_store.path.read_text(encoding="utf-8"),
        "prompt": prompt,
        "report": Path(report.path).read_text(encoding="utf-8"),
        "dashboard": json.dumps(dashboard, ensure_ascii=False),
        "timeline": json.dumps(dashboard["timeline"], ensure_ascii=False),
    }

    assert probe_calls == []
    assert current_incident["environment_signature"]["network_evidence"] is None
    assert current_incident["environment_signature"]["network_available"] is False
    for value in surfaces.values():
        assert secret not in value
        assert "[REDACTED]" in value


@pytest.mark.parametrize(
    "path",
    (
        "/magic-link-callback/K7p4Q9Lm3Vx8/complete",
        "/webhook-incoming/K7p4Q9Lm3Vx8/status",
        "/oauth-callback/K7p4Q9Lm3Vx8/complete",
        "/capability-v1/K7p4Q9Lm3Vx8/run",
        "/authorization-callback/K7p4Q9Lm3Vx8/complete",
    ),
)
def test_tokenized_compound_high_risk_marker_starts_fail_closed_tail(path: str):
    from playwright_auto.cdpa_safety import probeable_url, sanitize_url

    url = "https://api.example.invalid" + path
    sanitized = sanitize_url(url)
    assert sanitized is not None
    assert "K7p4Q9Lm3Vx8" not in sanitized
    assert "[REDACTED]" in sanitized
    assert probeable_url(url) is None


def test_bare_high_risk_context_is_unprobeable_even_without_a_tail():
    from playwright_auto.cdpa_safety import probeable_url, sanitize_url

    url = "https://api.example.invalid/signed-url"
    assert sanitize_url(url) == "https://api.example.invalid/[REDACTED]"
    assert probeable_url(url) is None


_DIRECT_MARKER_VALUE_PATHS = (
    "/run/token-{secret}/status",
    "/run/secret_{secret}/status",
    "/run/CREDENTIAL-{secret}/status",
    "/run/signature-{secret}/status",
    "/run/session-{secret}/status",
    "/run/jwt-{secret}/status",
    "/run/api-key-{secret}/status",
    "/run/auth-{secret}/status",
    "/run/authorization-{secret}/status",
    "/run/code-{secret}/status",
    "/run/key-{secret}/status",
    "/run/signed-{secret}/status",
    "/run/token%2D{secret}/status",
)


@pytest.mark.parametrize("path_template", _DIRECT_MARKER_VALUE_PATHS)
def test_direct_marker_value_segment_is_redacted_with_its_tail(path_template: str):
    from playwright_auto.cdpa_safety import (
        extract_probeable_url,
        probeable_url,
        sanitize_text,
        sanitize_url,
    )

    secret = "K7p4Q9Lm3Vx8"
    url = "https://api.example.invalid" + path_template.format(secret=secret)
    sanitized = sanitize_url(url)

    assert sanitized is not None
    assert secret not in sanitized
    assert "[REDACTED]" in sanitized
    assert secret not in sanitize_text(f"GET {url} failed")
    assert probeable_url(url) is None
    assert extract_probeable_url(f"GET {url} failed") is None


@pytest.mark.parametrize("path_template", _DIRECT_MARKER_VALUE_PATHS)
def test_direct_marker_value_never_probes_or_persists_on_maintenance_surfaces(
    tmp_path: Path,
    monkeypatch,
    path_template: str,
):
    import json
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.dashboard import build_task_payload
    from test_cdpa_core import write_config

    secret = "K7p4Q9Lm3Vx8"
    sensitive_url = "https://api.example.invalid" + path_template.format(secret=secret)
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    state = store.create_task(
        "direct marker value credential sanitization",
        requested_team="alpha",
        task_id="task-direct-marker-value-credential",
    )
    manifest_path = Path(state["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="network timeout",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        return current

    state = store.update(manifest_path, block)
    incident = state["maintenance"]["incidents"][0]
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    probe_calls: list[str] = []

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def record_probe(request, timeout):
        probe_calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(maintenance_module, "_open_no_redirect", record_probe)
    global_state = coordinator.state_store.load()
    error = TimeoutError(f"GET {sensitive_url} timed out")

    assert coordinator._record_environment_failure(
        manifest_path,
        incident_id=incident["incident_id"],
        error=error,
        browser_context=SimpleNamespace(pages=[]),
        global_state=global_state,
    ) is True

    current = store.load(manifest_path)
    current_incident = current["maintenance"]["incidents"][0]
    prompt = coordinator._prompt(
        current,
        current_incident,
        include_constructor=False,
        tasks=[current],
    )
    store.update_maintenance(
        manifest_path,
        lambda value: (
            value["maintenance"]["incidents"][0].update(prompt=prompt) or value
        ),
    )
    report = write_maintenance_report(
        tmp_path,
        team="alpha",
        turn=1,
        report=f"# Direct marker value report\n\nObserved {sensitive_url}.\n",
        at=datetime(2026, 7, 25, 7, 40, 0, tzinfo=timezone.utc),
    )
    current = store.load(manifest_path)
    dashboard = build_task_payload(current, tasks=[current])
    surfaces = {
        "manifest": manifest_path.read_text(encoding="utf-8"),
        "global": coordinator.state_store.path.read_text(encoding="utf-8"),
        "prompt": prompt,
        "report": Path(report.path).read_text(encoding="utf-8"),
        "dashboard": json.dumps(dashboard, ensure_ascii=False),
        "timeline": json.dumps(dashboard["timeline"], ensure_ascii=False),
    }

    assert probe_calls == []
    assert current_incident["environment_signature"]["network_evidence"] is None
    assert current_incident["environment_signature"]["network_available"] is False
    for value in surfaces.values():
        assert secret not in value
        assert "[REDACTED]" in value


def test_direct_credential_segment_itself_is_removed_not_only_its_tail():
    from playwright_auto.cdpa_safety import sanitize_url

    url = "https://api.example.invalid/run/secret-token-value/status"
    sanitized = sanitize_url(url)
    assert sanitized is not None
    assert "secret-token-value" not in sanitized
    assert sanitized == (
        "https://api.example.invalid/run/[REDACTED]/[REDACTED]"
    )


_CANONICALIZED_HIGH_RISK_ROUTE_PATHS = (
    "/oauth2/callback/{secret}/complete",
    "/oauthCallback/{secret}/complete",
    "/oauthcallback/{secret}/complete",
    "/oauth2Callback/{secret}/complete",
    "/oauth2callback/{secret}/complete",
    "/signedUrl/{secret}/result",
    "/signedurl/{secret}/result",
    "/magicLink/{secret}/complete",
    "/magiclink/{secret}/complete",
    "/webhookIncoming/{secret}/status",
    "/webhookincoming/{secret}/status",
    "/authorizationCallback/{secret}/complete",
    "/authorizationcallback/{secret}/complete",
    "/apiKey/{secret}/status",
    "/apikey/{secret}/status",
    "/accessToken/{secret}/status",
    "/accesstoken/{secret}/status",
    "/sessionId/{secret}/status",
    "/sessionid/{secret}/status",
    "/passwordResetLink/{secret}",
    "/passwordresetlink/{secret}",
    "/resetPassword/{secret}/complete",
    "/resetpassword/{secret}/complete",
    "/refreshtoken/{secret}/status",
    "/idtoken/{secret}/status",
    "/apitoken/{secret}/status",
    "/clientsecret/{secret}/status",
    "/clientcredential/{secret}/status",
    "/bearertoken/{secret}/status",
    "/authtoken/{secret}/status",
    "/sessiontoken/{secret}/status",
    "/csrftoken/{secret}/status",
    "/verificationcode/{secret}/status",
    "/activationcode/{secret}/status",
    "/invitecode/{secret}/status",
    "/resetcode/{secret}/status",
    "/passwordreset/{secret}/complete",
    "/magiclinkcallback/{secret}/complete",
    "/signedurlcallback/{secret}/complete",
    "/webhooksincoming/{secret}/status",
    "/webhookcallback/{secret}/status",
    "/oauthredirect/{secret}/complete",
    "/oauth2redirect/{secret}/complete",
    "/refreshToken/{secret}/status",
    "/idToken/{secret}/status",
    "/apiToken/{secret}/status",
    "/clientSecret/{secret}/status",
    "/clientCredential/{secret}/status",
    "/bearerToken/{secret}/status",
    "/authToken/{secret}/status",
    "/sessionToken/{secret}/status",
    "/csrfToken/{secret}/status",
    "/verificationCode/{secret}/status",
    "/activationCode/{secret}/status",
    "/inviteCode/{secret}/status",
    "/resetCode/{secret}/status",
    "/passwordReset/{secret}/complete",
    "/magicLinkCallback/{secret}/complete",
    "/signedUrlCallback/{secret}/complete",
    "/webhooksIncoming/{secret}/status",
    "/webhookCallback/{secret}/status",
    "/oauthRedirect/{secret}/complete",
    "/oauth2Redirect/{secret}/complete",
    "/RefreshToken/{secret}/status",
    "/IDToken/{secret}/status",
    "/APIToken/{secret}/status",
    "/ClientSecret/{secret}/status",
    "/CSRFToken/{secret}/status",
    "/VerificationCode/{secret}/status",
    "/MagicLinkCallback/{secret}/complete",
    "/SignedURLCallback/{secret}/complete",
    "/OAuth2Redirect/{secret}/complete",
)


@pytest.mark.parametrize("path_template", _CANONICALIZED_HIGH_RISK_ROUTE_PATHS)
def test_camel_compact_and_numeric_high_risk_routes_are_fully_redacted(
    path_template: str,
):
    from playwright_auto.cdpa_safety import (
        extract_probeable_url,
        probeable_url,
        sanitize_text,
        sanitize_url,
    )

    secret = "K7p4Q9Lm3Vx8"
    url = "https://api.example.invalid" + path_template.format(secret=secret)
    sanitized = sanitize_url(url)

    assert sanitized is not None
    assert secret not in sanitized
    assert "[REDACTED]" in sanitized
    assert secret not in sanitize_text(f"GET {url} failed")
    assert probeable_url(url) is None
    assert extract_probeable_url(f"GET {url} failed") is None


@pytest.mark.parametrize("path_template", _CANONICALIZED_HIGH_RISK_ROUTE_PATHS)
def test_canonicalized_high_risk_routes_never_probe_or_persist_on_maintenance_surfaces(
    tmp_path: Path,
    monkeypatch,
    path_template: str,
):
    import json
    from types import SimpleNamespace

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.dashboard import build_task_payload
    from test_cdpa_core import write_config

    secret = "K7p4Q9Lm3Vx8"
    sensitive_url = "https://api.example.invalid" + path_template.format(secret=secret)
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    store = TaskStore(config)
    state = store.create_task(
        "canonicalized route credential sanitization",
        requested_team="alpha",
        task_id="task-canonicalized-route-credential",
    )
    manifest_path = Path(state["manifest_path"])

    def block(current):
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="unexpected_error",
            block_reason="network timeout",
        )
        incident = ensure_maintenance_incident(current)
        assert incident is not None
        return current

    state = store.update(manifest_path, block)
    incident = state["maintenance"]["incidents"][0]
    coordinator = maintenance_module.MaintainerCoordinator(config, store=store)
    probe_calls: list[str] = []

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def record_probe(request, timeout):
        probe_calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(maintenance_module, "_open_no_redirect", record_probe)
    global_state = coordinator.state_store.load()
    error = TimeoutError(f"GET {sensitive_url} timed out")

    assert coordinator._record_environment_failure(
        manifest_path,
        incident_id=incident["incident_id"],
        error=error,
        browser_context=SimpleNamespace(pages=[]),
        global_state=global_state,
    ) is True

    current = store.load(manifest_path)
    current_incident = current["maintenance"]["incidents"][0]
    prompt = coordinator._prompt(
        current,
        current_incident,
        include_constructor=False,
        tasks=[current],
    )
    store.update_maintenance(
        manifest_path,
        lambda value: (
            value["maintenance"]["incidents"][0].update(prompt=prompt) or value
        ),
    )
    report = write_maintenance_report(
        tmp_path,
        team="alpha",
        turn=1,
        report=f"# Canonicalized route report\n\nObserved {sensitive_url}.\n",
        at=datetime(2026, 7, 25, 8, 10, 0, tzinfo=timezone.utc),
    )
    current = store.load(manifest_path)
    dashboard = build_task_payload(current, tasks=[current])
    surfaces = {
        "manifest": manifest_path.read_text(encoding="utf-8"),
        "global": coordinator.state_store.path.read_text(encoding="utf-8"),
        "prompt": prompt,
        "report": Path(report.path).read_text(encoding="utf-8"),
        "dashboard": json.dumps(dashboard, ensure_ascii=False),
        "timeline": json.dumps(dashboard["timeline"], ensure_ascii=False),
    }

    assert probe_calls == []
    assert current_incident["environment_signature"]["network_evidence"] is None
    assert current_incident["environment_signature"]["network_available"] is False
    for value in surfaces.values():
        assert secret not in value
        assert "[REDACTED]" in value


@pytest.mark.parametrize(
    "path",
    (
        "/v1/resources/K7p4Q9Lm3Vx8/status",
        "/v1/oauth20/K7p4Q9Lm3Vx8/status",
        "/v1/oauthcallbacker/K7p4Q9Lm3Vx8/status",
        "/v1/authorizationcallbacker/K7p4Q9Lm3Vx8/status",
        "/v1/magiclinker/K7p4Q9Lm3Vx8/status",
        "/v1/signedness/K7p4Q9Lm3Vx8/status",
        "/v1/webhookincomingarchive/K7p4Q9Lm3Vx8/status",
        "/v1/monkey/K7p4Q9Lm3Vx8/status",
        "/v1/verificationcoder/K7p4Q9Lm3Vx8/status",
        "/v1/activationcoder/K7p4Q9Lm3Vx8/status",
        "/v1/invitecoder/K7p4Q9Lm3Vx8/status",
        "/v1/magiclinkcallbacker/K7p4Q9Lm3Vx8/status",
        "/v1/signedurlcallbacker/K7p4Q9Lm3Vx8/status",
        "/v1/oauth2redirector/K7p4Q9Lm3Vx8/status",
        "/v1/authorizationredirector/K7p4Q9Lm3Vx8/status",
    ),
)
def test_compact_alias_matching_does_not_use_generic_substrings(path: str):
    from playwright_auto.cdpa_safety import probeable_url, sanitize_url

    url = "https://api.example.invalid" + path
    assert sanitize_url(url) == url
    assert probeable_url(url) == url
