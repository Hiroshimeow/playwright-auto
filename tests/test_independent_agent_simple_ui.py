from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_actions import AcquiredRole, RoleOwnershipError
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.dashboard import ASSET_ROOT, DASHBOARD_HTML_PATH
from playwright_auto.dashboard_api import DashboardAPI

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions


def setup_agent(tmp_path: Path, *, prompt: str = "Inspect the requested work."):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_independent_agent(
        "Simple Agent",
        system_prompt=prompt,
        task_id="agent-simple-g1",
        trigger_settings={
            "recovery": False,
            "interval_minutes": 30,
            "task_done": True,
            "role_completed": ["DEV", "REVIEW"],
            "teams": ["alpha", "beta"],
            "states": ["RUNNING", "BLOCKED"],
            "check_all": True,
        },
    )
    return config, store, state, CDPAWorker(config, store=store)


def mark_responded(store: TaskStore, state: dict, *, trigger_type: str = "manual") -> dict:
    state = store.run_independent_now(state["manifest_path"], trigger_type=trigger_type)

    def mutate(current: dict) -> dict:
        hop = _active_hop(current)
        hop["state"] = "responded"
        hop["response"] = "Completed and verified."
        hop["response_sha256"] = "a" * 64
        role = current["roles"]["AGENT"]
        role["page_id"] = "saved-page"
        role["page_url"] = "https://chatgpt.com/c/saved-conversation"
        role["online"] = True
        hop["conversation_url"] = role["page_url"]
        return current

    return store.update(state["manifest_path"], mutate)


