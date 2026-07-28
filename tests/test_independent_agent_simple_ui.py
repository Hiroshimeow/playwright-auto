from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_actions import AcquiredRole
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


def test_manual_completion_requests_immediate_existing_idle_close_and_retains_url(
    tmp_path: Path,
):
    _config, store, state, worker = setup_agent(tmp_path)
    responded = mark_responded(store, state, trigger_type="manual")
    _completed, successor = store.complete_independent_task(
        responded["manifest_path"],
        outcome="SUCCESS",
        summary="Manual job completed.",
    )

    assert successor["independent"]["close_tab_when_idle"] is True
    saved_page_id = successor["roles"]["AGENT"]["page_id"]
    saved_page_url = successor["roles"]["AGENT"]["page_url"]

    worker.hydrate_runtime(startup=False)
    actions = FakeActions()
    changed = asyncio.run(worker._close_idle_independent_tabs(actions))
    persisted = store.load(successor["manifest_path"])

    assert successor["task_id"] in changed
    assert actions.closed_teams == 1
    assert persisted["independent"]["close_tab_when_idle"] is False
    assert persisted["roles"]["AGENT"]["page_id"] == saved_page_id
    assert persisted["roles"]["AGENT"]["page_url"] == saved_page_url
    assert persisted["roles"]["AGENT"]["online"] is False
    assert persisted["independent"]["idle_tab_closed_at"]

    class ReopenActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.reopened_url = None

        async def locate_owned(self, _state, _role):
            return None

        async def reopen(self, current, logical_role, *, require_clean_ready=True):
            assert require_clean_ready is True
            record = current["roles"][logical_role]
            self.reopened_url = record["page_url"]
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id=record["page_id"],
                url=record["page_url"],
                created=True,
                new_chat=False,
            )

    reopen_actions = ReopenActions()
    result = asyncio.run(
        worker._apply_independent_control(
            persisted,
            {"action": "open_tab"},
            reopen_actions,
            role="AGENT",
            hop=_active_hop(persisted),
        )
    )
    assert result == {
        "page_id": saved_page_id,
        "created": False,
        "reopened": True,
    }
    assert reopen_actions.reopened_url == saved_page_url

    nonmanual_root = tmp_path / "nonmanual"
    nonmanual_root.mkdir()
    _config2, store2, state2, worker2 = setup_agent(nonmanual_root)
    responded2 = mark_responded(store2, state2, trigger_type="check_all")
    _completed2, successor2 = store2.complete_independent_task(
        responded2["manifest_path"],
        outcome="SUCCESS",
        summary="CHECK_ALL completed.",
    )
    assert successor2["independent"].get("close_tab_when_idle") is False
    worker2.hydrate_runtime(startup=False)
    actions2 = FakeActions()
    changed2 = asyncio.run(worker2._close_idle_independent_tabs(actions2))
    assert successor2["task_id"] not in changed2
    assert actions2.closed_teams == 0


def test_board_uses_simple_labels_command_dialog_and_restored_settings():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")

    for label in (
        "Run once",
        "Command",
        "Open tab",
        "Close tab",
        "Renew",
        "Settings",
        "History",
        "Reports",
    ):
        assert f'"{label}"' in detail
    assert '"Run now"' not in detail
    assert '"New Chat next job"' not in detail
    assert 'id="agent-command-dialog"' in html
    assert 'name="instruction"' in html
    assert "detail.agent?.system_prompt || \"\"" in app
    assert "new_chat_next_job" not in html[html.index('id="agent-settings-dialog"') :]
    assert "instruction" in app
    assert "trigger_settings: triggerSettings" in app