def test_new_triggered_agent_ignores_history_and_opens_only_for_a_new_event(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    old = store.create_task("Old done", requested_team="old", task_id="old-done")
    old = store.update(
        old["manifest_path"],
        lambda current: {
            **current,
            "status": "DONE",
            "terminal_state": "DONE",
            "completed_at": "2026-07-01T00:00:00+00:00",
            "updated_at": "2026-07-01T00:00:00+00:00",
            "active_role": None,
            "active_hop_id": None,
        },
    )
    agent = store.create_independent_agent(
        "Future DONE watcher",
        system_prompt="Report newly completed tasks.",
        trigger_settings={"task_done": True},
    )

    assert agent["status"] == "WAITING"
    assert agent["independent"]["active_event"] is None
    assert agent["roles"]["AGENT"]["page_id"] is None
    assert agent["roles"]["AGENT"]["page_url"] is None
    assert agent["roles"]["AGENT"]["online"] is False

    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime(startup=False)
    assert agent["task_id"] not in worker._activate_independent_agents()
    persisted = store.load(agent["manifest_path"])
    assert persisted["status"] == "WAITING"
    assert persisted["independent"]["active_event"] is None

    new = store.create_task("New done", requested_team="new", task_id="new-done")
    store.update(
        new["manifest_path"],
        lambda current: {
            **current,
            "status": "DONE",
            "terminal_state": "DONE",
            "completed_at": "2099-07-01T00:00:00+00:00",
            "updated_at": "2099-07-01T00:00:00+00:00",
            "active_role": None,
            "active_hop_id": None,
        },
    )
    worker.hydrate_runtime(startup=False)
    assert agent["task_id"] in worker._activate_independent_agents()
    persisted = store.load(agent["manifest_path"])
    assert persisted["status"] == "RUNNING"
    assert persisted["independent"]["active_event"]["target_task_id"] == "new-done"


def test_command_dispatch_is_idempotent_and_persists_instruction(tmp_path: Path):
    config, store, state, worker = setup_agent(tmp_path)
    worker.hydrate_runtime()
    api = DashboardAPI(config, db=worker.runtime_db)
    assert api.normalize_independent_run({}) == {"trigger_type": "manual"}
    with pytest.raises(Exception, match="instruction"):
        api.normalize_independent_run({"instruction": "   "})
    with pytest.raises(Exception, match="manual"):
        api.normalize_independent_run(
            {"trigger_type": "check_all", "instruction": "Not valid."}
        )
    payload = api.normalize_independent_run(
        {
            "trigger_type": "manual",
            "instruction": "Inspect only the selected task.",
        }
    )
    first = api.enqueue(
        idempotency_key="same-command",
        kind="independent_run_now",
        task_id=state["task_id"],
        payload=payload,
    )
    replay = api.enqueue(
        idempotency_key="same-command",
        kind="independent_run_now",
        task_id=state["task_id"],
        payload=payload,
    )

    assert replay["command_id"] == first["command_id"]
    assert worker.dispatch_command_once()["status"] == "applied"
    assert worker.dispatch_command_once() is None
    persisted = store.load(state["manifest_path"])
    assert persisted["independent"]["active_event"]["instruction"] == (
        "Inspect only the selected task."
    )
    assert persisted["applied_command_ids"].count(first["command_id"]) == 1
    assert '"instruction": "Inspect only the selected task."' in (
        _active_hop(persisted)["handoff"]
    )

def test_selected_detail_exposes_prompt_without_board_summary_leak(tmp_path: Path):
    prompt = "Full current prompt\nwith private-identity-123 saved exactly."
    _config, _store, state, _worker = setup_agent(tmp_path, prompt=prompt)
    state["roles"]["AGENT"]["page_id"] = "private-identity-123"

    projection = build_task_projection(state, tasks=[state])

    assert "system_prompt" not in projection.summary["agent"]
    assert projection.detail["agent"]["system_prompt"] == prompt
    assert projection.detail["agent"]["trigger_settings"] == state["independent"][
        "trigger_settings"
    ]


def test_idle_renew_closes_and_discards_old_binding_then_next_run_uses_new_chat(
    tmp_path: Path,
):
    _config, store, state, worker = setup_agent(tmp_path)
    hop = _active_hop(state)
    role = state["roles"]["AGENT"]
    role.update(
        page_id="old-page",
        page_url="https://chatgpt.com/c/old-conversation",
        online=True,
    )
    hop["conversation_url"] = role["page_url"]

    class RecordingActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.new_chat_calls = 0

        async def new_chat(self, current, logical_role):
            self.new_chat_calls += 1
            return await super().new_chat(current, logical_role)

    actions = RecordingActions()
    result = asyncio.run(
        worker._apply_independent_control(
            state,
            {"action": "new_chat"},
            actions,
            role="AGENT",
            hop=hop,
        )
    )

    assert result == {"renewed": True, "closed_tabs": 1}
    assert state["independent"]["new_chat_next_job"] is True
    assert state["independent"].get("new_chat_deferred_task_id") is None
    assert role["page_id"] is None
    assert role["page_url"] is None
    assert role["online"] is False
    assert hop.get("conversation_url") is None
    assert actions.new_chat_calls == 0

    state = store.save(state["manifest_path"], state)
    running = store.run_independent_now(state["manifest_path"], trigger_type="manual")
    asyncio.run(worker._pre_send(running, _active_hop(running), actions))

    assert actions.new_chat_calls == 1
    assert running["independent"]["new_chat_next_job"] is False
    assert _active_hop(running)["state"] == "sending"


def test_renew_rejects_accepted_inflight_without_mutation(tmp_path: Path):
    _config, _store, state, worker = setup_agent(tmp_path)
    hop = _active_hop(state)
    hop["state"] = "sent"
    hop["receipt"] = {"accepted_at": "2026-07-29T00:00:00+00:00"}
    state["roles"]["AGENT"].update(
        page_id="old-page",
        page_url="https://chatgpt.com/c/old-conversation",
        online=True,
    )
    before = copy.deepcopy(state)
    actions = FakeActions()

    with pytest.raises(RuntimeError, match="in-flight"):
        asyncio.run(
            worker._apply_independent_control(
                state,
                {"action": "new_chat"},
                actions,
                role="AGENT",
                hop=hop,
            )
        )

    assert state == before
    assert actions.closed_teams == 0


@pytest.mark.parametrize("trigger_type", ["manual", "check_all"])
def test_completed_job_closes_after_one_minute_and_next_trigger_reopens_saved_url(
    tmp_path: Path,
    trigger_type: str,
):
    _config, store, state, worker = setup_agent(tmp_path)
    responded = mark_responded(store, state, trigger_type=trigger_type)
    completed = store.complete_independent_task(
        responded["manifest_path"],
        outcome="SUCCESS",
        summary=f"{trigger_type} job completed.",
    )

    assert completed["task_id"] == responded["task_id"]
    assert completed["independent"]["close_tab_when_idle"] is False
    assert completed["status"] == ("PAUSED" if trigger_type == "manual" else "WAITING")
    saved_page_id = completed["roles"]["AGENT"]["page_id"]
    saved_page_url = completed["roles"]["AGENT"]["page_url"]
    idle_epoch = datetime.fromisoformat(
        completed["independent"]["idle_since"]
    ).timestamp()

    worker.hydrate_runtime(startup=False)
    actions = FakeActions()
    assert not asyncio.run(
        worker._close_idle_independent_tabs(actions, now_epoch=idle_epoch + 59)
    )
    assert actions.closed_teams == 0

    changed = asyncio.run(
        worker._close_idle_independent_tabs(actions, now_epoch=idle_epoch + 60)
    )
    persisted = store.load(completed["manifest_path"])
    assert completed["task_id"] in changed
    assert actions.closed_teams == 1
    assert persisted["roles"]["AGENT"]["page_id"] == saved_page_id
    assert persisted["roles"]["AGENT"]["page_url"] == saved_page_url
    assert persisted["roles"]["AGENT"]["online"] is False
    assert persisted["independent"]["idle_tab_closed_at"]

    class ReopenActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.reopened_url = None
            self.required_clean_ready = None

        async def acquire(self, _state, _role):
            raise RoleOwnershipError("saved agent tab is offline", code="role_offline")

        async def reopen(self, current, logical_role, *, require_clean_ready=True):
            record = current["roles"][logical_role]
            self.reopened_url = record["page_url"]
            self.required_clean_ready = require_clean_ready
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id=record["page_id"],
                url=record["page_url"],
                created=True,
                new_chat=False,
            )

    if trigger_type == "manual":
        persisted = store.update_independent_agent(
            persisted["manifest_path"], enabled=True
        )
    running = store.run_independent_now(
        persisted["manifest_path"], trigger_type="manual"
    )
    reopen_actions = ReopenActions()
    asyncio.run(worker._pre_send(running, _active_hop(running), reopen_actions))
    assert reopen_actions.reopened_url == saved_page_url
    assert reopen_actions.required_clean_ready is True
    assert _active_hop(running)["state"] == "sending"

def test_board_uses_operator_labels_run_task_and_restored_settings():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    create_form = html[
        html.index('id="agent-form"') : html.index('id="agent-command-dialog"')
    ]

    for label in (
        "Run task",
        "Open tab",
        "Close tab",
        "Reset",
        "Settings",
        "History",
        "Reports",
    ):
        assert f'"{label}"' in detail
    for removed in ("Run once", "Command", "Stop current job", "Renew"):
        assert f'"{removed}"' not in detail
    assert 'aria-expanded="false"' in html
    assert 'data-open-agent>Add Agents</button>' in html
    assert '<h2 id="agents-panel-title">Add Agents</h2>' in html
    assert 'aria-label="Close Add Agents panel"' in html
    assert 'data-new-agent>New agent</button>' in html
    assert 'name="independent"' in create_form
    assert 'New agents are Custom Workflow Agents' in create_form
    assert 'name="mode"' not in create_form
    assert 'Add independent agent' not in html
    command_form = html[html.index('id="agent-command-dialog"') :]
    assert '<h2>Run task</h2>' in command_form
    assert 'name="instruction"' in command_form
    assert 'name="max_cycles"' in create_form
    assert 'Max turns per job' in html
    assert '0 means Unlimited.' in html
    for trigger in (
        "manual",
        "interval",
        "task_done",
        "role_completed",
        "task_state",
        "check_all",
        "recovery",
    ):
        assert f'value="{trigger}"' in create_form
    assert "function basicTriggerSettings(values)" in app
    assert "function configuredTriggerSettings(form, values)" in app
    assert app.count("configuredTriggerSettings(roots.agentForm, values)") == 1
    assert app.count("configuredTriggerSettings(roots.agentSettingsForm, values)") == 1
    assert 'task_done: ["task_team"]' in app
    assert 'mode: "Independent"' in app
    assert 'Create independent agent · ${name}' in app
    assert 'Create workflow agent · ${name}' in app
    assert "No lifecycle records for this agent." in detail
    assert "No reports for this agent." in detail
    assert "this independent agent" not in app
    assert 'body: {trigger_type: "manual", instruction}' in app
    assert '/api/independent-agents/${encodeURIComponent(taskId)}/reset' in app
    assert '.filter(item => !item.agent?.deleted_at)' in app
    assert 'app.js?v=20260731-agent-ux-v3' in html
    assert "new_chat_next_job" not in html[html.index('id="agent-settings-dialog"') :]
