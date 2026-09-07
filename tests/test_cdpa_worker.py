from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_store as store_module
import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import (
    AcquiredRole,
    BranchBootstrapError,
    RoleOwnershipError,
    TeamCloseError,
)
from playwright_auto.cdpa_bootstraps import BootstrapCatalog
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_independent import (
    canonical_independent_events,
    validate_trigger_settings,
)
from playwright_auto.cdpa_routes import RouteContractError
from playwright_auto.cdpa_store import TaskStore, utc_now
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop, _report_mode
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    PageOwnershipError,
    RateLimitBlockedError,
    SendReceipt,
    StableMalformedResponseError,
    UnsafePageStateError,
    capture_message_baseline,
    capture_response_recovery_baseline,
    response_activity_signature,
    prompt_digest,
)
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_core import (
    install_cycle_isolation_graph,
    install_duplicate_task_graph,
    poison_catalog_identity_entry,
    write_config,
)


def canonical_recovery_events(state: dict) -> list[dict]:
    agent = {
        "task_mode": "independent",
        "task_id": "agent-test-maintainers-g1",
        "team": "agent-test-maintainers",
        "status": "WAITING",
        "independent": {
            "agent_name": "Test Maintainers",
            "agent_key": "test maintainers",
            "enabled": True,
            "trigger_settings": validate_trigger_settings({"recovery": True}),
            "active_event": None,
            "watermarks": {"seen_event_keys": []},
            "occurrence_counts": {},
        },
    }
    return [
        event
        for event in canonical_independent_events(agent, [state, agent])
        if event["trigger_type"] == "recovery"
    ]


class FakeActions:
    def __init__(self):
        self.restart_roles = []
        self.located_roles = []
        self.closed_teams = 0
        self.preflight_calls = 0
        self.stop_calls = 0
        self.pages = ["assigned-page"]

    async def acquire(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"page-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=record.get("page_id") is None,
            new_chat=False,
        )

    async def restart(self, state, role, *, known_automated_draft=None):
        self.restart_roles.append(role)
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"restarted-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=True,
            new_chat=True,
        )

    async def locate_owned(self, state, role):
        self.located_roles.append(role)
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"page-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=False,
            new_chat=False,
        )

    async def stop_if_active(self, acquired):
        self.stop_calls += 1
        return True

    async def new_chat(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"new-chat-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=False,
            new_chat=True,
        )

    async def preflight_team(self, _state):
        self.preflight_calls += 1
        return list(self.pages)

    async def close_team(self, _state, *, preflighted_pages=None):
        selected = list(preflighted_pages or [])
        assert all(page in self.pages for page in selected)
        self.closed_teams += 1
        for page in selected:
            self.pages.remove(page)
        return len(selected)


def setup_task(
    tmp_path: Path,
    *,
    task_id="task-a",
    report_mode="file",
    roles=None,
    repository=None,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id=task_id,
        report_mode=report_mode,
        roles=roles,
        repository=repository,
    )
    return config, store, state, CDPAWorker(config, store=store)


def test_ambient_page_automation_uses_workspace_timeout_config(tmp_path: Path, monkeypatch):
    config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-timeout-config"
    )
    seen_timeouts = []

    class Page:
        url = "https://chatgpt.com/c/ambient"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + "{}"

    class Client:
        def __init__(self, _page, *, timeout_ms):
            seen_timeouts.append(timeout_ms)

        def install_ambient_observer(self):
            return None

        async def read_wait_probe(self):
            return SimpleNamespace(stop_visible=False, last_assistant_message_id=None)

        def ambient_permission_action(self):
            return None

        async def mcp_allow_visible(self):
            return False

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[Page()])))

    assert seen_timeouts == [min(15_000, round(config.workspace_timeout_seconds * 1000))]


def test_ambient_page_automation_never_dispatches_registered_task(tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-single-owner"
    )
    worker.registry = SimpleNamespace(
        tasks_by_id={
            "task-ambient-single-owner": {
                "status": "RUNNING",
                "active_hop_id": 1,
                "hops": [{"hop_id": 1, "target_role": "PLAN", "state": "waiting"}],
                "roles": {"PLAN": {"page_id": "page-active"}},
            }
        }
    )
    calls = []

    class Page:
        url = "https://chatgpt.com/c/ambient"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": "task-ambient-single-owner",
                "pageId": "page-active",
                "role": "cdpa-ambient-single-owner-plan",
            })

    class Client:
        def __init__(self, _page, *, timeout_ms):
            calls.append(("init", timeout_ms))

        def install_ambient_observer(self):
            calls.append(("listen",))

        async def read_wait_probe(self):
            return SimpleNamespace(
                page_task_id="task-ambient-single-owner",
                stop_visible=False,
                last_assistant_message_id=None,
            )

        def ambient_permission_action(self):
            raise AssertionError("ambient controller must not inspect registered task permission")

        async def mcp_allow_visible(self):
            raise AssertionError("ambient controller must not dispatch registered task permission")

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[Page()])))

    assert calls[0][0] == "init"
    assert calls[1] == ("listen",)


def test_worker_arms_passive_observer_with_exact_hop_generation_and_receipt(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-passive-observer-wiring"
    )
    hop = _active_hop(state)
    state["roles"]["PLAN"]["conversation_generation"] = 7
    receipt = SendReceipt(
        prompt="prompt",
        prompt_sha256=prompt_digest("prompt"),
        binding=PageBinding("page-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u-passive",
        user_turn_id="t-passive",
        conversation_id="conversation-passive",
    )

    class Client:
        def __init__(self):
            self.calls = []

        def arm_passive_observer(self, **kwargs):
            self.calls.append(kwargs)

    client = Client()
    worker._arm_passive_request_observer(state, hop, client, receipt)

    assert client.calls == [
        {
            "request_id": hop["request_id"],
            "generation": 7,
            "conversation_id": "conversation-passive",
            "accepted_user_message_id": "u-passive",
            "task_id": "task-passive-observer-wiring",
            "team": "alpha",
        }
    ]


def test_mcp_allow_interrupt_waits_five_seconds_then_dispatches_exact_react_action(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-listen-auto-allow"
    )
    receipt = replace(
        receipt,
        prompt="use authorized mcp-g8 connector",
        prompt_sha256=prompt_digest("use authorized mcp-g8 connector"),
    )
    hop["wait"]["mcp_allow_seen_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=6)
    ).isoformat()
    calls = []
    passive_action = {
        "type": "allow",
        "target_message_id": "call-1",
        "remember_answer": True,
        "label": "Allow mcp-g8 for this conversation",
    }

    class Client:
        def passive_observation(self, **_kwargs):
            return {"permission_action": passive_action}

        async def mcp_allow_visible(self):
            return False

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            calls.append(("allow", passive_action))
            return {
                "method": "react_handler",
                "target_message_id": "call-1",
                "remember_answer": "true",
            }

        def clear_passive_permission_action(self):
            calls.append(("cleared",))

    snapshot = SimpleNamespace(stop_visible=False, messages=())
    handled = asyncio.run(
        worker._mcp_allow_interrupt(state, hop, Client(), receipt, snapshot)
    )

    assert handled is True
    assert calls == [("allow", passive_action), ("cleared",)]
    assert hop["wait"]["mcp_allow_clicked_at"]
    assert state["active_action"] == "wait_mcp_allow_continuation"


def test_normal_wait_uses_dom_controller_without_stream_status(tmp_path: Path):
    _store, state, worker, path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-first-no-status"
    )
    calls = []

    async def fake_dom(*_args, **_kwargs):
        calls.append("dom")

    worker._waiting_dom = fake_dom

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("normal wait must not poll stream_status")

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert calls == ["dom"]


def test_controller_stall_refreshes_after_ten_minutes_without_progress(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-controller-stall-refresh"
    )
    hop["wait"]["controller_state"] = "STOP"
    hop["wait"]["controller_state_since"] = (
        datetime.now(timezone.utc) - timedelta(seconds=601)
    ).isoformat()
    refreshed = []

    class Client:
        async def mcp_allow_visible(self):
            return True

        async def refresh(self):
            refreshed.append(True)

    snapshot = SimpleNamespace(
        retry_visible=False,
        stop_visible=True,
        response_activity_turn_id="turn-streaming",
        messages=(),
    )
    handled = asyncio.run(
        worker._refresh_stalled_controller_state(state, hop, Client(), snapshot, receipt)
    )
    assert handled is True
    assert refreshed == [True]
    assert hop["wait"]["controller_state"] == "STOP"


def test_retry_ui_queues_format_repair_without_retry_click(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-retry-format-repair"
    )
    worker._queue_format_repair(
        state,
        hop,
        None,
        RouteContractError("ChatGPT Retry UI is visible; continue from the existing state"),
    )
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == hop["target_role"]
    assert repair["turn"] == hop["turn"]


def test_mcp_allow_interrupt_refreshes_once_when_post_click_state_does_not_progress(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-allow-post-click-refresh"
    )
    hop["wait"]["mcp_allow_clicked_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=6)
    ).isoformat()
    refreshed = []

    class Client:
        async def mcp_allow_visible(self):
            return True

        async def refresh(self):
            refreshed.append(True)

    snapshot = SimpleNamespace(stop_visible=False, messages=())
    handled = asyncio.run(
        worker._mcp_allow_interrupt(state, hop, Client(), receipt, snapshot)
    )

    assert handled is True
    assert refreshed == [True]
    assert "mcp_allow_clicked_at" not in hop["wait"]
    assert state["active_action"] == "wait_response"


def test_worker_hot_mode_switch_keeps_listener_attached_in_dom_only(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-passive-mode-switch"
    )
    hop = _active_hop(state)
    worker.runtime_db.ensure_schema()

    class Client:
        def __init__(self):
            self.armed = 0
            self.detached = 0

        def arm_passive_observer(self, **_kwargs):
            self.armed += 1

        def detach_passive_observer(self):
            self.detached += 1

    client = Client()
    worker.runtime_db.put_snapshot("settings", {"dom_only": True})
    worker._arm_passive_request_observer(state, hop, client)
    assert (client.armed, client.detached) == (1, 0)

    worker.runtime_db.put_snapshot("settings", {"dom_only": False})
    worker._arm_passive_request_observer(state, hop, client)
    assert (client.armed, client.detached) == (2, 0)


def test_waiting_dom_snapshot_uses_passive_wake_as_probe_window(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-passive-wake"
    )
    hop = _active_hop(state)
    state["roles"]["PLAN"]["conversation_generation"] = 3
    receipt = SendReceipt(
        prompt="prompt",
        prompt_sha256=prompt_digest("prompt"),
        binding=PageBinding("page-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u-passive",
        user_turn_id="t-passive",
        conversation_id="conversation-passive",
    )
    calls = []

    class Client:
        async def wait_for_passive_observation(self, **kwargs):
            calls.append(("passive", kwargs))
            return True

        async def wait_snapshot(self, _receipt, **kwargs):
            calls.append(("snapshot", kwargs))
            return SimpleNamespace()

    acquired = AcquiredRole(Client(), "page-plan", "https://chatgpt.com/c/conversation-passive", False, False)
    snapshot, recovered = asyncio.run(
        worker._waiting_dom_snapshot(
            state,
            hop,
            SimpleNamespace(),
            acquired,
            Path(state["manifest_path"]),
            json.loads(json.dumps(state)),
            receipt,
            transport_baseline=None,
            probe_wait_ms=12_000,
        )
    )

    assert snapshot is not None
    assert recovered is acquired
    assert calls == [
        (
            "passive",
            {"request_id": hop["request_id"], "generation": 3, "timeout_ms": 12_000},
        ),
        ("snapshot", {"force_full": True, "probe_wait_ms": 0}),
    ]


def test_waiting_dom_snapshot_preserves_probe_window_when_passive_not_applicable(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-passive-not-applicable"
    )
    hop = _active_hop(state)
    receipt = SendReceipt(
        prompt="prompt",
        prompt_sha256=prompt_digest("prompt"),
        binding=PageBinding("page-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u-passive",
        user_turn_id="t-passive",
        conversation_id="conversation-passive",
    )
    calls = []

    class Client:
        async def wait_for_passive_observation(self, **kwargs):
            calls.append(("passive", kwargs))
            return None

        async def wait_snapshot(self, _receipt, **kwargs):
            calls.append(("snapshot", kwargs))
            return SimpleNamespace()

    acquired = AcquiredRole(Client(), "page-plan", "https://chatgpt.com/c/conversation-passive", False, False)
    snapshot, recovered = asyncio.run(
        worker._waiting_dom_snapshot(
            state,
            hop,
            SimpleNamespace(),
            acquired,
            Path(state["manifest_path"]),
            json.loads(json.dumps(state)),
            receipt,
            transport_baseline=None,
            probe_wait_ms=12_000,
        )
    )

    assert snapshot is not None
    assert recovered is acquired
    assert calls == [
        (
            "passive",
            {"request_id": hop["request_id"], "generation": 0, "timeout_ms": 12_000},
        ),
        ("snapshot", {"force_full": False, "probe_wait_ms": 12_000}),
    ]


def test_waiting_dom_snapshot_passive_timeout_consumes_probe_window_once(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-passive-timeout"
    )
    hop = _active_hop(state)
    receipt = SendReceipt(
        prompt="prompt",
        prompt_sha256=prompt_digest("prompt"),
        binding=PageBinding("page-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u-passive",
        user_turn_id="t-passive",
        conversation_id="conversation-passive",
    )
    calls = []

    class Client:
        async def wait_for_passive_observation(self, **kwargs):
            calls.append(("passive", kwargs))
            return False

        async def wait_snapshot(self, _receipt, **kwargs):
            calls.append(("snapshot", kwargs))
            return SimpleNamespace()

    acquired = AcquiredRole(Client(), "page-plan", "https://chatgpt.com/c/conversation-passive", False, False)
    snapshot, recovered = asyncio.run(
        worker._waiting_dom_snapshot(
            state,
            hop,
            SimpleNamespace(),
            acquired,
            Path(state["manifest_path"]),
            json.loads(json.dumps(state)),
            receipt,
            transport_baseline=None,
            probe_wait_ms=12_000,
        )
    )

    assert snapshot is not None
    assert recovered is acquired
    assert calls == [
        (
            "passive",
            {"request_id": hop["request_id"], "generation": 0, "timeout_ms": 12_000},
        ),
        ("snapshot", {"force_full": False, "probe_wait_ms": 0}),
    ]


def bootstrap_record(*, bootstrap_id="general-team-bootstrap", enabled=True):
    return {
        "bootstrap_id": bootstrap_id,
        "name": "General Team Bootstrap",
        "description": "Reusable task-neutral context",
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_assistant_message_id": "22222222-2222-4222-8222-222222222222",
        "enabled": enabled,
        "tags": ["general"],
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:00+00:00",
        "expires_at": None,
        "last_verified_at": None,
        "source_fingerprints": {},
    }


def send_snapshot(
    *,
    text="",
    messages=(),
    state=ChatGPTState.NEW_CHAT,
    task_id="task-a",
    team="alpha",
):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/cdpa-send",
        session_id="cdpa-send",
        page_id="page-PLAN",
        page_role="PLAN",
        page_task_id=task_id,
        page_team=team,
        state=state,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text=text,
        send_visible=bool(text),
        send_enabled=bool(text),
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=(),
        messages=tuple(messages),
    )


class RecordingCDPASendClient:
    def __init__(self, *, task_id="task-a", team="alpha"):
        self.binding = PageBinding("page-PLAN", "PLAN")
        self.current = send_snapshot(task_id=task_id, team=team)
        self.set_calls = []
        self.send_calls = []

    async def assert_ownership(self):
        return self.current

    async def set_text(self, text):
        self.set_calls.append(text)
        self.current = send_snapshot(
            text=text,
            messages=self.current.messages,
            state=ChatGPTState.DRAFT,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )

    async def send(
        self,
        text,
        *,
        wait_for_stop=True,
        max_attempts=2,
        recovery_reload=True,
        expected_task_id=None,
        expected_team=None,
        expected_attachment_ownership_token=None,
        expected_attachment_count=0,
        expected_attachment_names=None,
    ):
        self.send_calls.append(text)
        baseline = capture_message_baseline(self.current.messages)
        user = MessageSnapshot("user", "u1", "t1", text, ())
        self.current = send_snapshot(
            messages=(*self.current.messages, user),
            state=ChatGPTState.SUBMITTING,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )
        return SendReceipt(
            prompt=text,
            prompt_sha256=prompt_digest(text),
            binding=self.binding,
            baseline=baseline,
            attempts=1,
            accepted_via="user_message_identity",
            session_id_before="cdpa-send",
            user_message_id="u1",
            user_turn_id="t1",
        )


class RecordingCDPASendActions:
    def __init__(self, client):
        self.client = client

    async def locate_owned(self, _state, _role):
        return AcquiredRole(
            client=self.client,
            page_id="page-PLAN",
            url="https://chatgpt.com/c/cdpa-send",
            created=False,
            new_chat=False,
        )


def test_waiting_stop_control_applies_before_waiting_short_circuit(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    dependency = store.create_task(
        "dependency",
        requested_team="dependency",
        task_id="task-waiting-stop-dependency",
    )
    state = store.create_task(
        "waiting child",
        requested_team="waiting-stop",
        task_id="task-waiting-stop",
        depends_on_task_ids=(dependency["task_id"],),
    )
    path = Path(state["manifest_path"])
    assert state["status"] == "WAITING"

    requested = store.request_control(path, "stop", reason="operator force stop")
    assert requested["controls"][-1]["status"] == "requested"

    actions = FakeActions()
    worker = CDPAWorker(config, store=store)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    stopped = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=store.discover(),
        )
    )

    assert stopped == store.load(path)
    assert stopped["status"] == "STOPPED"
    assert stopped["terminal_state"] == "STOPPED"
    assert stopped["controls"][-1]["status"] == "applied"


def test_stop_terminalizes_even_when_owned_tab_lookup_fails(tmp_path: Path):
    config, store, state, worker = setup_task(tmp_path, task_id="task-force-stop")
    path = Path(state["manifest_path"])
    requested = store.request_control(path, "stop", reason="operator force stop")

    class MissingOwnedTabActions(FakeActions):
        async def locate_owned(self, _state, _role):
            raise RoleOwnershipError("owned tab is unavailable")

    stopped = asyncio.run(worker._apply_control(requested, MissingOwnedTabActions(), path))

    assert stopped is True
    assert requested["status"] == "STOPPED"
    assert requested["terminal_state"] == "STOPPED"
    assert requested["controls"][-1]["status"] == "applied"


def test_pause_requested_during_role_acquisition_survives_and_applies_before_send(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "owner",
        requested_team="alpha",
        task_id="task-presend-control-race",
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)

    class ConcurrentAcquireActions(FakeActions):
        async def acquire(self, current, role):
            store.request_control(
                path,
                "pause",
                reason="pause requested during role acquisition",
            )
            return await super().acquire(current, role)

    actions = ConcurrentAcquireActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    first = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert first == store.load(path)
    assert _active_hop(first)["state"] == "sending"
    assert len(first["controls"]) == 1
    assert first["controls"][0]["action"] == "pause"
    assert first["controls"][0]["role"] is None
    assert first["controls"][0]["reason"] == "pause requested during role acquisition"
    assert first["controls"][0]["status"] == "requested"

    second = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert second == store.load(path)
    assert second["status"] == "PAUSED"
    assert second["controls"][0]["status"] == "applied"
    assert _active_hop(second)["state"] == "sending"


def test_pause_requested_after_accepted_send_survives_without_duplicate_send(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "owner",
        requested_team="alpha",
        task_id="task-send-control-race",
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: FakeActions())
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    assert _active_hop(prepared)["state"] == "sending"

    class ConcurrentSendClient(RecordingCDPASendClient):
        async def send(self, text, **kwargs):
            receipt = await super().send(text, **kwargs)
            store.request_control(
                path,
                "pause",
                reason="pause requested after accepted send",
            )
            return receipt

    client = ConcurrentSendClient(
        task_id=prepared["task_id"],
        team=prepared["team"],
    )
    actions = RecordingCDPASendActions(client)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    sent = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert sent == store.load(path)
    assert _active_hop(sent)["state"] == "sent"
    assert len(client.send_calls) == 1
    assert sent["controls"][0]["action"] == "pause"
    assert sent["controls"][0]["status"] == "requested"

    paused = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert paused == store.load(path)
    assert paused["status"] == "PAUSED"
    assert paused["controls"][0]["status"] == "applied"
    assert _active_hop(paused)["state"] == "sent"
    assert len(client.send_calls) == 1


def test_pre_send_lazily_acquires_only_plan_and_persists_constructor(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert hop["state"] == "sending"
    assert hop["expected_report_path"] == ".plan/alpha/alpha-plan_turn1_task-a.md"
    assert "Constructor for PLAN" in hop["prompt"]
    assert ".plan/alpha/alpha-plan_turn1_task-a.md" not in hop["prompt"]
    assert ".plan/<team>/<physical-role>_turn<N>_<task-id>.md" in hop["prompt"]
    assert hop["prompt"].startswith("alpha · role: plan\n{")
    envelope, _ = json.JSONDecoder().raw_decode(
        hop["prompt"].removeprefix("alpha · role: plan\n")
    )
    assert envelope == {
        "title": "Implement exact production behavior",
        "task-id": "task-a",
        "team": "alpha",
        "role": "alpha-plan",
        "source-role": None,
        "turn": 1,
        "workspace": str(tmp_path),
        "allowed-routes": ["PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "PAUSE", "DONE"],
        "goal": "Implement exact production behavior",
        "handoff": "Implement exact production behavior",
    }
    assert state["roles"]["PLAN"]["page_id"] == "page-alpha-plan"
    assert state["roles"]["DEV"]["status"] == "unallocated"
    assert state["roles"]["PLAN"]["constructor_sent_generation"] == 0


def test_pre_send_first_role_uses_bootstrap_once_and_keeps_full_prompt(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = bootstrap_record()
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id="task-bootstrap-runtime",
        roles=("PLAN", "DEV", "REVIEW"),
        bootstrap=anchor,
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branch_calls.append((role, source_conversation_id, assistant_message_id))
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id=f"branch-{role.lower()}",
                url=f"https://chatgpt.com/c/{role.lower()}-branch",
                created=True,
                new_chat=True,
            )

    actions = Actions()
    first = _active_hop(state)
    asyncio.run(worker._pre_send(state, first, actions))

    assert actions.branch_calls == [
        ("PLAN", anchor["conversation_id"], anchor["terminal_assistant_message_id"])
    ]
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"
    assert state["roles"]["PLAN"]["conversation_generation"] == 1
    assert "Constructor for PLAN" in first["prompt"]
    assert "Return only the strict route JSON." in first["prompt"]

    plan_turn2 = worker._append_hop(
        state, source_role="REVIEW", target_role="PLAN", handoff="return to plan"
    )
    asyncio.run(worker._pre_send(state, plan_turn2, actions))
    assert len(actions.branch_calls) == 1
    assert state["roles"]["PLAN"]["page_id"] == "page-alpha-plan"

    for role in ("DEV", "REVIEW"):
        hop = worker._append_hop(
            state, source_role="PLAN", target_role=role, handoff=f"first {role}"
        )
        asyncio.run(worker._pre_send(state, hop, actions))
    assert [call[0] for call in actions.branch_calls] == ["PLAN", "DEV", "REVIEW"]
    assert all(call[1:] == actions.branch_calls[0][1:] for call in actions.branch_calls)


def test_bootstrap_branch_rejects_foreign_durable_writable_conversation(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = bootstrap_record()
    state = store.create_task(
        "owner collision",
        requested_team="owner-collision",
        task_id="task-owner-collision",
        bootstrap=anchor,
    )
    foreign = store.create_task(
        "foreign owner",
        requested_team="foreign-owner",
        task_id="task-foreign-owner",
    )
    candidate_id = "88888888-8888-4888-8888-888888888888"
    foreign_role = foreign["roles"]["PLAN"]
    foreign_role["page_id"] = "foreign-owned-page"
    foreign_role["page_url"] = f"https://chatgpt.com/c/{candidate_id}"
    store.save(Path(foreign["manifest_path"]), foreign)
    worker = CDPAWorker(config, store=store)

    class BranchPage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    branch_page = BranchPage()

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            return AcquiredRole(
                client=SimpleNamespace(page=branch_page),
                page_id="new-branch-page",
                url=f"https://chatgpt.com/c/{candidate_id}",
                created=True,
                new_chat=True,
            )

    async def no_ui_fallback(*_args, **_kwargs):
        raise worker_module.BootstrapUIBranchError("no second branch")

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", no_ui_fallback)
    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", Actions()))

    assert acquired is None
    assert branch_page.closed is True
    assert state["roles"]["PLAN"].get("page_id") is None
    assert state["active_action"] == "bootstrap_retry"


def test_pre_send_unresolved_native_branch_blocks_without_ui_fallback(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = bootstrap_record()
    state = store.create_task(
        "unresolved provisional branch",
        requested_team="unresolved-branch",
        task_id="task-unresolved-branch",
        bootstrap=anchor,
    )
    worker = CDPAWorker(config, store=store)
    unresolved_type = getattr(
        worker_module, "BranchTargetUnresolvedError", BranchBootstrapError
    )

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise unresolved_type("branch target remained provisional WEB identity")

    ui_calls = []

    async def forbidden_ui(_state, role, _actions, donor):
        ui_calls.append((role, dict(donor)))
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id="unexpected-ui-page",
            url="https://chatgpt.com/c/unexpected-ui",
            created=True,
            new_chat=True,
        )

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", forbidden_ui)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, Actions()))

    assert ui_calls == []
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "branch_target_unresolved"
    assert state["active_action"] == "blocked"
    assert hop["state"] == "pre_send"
    assert RequestLedger(hop["ledger_path"]).peek(hop["request_id"]) is None


def test_bootstrap_alias_rejection_keeps_durable_attempts_at_zero(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = bootstrap_record()
    state = store.create_task(
        "alias stays pre-send",
        requested_team="alias-zero-send",
        task_id="task-alias-zero-send",
        bootstrap=anchor,
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise BranchBootstrapError("branch canonicalized back to donor")

    async def ui_alias(*_args, **_kwargs):
        raise worker_module.BootstrapUIBranchError(
            "UI branch canonicalized back to donor"
        )

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", ui_alias)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, Actions()))

    assert hop["state"] != "sending"
    assert state["active_action"] == "bootstrap_retry"
    assert RequestLedger(hop["ledger_path"]).peek(hop["request_id"]) is None


def test_pre_send_bootstrap_fallback_chain_is_pre_send_only(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "fallback",
        requested_team="fallback",
        task_id="task-bootstrap-fallback",
        bootstrap=bootstrap_record(),
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branch_calls = 0
            self.acquire_calls = 0

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == bootstrap_record()["conversation_id"]
            return _bootstrap_graph(bootstrap_record()["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise BranchBootstrapError("native unavailable")

        async def acquire(self, state, role):
            self.acquire_calls += 1
            return await super().acquire(state, role)

    actions = Actions()
    ui_calls = []

    async def ui_success(_state, role, _actions, donor):
        ui_calls.append((role, donor["conversation_id"], donor["assistant_message_id"]))
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id="ui-branch-plan",
            url="https://chatgpt.com/c/ui-branch-plan",
            created=True,
            new_chat=True,
        )

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", ui_success, raising=False)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, actions))
    assert actions.branch_calls == 1
    assert ui_calls == [
        (
            "PLAN",
            bootstrap_record()["conversation_id"],
            bootstrap_record()["terminal_assistant_message_id"],
        )
    ]
    assert actions.acquire_calls == 0
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"

    protected = store.create_task(
        "protected send",
        requested_team="protected-send",
        task_id="task-bootstrap-protected-send",
        bootstrap=bootstrap_record(),
    )
    protected_hop = _active_hop(protected)
    protected_hop["receipt"] = {"accepted": True}
    no_actions = Actions()
    asyncio.run(worker._pre_send(protected, protected_hop, no_actions))
    assert no_actions.branch_calls == 0
    assert no_actions.acquire_calls == 0
    assert protected["block_code"] == "pre_send_request_recovery_required"


def test_ui_bootstrap_fallback_uses_exact_semantic_source_message(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = bootstrap_record()
    state = store.create_task(
        "ui fallback semantics",
        requested_team="alpha",
        task_id="task-ui-bootstrap-semantics",
        bootstrap=anchor,
    )
    worker = CDPAWorker(config, store=store)
    calls = []

    class BranchResponse:
        url = "https://chatgpt.com/backend-api/conversation/new_branch"
        status = 200

        async def json(self):
            return {
                "conversation": {
                    "conversation_id": "77777777-7777-4777-8777-777777777777"
                }
            }

    class Button:
        def __init__(self, page, label):
            self.page = page
            self.label = label

        async def click(self):
            calls.append(("click", self.label))
            if self.label == "Branch in new chat":
                self.page.url = "https://chatgpt.com/c/WEB:ui-fallback"
                for listener in tuple(self.page.listeners.get("response", ())):
                    listener(BranchResponse())

    class Turn:
        def __init__(self, page):
            self.page = page

        async def hover(self):
            calls.append(("hover", "turn"))

        def get_by_role(self, role, *, name, exact):
            calls.append(("turn-role", role, name, exact))
            return Button(self.page, name)

    class Assistant:
        def __init__(self, page):
            self.page = page
            self.first = self

        async def wait_for(self, *, state, timeout):
            calls.append(("assistant-wait", state, timeout))

        def locator(self, selector):
            calls.append(("ancestor", selector))
            return Turn(self.page)

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.closed = False
            self.listeners = {}

        async def goto(self, url, *, wait_until, timeout):
            self.url = url
            calls.append(("goto", url, wait_until, timeout))

        def locator(self, selector):
            calls.append(("assistant-selector", selector))
            return Assistant(self)

        def get_by_role(self, role, *, name, exact):
            calls.append(("page-role", role, name, exact))
            return Button(self, name)

        def on(self, event, listener):
            self.listeners.setdefault(event, []).append(listener)

        def remove_listener(self, event, listener):
            listeners = self.listeners.get(event, [])
            if listener in listeners:
                listeners.remove(listener)

        async def wait_for_url(self, predicate, *, wait_until, timeout):
            calls.append(("wait-url", wait_until, timeout))
            assert predicate(self.url)

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()

    class Context:
        def __init__(self):
            self.pages = []

        async def new_page(self):
            self.pages.append(page)
            return page

    class Client:
        def __init__(self):
            self.binding = PageBinding("ui-page", "alpha-plan")
            self.page = page

        async def wait_until_clean_ready(self, *, timeout_ms):
            calls.append(("clean-ready", timeout_ms))

        async def bind_task_identity(self, task_id, team):
            calls.append(("bind-task", task_id, team))

        async def assert_ownership(self):
            return SimpleNamespace(
                page_id="ui-page",
                page_role="alpha-plan",
                page_task_id=state["task_id"],
                page_team=state["team"],
                composer_text="",
                composer_present=True,
                composer_editable=True,
                stop_visible=False,
                blocking_dialogs=(),
                attachment_markers=(),
                state=ChatGPTState.NEW_CHAT,
                requires_login=False,
                url=page.url,
            )

    class Workspace:
        async def bind(self, role, bound_page, *, timeout_ms, force_new_page_id):
            calls.append(("workspace-bind", role, timeout_ms, force_new_page_id))
            assert bound_page is page
            return Client()

    monkeypatch.setattr(worker_module, "ChatGPTWorkspace", Workspace)
    donor = {
        "conversation_id": anchor["conversation_id"],
        "assistant_message_id": anchor["terminal_assistant_message_id"],
    }
    context = Context()
    acquired = asyncio.run(
        worker._branch_from_bootstrap_ui(
            state,
            "PLAN",
            worker_module.CDPATabActions(context, config),
            donor,
        )
    )

    assert acquired.page_id == "ui-page"
    assert acquired.new_chat is True
    assert (
        "assistant-selector",
        f'[data-message-author-role="assistant"][data-message-id="{donor["assistant_message_id"]}"]',
    ) in calls
    assert ("turn-role", "button", "More actions", True) in calls
    assert ("page-role", "menuitem", "Branch in new chat", True) in calls
    assert ("workspace-bind", "alpha-plan", 15000, True) in calls
    assert ("bind-task", state["task_id"], state["team"]) in calls


def test_ui_bootstrap_branch_rejects_eventual_source_alias_before_task_bind(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = bootstrap_record()
    state = store.create_task(
        "ui alias",
        requested_team="ui-alias",
        task_id="task-ui-alias",
        bootstrap=anchor,
    )
    worker = CDPAWorker(config, store=store)
    bind_calls = []

    class BranchResponse:
        url = "https://chatgpt.com/backend-api/conversation/new_branch"
        status = 200

        async def json(self):
            return {"conversation": {"conversation_id": anchor["conversation_id"]}}

    class ClickTarget:
        def __init__(self, page, label):
            self.page = page
            self.label = label

        async def click(self):
            if self.label == "Branch in new chat":
                self.page.url = "https://chatgpt.com/c/WEB:temporary-branch"
                for listener in tuple(self.page.listeners.get("response", ())):
                    listener(BranchResponse())

    class Turn:
        async def hover(self):
            return None

        def get_by_role(self, _role, *, name, exact):
            assert exact is True
            return ClickTarget(page, name)

    class Assistant:
        first = None

        def __init__(self):
            self.first = self

        async def wait_for(self, **_kwargs):
            return None

        def locator(self, _selector):
            return Turn()

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.closed = False
            self.wait_calls = 0
            self.listeners = {}

        async def goto(self, url, **_kwargs):
            self.url = url

        def locator(self, _selector):
            return Assistant()

        def get_by_role(self, _role, *, name, exact):
            assert exact is True
            return ClickTarget(self, name)

        def on(self, event, listener):
            self.listeners.setdefault(event, []).append(listener)

        def remove_listener(self, event, listener):
            listeners = self.listeners.get(event, [])
            if listener in listeners:
                listeners.remove(listener)

        async def wait_for_url(self, predicate, **_kwargs):
            self.wait_calls += 1
            if self.wait_calls > 1:
                self.url = f"https://chatgpt.com/c/{anchor['conversation_id']}"
            if not predicate(self.url):
                raise TimeoutError("URL predicate did not match")

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()

    class Context:
        def __init__(self):
            self.pages = []

        async def new_page(self):
            self.pages.append(page)
            return page

    class Client:
        def __init__(self):
            self.binding = PageBinding("ui-alias-page", "ui-alias-plan")
            self.bound = False
            self.page = page

        async def wait_until_clean_ready(self, **_kwargs):
            return None

        async def bind_task_identity(self, task_id, team):
            bind_calls.append((task_id, team))
            self.bound = True

        async def assert_ownership(self):
            return SimpleNamespace(
                page_id="ui-alias-page",
                page_role="ui-alias-plan",
                page_task_id=state["task_id"] if self.bound else None,
                page_team=state["team"] if self.bound else None,
                composer_text="",
                composer_present=True,
                composer_editable=True,
                stop_visible=False,
                blocking_dialogs=(),
                attachment_markers=(),
                state=ChatGPTState.NEW_CHAT,
                requires_login=False,
                url=page.url,
            )

    class Workspace:
        async def bind(self, _role, bound_page, **_kwargs):
            assert bound_page is page
            return Client()

    monkeypatch.setattr(worker_module, "ChatGPTWorkspace", Workspace)
    context = Context()
    actions = worker_module.CDPATabActions(context, config)
    donor = {
        "conversation_id": anchor["conversation_id"],
        "assistant_message_id": anchor["terminal_assistant_message_id"],
    }

    with pytest.raises(worker_module.BootstrapUIBranchError, match="source|donor|alias"):
        asyncio.run(worker._branch_from_bootstrap_ui(state, "PLAN", actions, donor))

    assert bind_calls == []
    assert page.closed is True


def test_pre_send_limits_allowed_routes_to_selected_workflow_roles(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, roles=("PLAN", "REVIEW"))
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    envelope, _ = json.JSONDecoder().raw_decode(
        hop["prompt"].removeprefix("alpha · role: plan\n")
    )
    assert envelope["allowed-routes"] == ["PLAN", "REVIEW", "PAUSE", "DONE"]
    assert list(state["roles"]) == ["PLAN", "REVIEW"]


def test_normal_cdpa_send_keeps_transport_identity_out_of_actual_payload(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    client = RecordingCDPASendClient()

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert client.send_calls == [hop["prompt"]]
    assert client.send_calls[0].startswith("alpha · role: plan\n{")
    assert hop["receipt"]["prompt"] == hop["prompt"]
    assert record is not None
    assert record.rendered_prompt == hop["prompt"]
    for forbidden in (
        "ROLE_REQUEST_ID",
        hop["request_id"],
        hop["ledger_path"],
        state["manifest_path"],
        hop["expected_report_path"],
        "controller_id",
        "run_id",
        "prompt_sha256",
        "created_at",
        "updated_at",
        "page_id",
    ):
        assert forbidden not in client.send_calls[0]
        assert forbidden not in hop["receipt"]["prompt"]




def test_reused_cdpa_role_waits_for_hydrated_history_before_send(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-reused-history")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    state["hops"].insert(
        0,
        {
            "hop_id": 0,
            "target_role": "PLAN",
            "receipt": {"accepted_via": "user_message_identity"},
        },
    )
    history = (
        MessageSnapshot("user", "u-old", "t-old", "older prompt", ()),
        MessageSnapshot("assistant", "a-old", "t-old", "older answer", ()),
    )

    class HydratingClient(RecordingCDPASendClient):
        def __init__(self):
            super().__init__(task_id=state["task_id"], team=state["team"])
            self.prompt_reads = 0
            self.require_history = []

        async def assert_ownership(self):
            if self.current.composer_text and not self.current.messages:
                self.prompt_reads += 1
                if self.prompt_reads >= 2:
                    self.current = send_snapshot(
                        text=self.current.composer_text,
                        messages=history,
                        state=ChatGPTState.DRAFT,
                        task_id=state["task_id"],
                        team=state["team"],
                    )
            return self.current

        async def send(
            self,
            text,
            *,
            require_existing_conversation_baseline=False,
            **kwargs,
        ):
            self.require_history.append(require_existing_conversation_baseline)
            return await super().send(text, **kwargs)

    client = HydratingClient()

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert record is not None and record.baseline is not None
    assert record.baseline.message_ids == frozenset({"u-old", "a-old"})
    assert client.require_history == [True]
    assert len(client.send_calls) == 1


def test_reused_cdpa_role_persistent_empty_history_blocks_retryably_preboundary(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(tmp_path, task_id="task-reused-history-empty")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    state["hops"].insert(
        0,
        {
            "hop_id": 0,
            "target_role": "PLAN",
            "receipt": {"accepted_via": "user_message_identity"},
        },
    )
    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "conversation_transcript_not_ready"
    assert state["block_retryable"] is True
    assert record is not None and record.status is RequestStatus.PROMPT_SET
    assert record.attempts == 0
    assert record.baseline is None
    assert client.send_calls == []


def test_missing_local_file_report_enters_route_repair_without_advancing(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-missing-local-report"
    )
    handoff = str(hop["expected_report_path"])
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert "report" in repair["validation_error"].lower()
    assert "missing" in repair["validation_error"].lower()
    assert state["reports"] == []
    assert not any(
        item.get("target_role") == "DEV" and item.get("kind") == "handoff"
        for item in state["hops"]
    )


def test_empty_local_file_report_enters_route_repair_without_advancing(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-empty-local-report"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(b"")
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert "report" in repair["validation_error"].lower()
    assert "empty" in repair["validation_error"].lower()
    assert state["reports"] == []


def test_local_file_report_routes_once_with_hash_and_size_evidence(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-valid-local-report"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    data = b"# DEV report\n\nVerified local artifact.\n"
    report.write_bytes(data)
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["state"] == "routed"
    assert hop["route"] == "DEV"
    assert hop["report_path"] == handoff
    assert hop["report_sha256"] == worker_module.hashlib.sha256(data).hexdigest()
    assert hop["report_size"] == len(data)
    assert child["target_role"] == "DEV"
    assert child["handoff"] == handoff
    assert state["reports"][-1]["sha256"] == hop["report_sha256"]
    assert state["reports"][-1]["size"] == len(data)


def test_mismatched_local_file_report_path_enters_route_repair(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-mismatched-local-report"
    )
    handoff = (
        f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_"
        "wrong-task-id.md"
    )
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# wrong report\n", encoding="utf-8")
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert "expected role report" in repair["validation_error"].lower()
    assert state["reports"] == []


def test_symlink_local_file_report_enters_route_repair(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-symlink-local-report"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-report.md"
    outside.write_text("# outside\n", encoding="utf-8")
    report.symlink_to(outside)
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert "symlink" in repair["validation_error"].lower()
    assert state["reports"] == []



def test_unselected_route_enters_safe_repair_without_recording_report(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-unselected-route",
        roles=("PLAN", "REVIEW"),
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = (
        '{"route":"DEV","handoff":"' + str(hop["expected_report_path"]) + '"}'
    )
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert "not selected" in repair["validation_error"]
    assert state["reports"] == []
    assert not (tmp_path / hop["expected_report_path"]).exists()


def test_malformed_response_creates_same_turn_guide_only_repair(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = "not json"
    hop["state"] = "responded"

    worker._responded(state, hop)
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert repair["turn"] == 1
    assert repair["repair_attempt"] == 1

    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert "Implement exact production behavior" not in repair["prompt"]
    assert "Validation error:" in repair["prompt"]
    assert ".plan/alpha/alpha-plan_turn1_task-a.md" not in repair["prompt"]
    assert ".plan/<team>/<physical-role>_turn<N>_<task-id>.md" in repair["prompt"]
    assert "Constructor for PLAN" not in repair["prompt"]
    assert repair["handoff"] == "route repair"
















def test_only_plan_done_can_make_task_terminal(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("final evidence", encoding="utf-8")
    hop["response"] = '{"route":"DONE","handoff":".plan/alpha/alpha-plan_turn1_task-a.md"}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert state["active_hop_id"] is None
    assert state["kanban_column"] == "DONE_STOPPED"


def test_resume_and_retry_controls_have_distinct_state_contracts(tmp_path: Path):
    _, _, paused, worker = setup_task(tmp_path, task_id="task-paused")
    paused["status"] = "PAUSED"
    paused["kanban_column"] = "PAUSED"
    paused["resume_column"] = "PLANNING"
    paused["pause_reason"] = "manual"
    paused_request_id = _active_hop(paused)["request_id"]
    paused["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(paused, FakeActions())) is True
    assert paused["status"] == "RUNNING"
    assert paused["kanban_column"] == "PLANNING"
    assert paused["controls"][0]["status"] == "recovering"
    assert paused["controls"][0]["command_state"] == "RUNNING"
    assert paused["controls"][0]["applied_at"] is None
    assert paused["controls"][0]["result"]["outcome"] == "recovering"
    assert paused["controls"][0]["result"]["before"]["active_request_id"] == paused_request_id
    assert _active_hop(paused)["request_id"] == paused_request_id

    _, _, blocked, worker = setup_task(tmp_path, task_id="task-blocked")
    blocked["status"] = "BLOCKED"
    blocked["kanban_column"] = "BLOCKED"
    blocked["block_reason"] = "validation failed"
    blocked_hop = _active_hop(blocked)
    blocked_hop["repair_attempt"] = 2
    blocked_request_id = blocked_hop["request_id"]
    blocked["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(blocked, FakeActions())) is True
    assert blocked["status"] == "RUNNING"
    assert blocked["kanban_column"] == "PLANNING"
    assert blocked["block_reason"] is None
    assert blocked["controls"][0]["status"] == "recovering"
    assert blocked["controls"][0]["command_state"] == "RUNNING"
    assert blocked["controls"][0]["applied_at"] is None
    assert blocked["controls"][0]["result"]["outcome"] == "recovering"
    assert blocked["controls"][0]["result"]["before"]["active_request_id"] == blocked_request_id
    assert _active_hop(blocked)["repair_attempt"] == 2
    assert _active_hop(blocked)["request_id"] == blocked_request_id

    _, _, retried, worker = setup_task(tmp_path, task_id="task-retry")
    retried["status"] = "BLOCKED"
    retried["kanban_column"] = "BLOCKED"
    retried["block_code"] = "route_validation_exhausted"
    retried["block_retryable"] = True
    retried["block_reason"] = "route repair exhausted"
    retry_hop = _active_hop(retried)
    retry_hop["validation_error"] = "bad route"
    retry_hop["repair_attempt"] = worker.config.route_repair_attempts
    retried["controls"] = [
        {
            "control_id": 1,
            "action": "retry",
            "role": "PLAN",
            "status": "requested",
        }
    ]
    assert asyncio.run(worker._apply_control(retried, FakeActions())) is True
    assert retried["status"] == "RUNNING"
    assert retry_hop["repair_attempt"] == worker.config.route_repair_attempts - 1








































def test_open_tab_recovers_presend_role_offline_and_sends_original_once(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-presend-recovery"
    )
    path = Path(state["manifest_path"])
    worker.runtime_db.ensure_schema()
    worker.runtime_db.put_snapshot("settings", {"dom_only": True})
    hop = _active_hop(state)
    original_hop_id = hop["hop_id"]
    original_request_id = hop["request_id"]
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
        active_action="blocked",
    )
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/owned-plan",
        online=False,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact owned PLAN tab",
            "confirmed": False,
            "status": "requested",
            "requested_at": "2026-07-23T00:00:00+00:00",
            "applied_at": None,
            "result": None,
        }
    ]
    store.save(path, state)
    sent_prompts: list[str] = []

    class RecoveryClient:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/owned-plan",
                url="https://chatgpt.com/c/owned-plan",
            )

    client = RecoveryClient()
    acquired = AcquiredRole(
        client=client,
        page_id="recovered-page",
        url="https://chatgpt.com/c/owned-plan",
        created=False,
        new_chat=False,
    )

    class RecoveryActions:
        def __init__(self):
            self.recovered = False

        async def locate_owned(self, _state, _role):
            return acquired if self.recovered else None

        async def reopen(
            self,
            _state,
            _role,
            *,
            require_clean_ready=True,
            foreground=True,
        ):
            assert require_clean_ready is True
            assert foreground is True
            self.recovered = True
            return acquired

        async def open_tab(self, _acquired):
            self.recovered = True

        async def acquire(self, _state, _role):
            assert self.recovered is True
            return acquired

    actions = RecoveryActions()
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: actions,
    )

    class OneSendBlock:
        def __init__(self, prompt, **_kwargs):
            self.prompt = prompt

        async def run(self, _context):
            sent_prompts.append(self.prompt)
            receipt = SendReceipt(
                prompt=self.prompt,
                prompt_sha256=prompt_digest(self.prompt),
                binding=PageBinding("recovered-page", "alpha-plan"),
                baseline=MessageBaseline(
                    frozenset(), frozenset(), frozenset(), frozenset()
                ),
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before="owned-plan",
                conversation_id="owned-plan",
                user_message_id="user-1",
                user_turn_id="turn-1",
            )
            return {
                "receipt": receipt.to_dict(),
                "record": {
                    "accepted_at": datetime.now(timezone.utc).timestamp(),
                },
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", OneSendBlock)
    context = SimpleNamespace(pages=[])

    recovered = asyncio.run(worker.advance(path, context))
    recovered_hop = _active_hop(recovered)
    assert recovered["status"] == "RUNNING"
    assert recovered["kanban_column"] == "PLANNING"
    assert recovered["block_code"] is None
    assert recovered["block_reason"] is None
    assert recovered["roles"]["PLAN"]["online"] is True
    assert recovered["roles"]["PLAN"]["page_id"] == "recovered-page"
    assert recovered_hop["hop_id"] == original_hop_id
    assert recovered_hop["request_id"] == original_request_id
    assert recovered_hop["state"] == "pre_send"
    assert recovered["controls"][0]["status"] == "applied"
    assert recovered["controls"][0]["result"]["resumed"] is True
    assert sent_prompts == []

    prepared = asyncio.run(worker.advance(path, context))
    assert _active_hop(prepared)["state"] == "sending"
    assert _active_hop(prepared)["request_id"] == original_request_id
    assert sent_prompts == []

    sent = asyncio.run(worker.advance(path, context))
    assert _active_hop(sent)["state"] == "sent"
    assert _active_hop(sent)["request_id"] == original_request_id
    assert len(sent["hops"]) == 1
    assert len(sent_prompts) == 1

    waiting = asyncio.run(worker.advance(path, context))
    waiting_hop = _active_hop(waiting)
    assert waiting_hop["state"] == "waiting"
    assert waiting_hop["request_id"] == original_request_id
    assert len(sent_prompts) == 1

    worker._waiting_dom = AsyncMock()
    asyncio.run(worker._waiting(waiting, waiting_hop, actions, path))
    worker._waiting_dom.assert_awaited_once()
    assert len(sent_prompts) == 1


def test_open_tab_recovers_waiting_accepted_send_on_exact_conversation_without_resend(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-waiting-accepted"
    )
    hop = _active_hop(state)
    conversation_url = "https://chatgpt.com/c/owned-plan"
    original_request_id = hop["request_id"]
    receipt = {
        "prompt": "accepted prompt",
        "prompt_sha256": "digest",
        "binding": {"page_id": "closed-page", "role": "alpha-plan"},
        "accepted_via": "user_message_identity",
        "user_message_id": "accepted-user-message",
        "user_turn_id": "accepted-user-turn",
    }
    hop.update(
        state="waiting",
        conversation_url=conversation_url,
        receipt=receipt,
    )
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url=conversation_url,
        online=False,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact accepted waiting conversation",
            "status": "requested",
        }
    ]

    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="closed-page",
        url=conversation_url,
        created=True,
        new_chat=False,
    )

    class ExactWaitingRecovery:
        def __init__(self):
            self.reopen_clean = []

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, require_clean_ready=True, **_kwargs):
            self.reopen_clean.append(require_clean_ready)
            if require_clean_ready:
                raise RuntimeError("accepted waiting has no clean editable composer")
            return acquired

    actions = ExactWaitingRecovery()

    assert asyncio.run(worker._apply_control(state, actions)) is True
    assert actions.reopen_clean == [False]
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["roles"]["PLAN"]["page_id"] == "closed-page"
    assert state["roles"]["PLAN"]["page_url"] == conversation_url
    assert hop["state"] == "waiting"
    assert hop["request_id"] == original_request_id
    assert hop["receipt"] == receipt
    assert state["controls"][0]["status"] == "applied"
    assert state["controls"][0]["result"] == {
        "page_id": "closed-page",
        "recovered": True,
        "resumed": True,
        "hop_id": hop["hop_id"],
        "request_id": original_request_id,
    }
























def test_open_tab_reconciles_stale_web_command_snapshot_from_exact_ledger(tmp_path: Path):
    from dataclasses import replace
    from playwright_auto.cdpa_commands import WorkerCommand

    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-open-tab-stale-web"
    )
    provisional_url = "https://chatgpt.com/c/WEB:stale-open-tab"
    canonical_id = "canonical-open-tab"
    canonical_url = f"https://chatgpt.com/c/{canonical_id}"
    hop["conversation_url"] = provisional_url
    state["roles"]["PLAN"].update(page_url=provisional_url, online=False)
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    command = WorkerCommand.create(
        origin="operator",
        action="open_tab",
        reason="recover stale provisional conversation",
        state=state,
        role="PLAN",
    )
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id=canonical_id).to_dict(),
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover stale provisional conversation",
            "status": "requested",
            "command": command.to_dict(),
        }
    ]
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id=receipt.binding.page_id,
        url=canonical_url,
        created=True,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.reopen_calls = []

        async def reopen(self, _state, _role, **kwargs):
            self.reopen_calls.append(kwargs)
            return acquired

    actions = Actions()
    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert actions.reopen_calls == [{"require_clean_ready": False, "foreground": True}]
    assert state["controls"][0]["status"] == "applied"
    assert state["status"] == "RUNNING"
    assert hop["receipt"]["conversation_id"] == canonical_id
    assert hop["conversation_url"] == canonical_url
    assert state["roles"]["PLAN"]["page_url"] == canonical_url
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_open_tab_rejects_conflicting_canonical_snapshot_after_ledger_reconcile(tmp_path: Path):
    from dataclasses import replace
    from playwright_auto.cdpa_commands import WorkerCommand

    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-open-tab-canonical-conflict"
    )
    provisional_url = "https://chatgpt.com/c/WEB:stale-conflict"
    hop["conversation_url"] = provisional_url
    state["roles"]["PLAN"].update(page_url=provisional_url, online=False)
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    command_state = json.loads(json.dumps(state))
    command_hop = _active_hop(command_state)
    command_hop["conversation_url"] = "https://chatgpt.com/c/conflicting-canonical"
    command_state["roles"]["PLAN"]["page_url"] = command_hop["conversation_url"]
    command = WorkerCommand.create(
        origin="operator",
        action="open_tab",
        reason="conflicting canonical snapshot",
        state=command_state,
        role="PLAN",
    )
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id="durable-canonical").to_dict(),
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "conflicting canonical snapshot",
            "status": "requested",
            "command": command.to_dict(),
        }
    ]

    class Actions:
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("canonical command conflict must fail before reopening")

    assert asyncio.run(worker._apply_control(state, Actions())) is True
    assert state["controls"][0]["status"] == "ineffective"
    assert "canonical" in str(state["controls"][0]["result"]).lower()
    assert state["status"] == "BLOCKED"
    assert _active_hop(state)["conversation_url"] == provisional_url


def test_advance_persists_ineffective_open_tab_before_later_requested_control(
    tmp_path: Path,
    monkeypatch,
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-btm-like-control-order")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="recorded PLAN tab is offline",
        active_action="blocked",
    )
    state["roles"]["PLAN"].update(
        page_id="old-plan-page",
        page_url="https://chatgpt.com/",
        online=False,
        status="pending",
    )
    hop["conversation_url"] = None
    store.save(path, state)
    worker.hydrate_runtime()

    worker.runtime_db.enqueue_command(
        command_id="cmd-btm-like-open",
        idempotency_key="btm-like-open",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={"action": "open_tab", "role": "PLAN", "reason": None},
    )
    assert worker.dispatch_command_once()["status"] == "running"
    worker.runtime_db.enqueue_command(
        command_id="cmd-btm-like-restart",
        idempotency_key="btm-like-restart",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={
            "action": "restart_role",
            "role": "PLAN",
            "reason": "bookkeeping conversation only",
        },
    )
    assert worker.dispatch_command_once()["status"] == "running"

    requested = store.load(path)
    assert [(item["action"], item["status"]) for item in requested["controls"][-2:]] == [
        ("open_tab", "requested"),
        ("restart_role", "requested"),
    ]

    class ControlActions:
        async def restart(self, *_args, **_kwargs):
            raise RoleOwnershipError("no exact conversation can be safely restarted")

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: ControlActions(),
    )

    result = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=list(worker.registry.tasks_by_id.values()),
        )
    )

    assert result is not None
    saved = store.load(path)
    open_control, restart_control = saved["controls"][-2:]
    assert open_control["status"] == "ineffective"
    assert open_control["command_state"] == "INEFFECTIVE"
    assert "exact saved ChatGPT conversation URL" in str(open_control["result"])
    assert restart_control["status"] == "requested"
    assert restart_control["command_state"] == "PENDING"
    assert worker.runtime_db.get_command("cmd-btm-like-open")["status"] == "failed"
    assert worker.runtime_db.get_command("cmd-btm-like-restart")["status"] == "running"

    second = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=list(worker.registry.tasks_by_id.values()),
        )
    )

    assert second is not None
    final = store.load(path)
    assert final["controls"][-1]["status"] == "rejected"
    assert final["controls"][-1]["command_state"] == "REJECTED"
    assert worker.runtime_db.get_command("cmd-btm-like-restart")["status"] == "failed"


def test_blocked_task_does_not_advance_without_explicit_control(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "manual inspection required"
    store.save(path, state)

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "BLOCKED"
    assert result["active_hop_id"] == 1
    assert _active_hop(result)["state"] == "pre_send"
    assert result["block_reason"] == "manual inspection required"














def _prepare_sent_waiting_task(
    tmp_path: Path,
    *,
    task_id: str,
    report_mode: str = "file",
    repository=None,
):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id=task_id,
        report_mode=report_mode,
        repository=repository,
    )
    from dataclasses import replace
    worker.config = replace(
        worker.config, response_stream_status_terminal_settle_seconds=0.0
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    prompt = hop["prompt"]
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=prompt,
        prompt_sha256=prompt_digest(prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="t1",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=prompt,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    sent_at = datetime.now(timezone.utc)
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = sent_at.isoformat()
    worker._start_wait_budget_from_sent(hop)
    state["roles"]["PLAN"]["page_id"] = "page-alpha-plan"
    state = store.save(path, state)
    hop = _active_hop(state)
    return store, state, worker, path, hop, receipt, sent_at


def _enable_backend_wait_identity(store, state, path, hop, receipt, *, conversation_id="conversation-1"):
    from dataclasses import replace

    enriched = replace(receipt, conversation_id=conversation_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{conversation_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    hop["timestamps"]["sent_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    state = store.save(path, state)
    return state, _active_hop(state), enriched


def test_legacy_receipt_upgrade_advances_wait_persistence_baseline(tmp_path: Path):
    from dataclasses import replace

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-legacy-receipt-persistence-baseline"
    )
    legacy = replace(
        receipt,
        accepted_via="legacy_send_receipt",
        user_message_id=None,
        user_turn_id=None,
    )
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"], receipt=legacy.to_dict()
    )
    hop["receipt"] = legacy.to_dict()
    state = store.save(path, state)
    hop = _active_hop(state)
    persistence_baseline = json.loads(json.dumps(state))

    observed_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    hop["wait"]["continuous_responding_since"] = observed_at
    snapshot = SimpleNamespace(
        messages=(
            MessageSnapshot(
                "user",
                "accepted-user-upgrade",
                "accepted-turn-upgrade",
                receipt.prompt,
                (),
            ),
        )
    )

    upgraded = worker._upgrade_legacy_receipt(
        state,
        hop,
        legacy,
        snapshot,
        path,
        persistence_baseline,
    )

    assert upgraded is not None
    assert upgraded.user_message_id == "accepted-user-upgrade"
    assert upgraded.user_turn_id == "accepted-turn-upgrade"
    persisted = store.load(path)
    assert _active_hop(persisted)["wait"]["continuous_responding_since"] == observed_at
    assert _active_hop(persisted)["receipt"]["user_message_id"] == "accepted-user-upgrade"

    hop["wait"]["activity_length"] = 7
    saved = worker._persist_transport_result(path, persistence_baseline, state)
    assert _active_hop(saved)["wait"]["activity_length"] == 7


def _backend_graph(user_id: str, assistant_id: str, text: str):
    return {
        "current_node": assistant_id,
        "mapping": {
            user_id: {
                "id": user_id,
                "parent": None,
                "message": {
                    "id": user_id,
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["prompt"]},
                },
            },
            assistant_id: {
                "id": assistant_id,
                "parent": user_id,
                "message": {
                    "id": assistant_id,
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": [text]},
                },
            },
        },
    }


def test_listen_dom_streaming_status_is_sparse_and_never_fetches_graph(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stream-primary"
    )
    receipt = replace(receipt, conversation_id="conversation-1")
    worker.config = replace(
        worker.config, response_stream_status_terminal_settle_seconds=5.0
    )
    hop["receipt"] = receipt.to_dict()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    calls = {"status": 0, "graph": 0}

    class Actions:
        async def backend_stream_status(self, conversation_id):
            assert conversation_id == "conversation-1"
            calls["status"] += 1
            return {"status": "IS_STREAMING"}
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("Listen + DOM must never fetch the full conversation graph")

    persisted = []
    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state, hop, Actions(), receipt, persist_transport_state=lambda: persisted.append(True)
        )
    )

    assert (outcome, reason) == ("waiting", None)
    assert calls == {"status": 1, "graph": 0}
    assert hop["wait"]["stream_status_last_status"] == "IS_STREAMING"
    next_poll = worker_module.parse_time(hop["wait"]["stream_status_next_poll_at"])
    assert next_poll is not None
    assert next_poll - datetime.now(timezone.utc) >= timedelta(seconds=29)
    assert persisted


def test_complete_status_settles_then_routes_to_local_dom_without_graph(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-complete-local"
    )
    receipt = replace(receipt, conversation_id="conversation-1")
    worker.config = replace(
        worker.config, response_stream_status_terminal_settle_seconds=5.0
    )
    hop["receipt"] = receipt.to_dict()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    calls = {"status": 0, "graph": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("COMPLETE is only a local reconciliation trigger")

    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state, hop, Actions(), receipt, persist_transport_state=lambda: None
        )
    )
    assert (outcome, reason) == ("waiting", None)
    assert hop["wait"]["completion_mode"] == "terminal_local_settle"
    assert calls == {"status": 1, "graph": 0}

    hop["wait"]["terminal_local_ready_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state, hop, Actions(), receipt, persist_transport_state=lambda: None
        )
    )
    assert (outcome, reason) == ("dom_reconcile", None)
    assert calls == {"status": 1, "graph": 0}


def test_stream_status_preserves_streaming_stop_requested_and_failure_types(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-status-types"
    )
    receipt = replace(receipt, conversation_id="conversation-1")
    hop["receipt"] = receipt.to_dict()

    class Actions:
        def __init__(self, status):
            self.status = status
            self.released = []
        async def backend_stream_status(self, _conversation_id):
            return {"status": self.status}
        def release_backend_stream_status(self, conversation_id):
            self.released.append(conversation_id)

    cases = (
        ("IS_STREAMING", "waiting", None, False),
        ("IS_STOP_REQUESTED", "stream_stopped", "stream_status_stop_requested", True),
        ("FAILURE", "stream_failure", "stream_status_failure", True),
    )
    for status, expected, expected_reason, releases in cases:
        hop["wait"]["completion_mode"] = "stream_status"
        hop["wait"]["stream_status_next_poll_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        actions = Actions(status)
        outcome, reason = asyncio.run(
            worker._waiting_backend_step(
                state, hop, actions, receipt, persist_transport_state=lambda: None
            )
        )
        assert outcome == expected
        assert reason == expected_reason
        assert hop["wait"]["stream_status_last_status"] == status
        assert actions.released == (["conversation-1"] if releases else [])
        if status == "IS_STOP_REQUESTED":
            assert hop["wait"]["completion_mode"] == "stop_requested_local_reconcile"
            assert hop["wait"]["stop_requested_seen_at"]


def test_stop_requested_local_reconcile_mode_does_not_poll_status_again(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stop-requested-no-repoll"
    )
    receipt = replace(receipt, conversation_id="conversation-1")
    hop["receipt"] = receipt.to_dict()
    hop["wait"]["completion_mode"] = "stop_requested_local_reconcile"

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("stopped generation must not re-enter ordinary status polling")

    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state, hop, Actions(), receipt, persist_transport_state=lambda: None
        )
    )

    assert (outcome, reason) == ("stream_stopped", "stream_status_stop_requested")


def test_stop_requested_backend_helper_returns_local_reconcile_signal(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stop-requested-valid-response"
    )
    conversation_id = "11111111-1111-4111-8111-111111111111"
    state, hop, receipt = _enable_backend_wait_identity(
        store, state, path, hop, receipt, conversation_id=conversation_id
    )
    released = []

    class Actions:
        async def backend_stream_status(self, exact_conversation_id):
            assert exact_conversation_id == conversation_id
            return {"status": "IS_STOP_REQUESTED"}

        def release_backend_stream_status(self, exact_conversation_id):
            released.append(exact_conversation_id)

    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state,
            hop,
            Actions(),
            receipt,
            persist_transport_state=lambda: None,
        )
    )

    assert (outcome, reason) == ("stream_stopped", "stream_status_stop_requested")
    assert released == [conversation_id]
    assert hop["wait"]["stream_status_last_status"] == "IS_STOP_REQUESTED"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_stop_requested_backend_helper_does_not_replay_or_fetch_graph(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stop-requested-unresolved"
    )
    conversation_id = "22222222-2222-4222-8222-222222222222"
    state, hop, receipt = _enable_backend_wait_identity(
        store, state, path, hop, receipt, conversation_id=conversation_id
    )
    released = []

    class Actions:
        async def backend_stream_status(self, exact_conversation_id):
            assert exact_conversation_id == conversation_id
            return {"status": "IS_STOP_REQUESTED"}

        def release_backend_stream_status(self, exact_conversation_id):
            released.append(exact_conversation_id)

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("stop helper must not fetch the full graph")

        async def send(self, *_args, **_kwargs):
            raise AssertionError("stop helper must not replay Send")

    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state,
            hop,
            Actions(),
            receipt,
            persist_transport_state=lambda: None,
        )
    )

    assert (outcome, reason) == ("stream_stopped", "stream_status_stop_requested")
    assert released == [conversation_id]
    assert hop["wait"]["stream_status_last_status"] == "IS_STOP_REQUESTED"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_stream_status_error_uses_bounded_local_fallback_without_graph(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-status-recovery"
    )
    receipt = replace(receipt, conversation_id="conversation-1")
    hop["receipt"] = receipt.to_dict()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    calls = {"graph": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            raise worker_module.BackendUnavailableError(503, "stream_status")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("status recovery must not probe the conversation graph")

    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state, hop, Actions(), receipt, persist_transport_state=lambda: None
        )
    )
    assert (outcome, reason) == ("waiting", None)
    assert hop["wait"]["completion_mode"] == "status_recovery"
    assert calls["graph"] == 0

    hop["wait"]["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    hop["wait"]["stream_status_next_poll_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state, hop, Actions(), receipt, persist_transport_state=lambda: None
        )
    )
    assert outcome == "dom_fallback"
    assert reason in {"response_deadline", "status_unavailable"}
    assert calls["graph"] == 0


def test_resume_shares_status_due_slot_instead_of_forcing_poll(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-shared-status-slot"
    )
    receipt = replace(receipt, conversation_id="conversation-1")
    hop["receipt"] = receipt.to_dict()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=20)
    ).isoformat()

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("Resume must not create an extra status poll before the shared due time")

    outcome, reason = asyncio.run(
        worker._waiting_backend_step(
            state, hop, Actions(), receipt, persist_transport_state=lambda: None, resume_recovery=True
        )
    )
    assert (outcome, reason) == ("dom_reconcile", None)


def test_waiting_terminal_local_settle_hands_off_to_existing_dom_path(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-terminal-dom-handoff"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    worker.runtime_db.ensure_schema()
    worker.runtime_db.put_snapshot("settings", {"dom_only": False})
    hop["wait"]["completion_mode"] = "terminal_local_settle"
    hop["wait"]["terminal_local_ready_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    state = store.save(path, state)
    hop = _active_hop(state)
    worker._waiting_dom = AsyncMock()

    class Actions:
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("terminal local handoff must not fetch graph")

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    worker._waiting_dom.assert_awaited_once()


def test_unresolved_complete_accepts_terminal_continuation_before_final_block(
    tmp_path: Path,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-terminal-continuation-arrives"
    )
    state, hop, receipt = _enable_backend_wait_identity(
        store, state, path, hop, receipt
    )
    response = MessageSnapshot(
        "assistant",
        "assistant-terminal",
        "assistant-terminal-turn",
        json.dumps(
            {
                "route": "REVIEW",
                "handoff": ".plan/alpha/alpha-plan_turn1_task-terminal-continuation-arrives.md",
            }
        ),
        (),
    )
    snapshot = send_snapshot(
        messages=(
            MessageSnapshot("user", receipt.user_message_id, receipt.user_turn_id, receipt.prompt, ()),
            response,
        ),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    snapshot = SimpleNamespace(
        **{
            **snapshot.__dict__,
            "composer_empty": True,
            "manual_input_pending": False,
            "response_activity_text": response.text,
            "response_activity_structure": "full",
            "response_activity_turn_id": response.turn_id,
            "response_activity_length": len(response.text),
        }
    )

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            raise AssertionError("a persisted refresh must not be repeated")

    hop["wait"].update(
        completion_mode="dom_fallback",
        backend_fallback_category="graph_not_ready",
        dom_fallback_ready_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
        refresh_count=1,
        terminal_continuation_unresolved={
            "request_id": hop["request_id"],
            "started_at": utc_now(),
            "refresh_baseline": 0,
            "block_ready_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        },
    )

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert state["status"] == "RUNNING"
    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def _mark_completed_refresh(hop: dict, *, seconds_ago: int) -> None:
    finished = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    hop["wait"]["refresh_count"] = int(hop["wait"].get("refresh_count") or 0) + 1
    hop["wait"]["last_refresh_at"] = finished.isoformat()
    hop["wait"]["last_refresh_result"] = {
        "status": "completed",
        "finished_at": finished.isoformat(),
    }


def test_post_refresh_one_minute_checks_response_even_while_stop_is_visible(tmp_path: Path):
    from dataclasses import replace

    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-post-refresh-one-minute"
    )
    _mark_completed_refresh(hop, seconds_ago=61)
    snapshot = replace(
        send_snapshot(task_id=state["task_id"], team=state["team"]),
        stop_visible=True,
    )

    class Client:
        binding = receipt.binding

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/post-refresh-one", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

    worker._final_dom_response_reconciliation = AsyncMock(return_value=True)

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    worker._final_dom_response_reconciliation.assert_awaited_once()


def test_post_refresh_two_minutes_stop_visible_checks_response_then_keeps_waiting(tmp_path: Path):
    from dataclasses import replace

    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-post-refresh-two-minute-stop"
    )
    _mark_completed_refresh(hop, seconds_ago=121)
    snapshot = replace(
        send_snapshot(task_id=state["task_id"], team=state["team"]),
        stop_visible=True,
    )

    class Client:
        binding = receipt.binding

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/post-refresh-two-stop", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

    worker._final_dom_response_reconciliation = AsyncMock(return_value=False)

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    worker._final_dom_response_reconciliation.assert_awaited_once()
    assert state["active_hop_id"] == hop["hop_id"]
    assert state["active_action"] == "wait_response"
    assert hop["state"] == "waiting"


def test_post_refresh_two_minutes_without_stop_never_replays_accepted_request(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-post-refresh-no-replay"
    )
    _mark_completed_refresh(hop, seconds_ago=121)
    original_hop_id = hop["hop_id"]
    original_request_id = hop["request_id"]
    snapshot = send_snapshot(task_id=state["task_id"], team=state["team"])

    class Client:
        binding = receipt.binding

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/post-refresh-no-replay", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

    worker._final_dom_response_reconciliation = AsyncMock(return_value=False)

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    record = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert len(state["hops"]) == 1
    assert state["active_hop_id"] == original_hop_id
    assert hop["state"] == "waiting"
    assert record.status is RequestStatus.SENT
    assert state["active_action"] == "wait_response"


def test_post_refresh_legacy_reroute_marker_still_never_creates_another_send(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-post-refresh-legacy-marker-no-replay"
    )
    _mark_completed_refresh(hop, seconds_ago=121)
    hop["stall_reroute_attempt"] = 3
    original_request_id = hop["request_id"]
    snapshot = send_snapshot(task_id=state["task_id"], team=state["team"])

    class Client:
        binding = receipt.binding

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/post-refresh-legacy", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

    worker._final_dom_response_reconciliation = AsyncMock(return_value=False)

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    record = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert len(state["hops"]) == 1
    assert state["status"] == "RUNNING"
    assert state["active_hop_id"] == hop["hop_id"]
    assert hop["state"] == "waiting"
    assert record.status is RequestStatus.SENT


def test_normal_wait_routes_valid_local_file_response_without_repair(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-normal-wait-path-only"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = b"# TEST report\n\nNormal wait evidence.\n"
    report.write_bytes(report_bytes)
    response = MessageSnapshot(
        "assistant",
        "a-normal-path-only",
        "ta-normal-path-only",
        json.dumps({"route": "TEST", "handoff": handoff}),
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/normal-path-only", False, False
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert hop.get("validation_error") is None

    worker._responded(state, hop)

    child = _active_hop(state)
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert hop["state"] == "routed"
    assert hop["report_path"] == handoff
    assert hop["report_sha256"] == worker_module.hashlib.sha256(report_bytes).hexdigest()
    assert hop["report_size"] == len(report_bytes)
    assert child["kind"] == "handoff"
    assert child["target_role"] == "TEST"
    assert child["handoff"] == handoff
    assert all(item.get("kind") != "route_repair" for item in state["hops"])
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


def test_waiting_with_foreign_durable_history_uses_local_reconciliation_not_graph(
    tmp_path: Path,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-historical-owner"
    )
    conversation_id = "99999999-9999-4999-8999-999999999999"
    state, hop, receipt = _enable_backend_wait_identity(
        store, state, path, hop, receipt, conversation_id=conversation_id
    )
    worker.runtime_db.ensure_schema()
    worker.runtime_db.put_snapshot("settings", {"dom_only": False})
    hop["wait"]["completion_mode"] = "terminal_local_settle"
    hop["wait"]["terminal_local_ready_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    state = store.save(path, state)
    hop = _active_hop(state)
    worker._waiting_dom = AsyncMock()

    class Actions:
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("foreign history must not trigger an automation graph read")

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    worker._waiting_dom.assert_awaited_once()
    current = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert current is not None
    assert current.attempts == 1
    assert current.status is RequestStatus.SENT


def test_waiting_requires_valid_route_report_and_two_samples_before_hop_response(tmp_path: Path):
    from dataclasses import replace

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-valid-gate"
    )
    worker.config = replace(worker.config, response_poll_ms=5000)
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-valid-gate.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("valid report", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-valid",
        "ta-valid",
        '{"route":"TEST","handoff":".plan/alpha/alpha-plan_turn1_task-valid-gate.md"}',
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )

    class Client:
        def __init__(self):
            self.kwargs = None

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.kwargs = kwargs
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert client.kwargs["minimum_samples"] == 2
    assert client.kwargs["poll_ms"] == 5000
    assert client.kwargs["timeout_ms"] >= 11_000
    assert client.kwargs["invalid_grace_ms"] >= 1_000
    assert hop["state"] == "responded"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED
    assert _active_hop(state)["target_role"] == "TEST"


























def test_cdp_disconnect_does_not_convert_inflight_task_to_blocked(tmp_path: Path, monkeypatch):
    store, state, worker, path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-disconnect-preserve"
    )
    before = path.read_bytes()
    TargetClosedError = type("TargetClosedError", (RuntimeError,), {})

    async def disconnected_wait(*_args, **_kwargs):
        raise TargetClosedError("Target page, context or browser has been closed")

    monkeypatch.setattr(worker, "_waiting", disconnected_wait)

    with pytest.raises(TargetClosedError):
        asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert path.read_bytes() == before
    current = store.load(path)
    current_hop = _active_hop(current)
    assert current["status"] == "RUNNING"
    assert current_hop["state"] == "waiting"
    assert current_hop["request_id"] == hop["request_id"]
    assert current.get("block_code") is None









def _inline_response(route="DONE", body="# Inline report\n\nEvidence."):
    return f'{body}\n\n```json\n{{"route":"{route}","handoff":"INLINE"}}\n```'




def test_legacy_inline_response_materializes_once_then_switches_to_file(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-inline-materialize")
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = ".plan/alpha/alpha-plan_turn1_task-inline-materialize.md"
    hop["response"] = _inline_response(route="DEV")
    hop["state"] = "responded"

    worker._responded(state, hop)

    expected = (tmp_path / hop["expected_report_path"]).resolve()
    assert expected.read_text(encoding="utf-8") == "# Inline report\n\nEvidence."
    assert hop["report_path"] == str(expected)
    assert hop["report_size"] == len(expected.read_bytes())
    assert hop["report_sha256"] == worker_module.hashlib.sha256(expected.read_bytes()).hexdigest()
    assert len(state["reports"]) == 1
    assert state["reports"][0]["path"] == str(expected)
    assert state["options"]["report_mode"] == "file"
    next_hop = _active_hop(state)
    assert next_hop["target_role"] == "DEV"
    assert next_hop["handoff"] == hop["expected_report_path"]





















def test_legacy_inline_invalid_route_repairs_in_file_mode(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-inline-repair",
        roles=("PLAN", "REVIEW"),
    )
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = (
        ".plan/alpha/alpha-plan_turn1_task-legacy-inline-repair.md"
    )
    hop["response"] = _inline_response(route="DEV")
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["options"]["report_mode"] == "file"
    assert state["reports"] == []
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert '"handoff":"INLINE"' not in repair["prompt"]
    assert repair["expected_report_path"] == hop["expected_report_path"]


def test_pre_send_with_legacy_receipt_fails_closed_without_rebuilding_prompt(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-receipt"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-pre-send-legacy-receipt",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    original_prompt = "legacy accepted file prompt"
    receipt = SendReceipt(
        prompt=original_prompt,
        prompt_sha256=prompt_digest(original_prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="legacy_receipt",
        session_id_before=None,
        user_message_id=None,
        user_turn_id=None,
    )
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    hop["receipt"] = receipt.to_dict()
    state = store.save(path, state)
    hop = _active_hop(state)

    class NoActions:
        async def acquire(self, _state, _role):
            raise AssertionError("pre_send receipt conflict must not acquire a tab")

    asyncio.run(worker._pre_send(state, hop, NoActions()))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "pre_send_request_recovery_required"
    assert state["options"]["report_mode"] == "file"
    assert hop["state"] == "pre_send"
    assert hop["prompt"] == original_prompt
    assert hop["receipt"] == receipt.to_dict()
    assert hop["receipt"]["attempts"] == 1
    assert not Path(hop["ledger_path"]).exists()


def test_pre_send_with_durable_record_fails_closed_without_rebuilding_prompt(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-durable-record"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-pre-send-durable-record",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    original_prompt = "durable accepted file prompt"
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    binding = PageBinding("page-alpha-plan", "alpha-plan")
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=original_prompt,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=binding,
        baseline=baseline,
    )
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    state = store.save(path, state)
    hop = _active_hop(state)

    class NoActions:
        async def acquire(self, _state, _role):
            raise AssertionError("pre_send ledger conflict must not acquire a tab")

    asyncio.run(worker._pre_send(state, hop, NoActions()))

    current = ledger.get(record.request_id)
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "pre_send_request_recovery_required"
    assert state["options"]["report_mode"] == "file"
    assert hop["state"] == "pre_send"
    assert hop["prompt"] == original_prompt
    assert hop.get("receipt") is None
    assert current is not None
    assert current.rendered_prompt == original_prompt
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1


def test_legacy_inline_pre_send_switches_to_file_before_prompt(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-pre-send"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-pre-send",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    state["options"]["report_mode"] = "inline"
    state = store.save(path, state)
    baseline = json.loads(json.dumps(state))
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert state["options"]["report_mode"] == "file"
    assert "Report: .plan/<team>/<physical-role>_turn<N>_<task-id>.md" in hop["prompt"]
    assert '"handoff":"INLINE"' not in hop["prompt"]
    assert hop["expected_report_path"] == ".plan/alpha/alpha-plan_turn1_task-legacy-pre-send.md"
    persisted = worker._persist_transport_result(path, baseline, state)
    assert persisted["options"]["report_mode"] == "file"
    assert _active_hop(persisted)["prompt"] == hop["prompt"]


def test_accepted_legacy_inline_hop_drains_once_to_execution_repo_then_file(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-accepted"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-accepted-inline",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = (
        ".plan/alpha/alpha-plan_turn1_task-legacy-accepted-inline.md"
    )
    original_request_id = hop["request_id"]
    original_prompt = "legacy accepted inline prompt with handoff INLINE"
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=original_prompt,
        prompt_sha256=prompt_digest(original_prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="t1",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=original_prompt,
        request_id=original_request_id,
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = datetime.now(timezone.utc).isoformat()
    worker._start_wait_budget_from_sent(hop)
    state["roles"]["PLAN"]["page_id"] = "page-alpha-plan"
    state = store.save(path, state)
    hop = _active_hop(state)

    body = "# Legacy accepted inline\n\nMaterialize exactly once."
    response = MessageSnapshot(
        "assistant",
        "a-legacy-inline",
        "ta-legacy-inline",
        _inline_response(route="DEV", body=body),
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", original_prompt, ()),),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/legacy-inline",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    waiting_baseline = json.loads(json.dumps(state))
    asyncio.run(worker._waiting(state, hop, Actions(), path))
    state = worker._persist_transport_result(path, waiting_baseline, state)
    hop = _active_hop(state)
    assert hop["state"] == "responded"
    assert state["options"]["report_mode"] == "inline"

    responded_baseline = json.loads(json.dumps(state))
    worker._responded(state, hop)
    state = worker._persist_transport_result(path, responded_baseline, state)
    completed_hop = state["hops"][0]
    report_path = Path(completed_hop["report_path"])

    assert report_path == (
        execution_repository
        / ".plan"
        / "alpha"
        / "alpha-plan_turn1_task-legacy-accepted-inline.md"
    ).resolve()
    assert report_path.read_text(encoding="utf-8") == body
    assert not (
        tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-legacy-accepted-inline.md"
    ).exists()
    assert completed_hop["prompt"] == original_prompt
    assert completed_hop["request_id"] == original_request_id
    assert completed_hop["receipt"] == receipt.to_dict()
    original_record = RequestLedger(completed_hop["ledger_path"]).get(original_request_id)
    assert original_record is not None
    assert original_record.attempts == 1
    assert original_record.status is RequestStatus.COMPLETED
    assert state["options"]["report_mode"] == "file"

    next_hop = _active_hop(state)
    assert next_hop["target_role"] == "DEV"
    asyncio.run(worker._pre_send(state, next_hop, FakeActions()))
    assert state["options"]["report_mode"] == "file"
    assert '"handoff":"INLINE"' not in next_hop["prompt"]
    assert next_hop["expected_report_path"] == (
        ".plan/alpha/alpha-dev_turn1_task-legacy-accepted-inline.md"
    )
    final_record = RequestLedger(completed_hop["ledger_path"]).get(original_request_id)
    assert final_record is not None
    assert final_record.attempts == 1
    assert final_record.status is RequestStatus.COMPLETED


def test_accepted_legacy_inline_conflict_recovers_same_response_without_replay(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-conflict"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-inline-conflict",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = (
        ".plan/alpha/alpha-plan_turn1_task-legacy-inline-conflict.md"
    )
    original_request_id = hop["request_id"]
    original_prompt = "legacy accepted inline conflict prompt with handoff INLINE"
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    receipt = SendReceipt(
        prompt=original_prompt,
        prompt_sha256=prompt_digest(original_prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u-conflict",
        user_turn_id="t-conflict",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=original_prompt,
        request_id=original_request_id,
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=receipt.baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    body = "# Legacy accepted inline conflict\n\nAccepted report body."
    response_text = _inline_response(route="DEV", body=body)
    response = MessageSnapshot(
        "assistant",
        "a-conflict",
        "ta-conflict",
        response_text,
        (),
    )
    hop["receipt"] = receipt.to_dict()
    hop["response"] = response_text
    hop["response_sha256"] = worker_module.hashlib.sha256(
        response_text.encode("utf-8")
    ).hexdigest()
    hop["response_record"] = response.to_dict()
    hop["message_identity"] = {
        "message_id": response.message_id,
        "turn_id": response.turn_id,
    }
    hop["state"] = "responded"
    state = store.save(path, state)
    hop = _active_hop(state)

    report_path = (execution_repository / hop["expected_report_path"]).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_bytes = b"# Orphan steering artifact\n\nDifferent unowned bytes.\n"
    report_path.write_bytes(orphan_bytes)

    worker._responded(state, hop)
    state = store.save(path, state)
    blocked_hop = _active_hop(state)
    original_record = RequestLedger(blocked_hop["ledger_path"]).get(original_request_id)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert report_path.read_bytes() == orphan_bytes
    assert len(state["hops"]) == 1
    assert blocked_hop["state"] == "responded"
    assert blocked_hop["request_id"] == original_request_id
    assert blocked_hop["prompt"] == original_prompt
    assert blocked_hop["receipt"] == receipt.to_dict()
    assert original_record is not None
    assert original_record.attempts == 1
    assert original_record.status is RequestStatus.SENT
    mode_after_conflict = state["options"]["report_mode"]

    archive_path = execution_repository / ".recovery-orphans" / "legacy-inline-conflict.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.replace(archive_path)
    report_path.write_text(body, encoding="utf-8")

    recovered = store.load(path)
    recovered_hop = _active_hop(recovered)
    worker._responded(recovered, recovered_hop)
    recovered = store.save(path, recovered)
    completed_hop = recovered["hops"][0]
    completed_record = RequestLedger(completed_hop["ledger_path"]).get(original_request_id)

    assert mode_after_conflict == "inline"
    assert archive_path.read_bytes() == orphan_bytes
    assert Path(completed_hop["report_path"]) == report_path
    assert report_path.read_text(encoding="utf-8") == body
    assert completed_hop["state"] == "routed"
    assert completed_hop["route"] == "DEV"
    assert completed_hop["request_id"] == original_request_id
    assert completed_hop["prompt"] == original_prompt
    assert completed_hop["receipt"] == receipt.to_dict()
    assert completed_record is not None
    assert completed_record.attempts == 1
    assert completed_record.status is RequestStatus.COMPLETED
    assert recovered["options"]["report_mode"] == "file"
    assert len(recovered["hops"]) == 2
    next_hop = _active_hop(recovered)
    assert next_hop["kind"] == "handoff"
    assert next_hop["target_role"] == "DEV"


def test_cross_workspace_local_file_report_requires_execution_repository_artifact(tmp_path: Path):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-worker-execution"
    execution_repository.mkdir()
    store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-cross-workspace-file",
        repository=execution_repository,
    )
    original_request_id = hop["request_id"]
    handoff = str(hop["expected_report_path"])
    report = execution_repository / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = b"# PLAN report\n\nCross-workspace local evidence.\n"
    report.write_bytes(report_bytes)
    response = MessageSnapshot(
        "assistant",
        "a-cross-workspace",
        "ta-cross-workspace",
        json.dumps({"route": "DEV", "handoff": handoff}),
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/cross-workspace",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    assert state["options"]["report_mode"] == "file"
    assert '"handoff":"INLINE"' not in hop["prompt"]
    assert report.exists()
    assert not (tmp_path / handoff).exists()
    assert RequestLedger(hop["ledger_path"]).get(original_request_id).attempts == 1

    asyncio.run(worker._waiting(state, hop, Actions(), Path(state["manifest_path"])))
    assert hop["state"] == "responded"
    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["report_path"] == handoff
    assert hop["report_size"] == len(report_bytes)
    assert hop["report_sha256"] == worker_module.hashlib.sha256(report_bytes).hexdigest()
    assert child["target_role"] == "DEV"
    assert child["handoff"] == handoff
    assert hop["request_id"] == original_request_id
    record = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert record.attempts == 1
    assert record.status is RequestStatus.COMPLETED
    assert all(item.get("kind") != "route_repair" for item in state["hops"])

    persisted = store.save(state["manifest_path"], state)
    assert persisted["active_role"] == "DEV"
    assert len(persisted["hops"]) == 2
    assert persisted["hops"][0]["request_id"] == original_request_id
    assert persisted["hops"][0]["report_path"] == handoff
    assert persisted["hops"][1]["handoff"] == handoff


class ExplodingBrowserContext:
    @property
    def pages(self):
        raise AssertionError("dependency WAITING/release must not inspect browser pages")


def test_dependency_waiting_does_not_construct_browser_actions_or_rewrite(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    path = Path(child["manifest_path"])
    before = path.read_bytes()
    before_events = list(child["dependency_events"])

    result = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))

    assert result["status"] == "WAITING"
    assert result["waiting"]["waiting_on"] == ["task-parent"]
    assert result["hops"][0]["state"] == "pre_send"
    assert path.read_bytes() == before
    assert result["dependency_events"] == before_events


def test_dependency_release_is_one_durable_transition_without_browser_action(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )

    path = Path(child["manifest_path"])
    result = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))
    assert result["status"] == "INBOX"
    assert result["kanban_column"] == "INBOX"
    assert result["active_action"] == "queued"
    assert result["waiting"] == {
        "reason": None,
        "waiting_on": [],
        "stopped": [],
        "missing": [],
        "since": None,
    }
    assert result["hops"][0]["state"] == "pre_send"
    assert result["active_hop_id"] == 1
    assert result["dependency_events"][-1]["status"] == "RELEASED"

    persisted = path.read_bytes()
    # The next poll would be allowed to acquire PLAN, so only verify the release
    # transition itself is stable through a worker restart/load.
    restarted = CDPAWorker(config, store=store)
    loaded = store.load(path)
    assert loaded["status"] == "INBOX"
    assert path.read_bytes() == persisted
    assert restarted.store is store




def test_same_team_queue_releases_oldest_ready_task_before_browser_access(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued_b = store.create_task("queued b", reuse_team="alpha", task_id="task-b")
    queued_a = store.create_task("queued a", reuse_team="alpha", task_id="task-a")
    same_time = "2026-07-23T00:00:00+00:00"
    for queued in (queued_a, queued_b):
        store.update(
            queued["manifest_path"],
            lambda state: {
                **state,
                "created_at": same_time,
                "queue": {**state["queue"], "enqueued_at": same_time},
            },
        )
    store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )
    worker = CDPAWorker(config, store=store)

    later = asyncio.run(worker.advance(queued_b["manifest_path"], ExplodingBrowserContext()))
    first = asyncio.run(worker.advance(queued_a["manifest_path"], ExplodingBrowserContext()))

    assert later["status"] == "WAITING"
    assert later["waiting"]["blocked_by_task_id"] == "task-a"
    assert first["status"] == "INBOX"
    assert first["queue"]["released_at"]
    assert first["reusable_teams"] == ["alpha"]

    still_waiting = asyncio.run(
        worker.advance(queued_b["manifest_path"], ExplodingBrowserContext())
    )
    assert still_waiting["status"] == "WAITING"
    assert still_waiting["waiting"]["blocked_by_task_id"] == "task-a"

    store.update(
        queued_a["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )
    released = asyncio.run(
        worker.advance(queued_b["manifest_path"], ExplodingBrowserContext())
    )
    assert released["status"] == "INBOX"
    assert released["queue_events"][-1]["status"] == "RELEASED"





















































def test_cdpa_uploads_once_per_role_conversation_generation(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "context.md"
    attachment.write_text("stable context", encoding="utf-8")
    state = store.create_task(
        "Analyze context",
        requested_team="alpha",
        task_id="task-upload-generation",
        upload_paths=[attachment],
    )
    worker = CDPAWorker(config, store=store)
    captured_files: list[tuple[str, ...]] = []

    class Client:
        binding = PageBinding("page-alpha-plan", "alpha-plan")

        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/upload",
                url="https://chatgpt.com/c/upload",
            )

    client = Client()

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload",
                created=False,
                new_chat=False,
            )

    class FakeDurableSendBlock:
        def __init__(self, _prompt, *, files=(), **_kwargs):
            captured_files.append(tuple(str(path) for path in files))

        async def run(self, _context):
            return {
                "receipt": {"prompt_sha256": "a" * 64},
                "record": {"accepted_at": datetime.now(timezone.utc).timestamp()},
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeDurableSendBlock)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    assert str(attachment.resolve()) not in str(hop["prompt"])
    assert "stable context" not in str(hop["prompt"])
    asyncio.run(worker._sending(state, hop, Actions()))

    assert captured_files == [(str(attachment.resolve()),)]
    assert state["roles"]["PLAN"]["attachments_uploaded_generation"] == 0

    next_hop = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="PLAN",
        handoff="next turn",
    )
    asyncio.run(worker._pre_send(state, next_hop, FakeActions()))
    asyncio.run(worker._sending(state, next_hop, Actions()))

    assert captured_files[-1] == ()
    assert state["roles"]["PLAN"]["attachments_uploaded_generation"] == 0

    state["roles"]["PLAN"]["conversation_generation"] = 1
    fresh_hop = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="PLAN",
        handoff="fresh generation",
    )
    asyncio.run(worker._pre_send(state, fresh_hop, FakeActions()))
    asyncio.run(worker._sending(state, fresh_hop, Actions()))

    assert captured_files[-1] == (str(attachment.resolve()),)
    assert state["roles"]["PLAN"]["attachments_uploaded_generation"] == 1

    dev_hop = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="DEV",
        handoff="independent DEV context",
    )
    asyncio.run(worker._pre_send(state, dev_hop, FakeActions()))
    asyncio.run(worker._sending(state, dev_hop, Actions()))

    assert captured_files[-1] == (str(attachment.resolve()),)
    assert state["roles"]["DEV"]["attachments_uploaded_generation"] == 0














def test_worker_operational_failures_are_sanitized_before_manifest_and_dashboard(
    tmp_path: Path,
):
    from playwright_auto.cdpa_projection import build_task_projection

    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-worker-credential-boundary",
    )
    path = Path(state["manifest_path"])
    secret_path = "worker-path-secret-token"
    secret_query = "worker-query-secret"
    secret_bearer = "worker-bearer-secret"
    sensitive_url = (
        f"https://api.example.invalid/webhooks/{secret_path}/status"
        f"?access_token={secret_query}#worker-fragment-secret"
    )
    error = TimeoutError(
        f"GET {sensitive_url} timed out; Authorization: Bearer {secret_bearer}"
    )

    requested = store.request_control(path, "pause", reason="credential boundary fixture")
    control_id = requested["controls"][-1]["control_id"]

    saved = store.update(
        path,
        lambda current: worker._block(
            current,
            error,
            code="role_offline",
            retryable=False,
        ),
    )
    saved = store.reject_control(
        path,
        control_id,
        f"{type(error).__name__}: {error}",
        action="pause",
    )
    payload = build_task_projection(saved, tasks=[saved]).detail
    manifest_text = path.read_text(encoding="utf-8")
    dashboard_text = json.dumps(payload, ensure_ascii=False)

    for secret in (
        secret_path,
        secret_query,
        secret_bearer,
        "worker-fragment-secret",
    ):
        assert secret not in manifest_text
        assert secret not in dashboard_text
    assert "[REDACTED]" in manifest_text
    assert "[REDACTED]" in dashboard_text
    assert secret_path not in str(saved["block_reason"])
    assert secret_path not in str(saved["roles"][saved["active_role"]]["last_error"])
    assert all(secret_path not in str(item) for item in saved["errors"])
    assert all(secret_path not in str(item) for item in _active_hop(saved)["errors"])
    assert secret_path not in str(saved["controls"][-1]["result"])
    assert all(secret_path not in item["message"] for item in payload["timeline"])












def test_runtime_worker_discovers_once_and_idle_cycles_do_not_scan(tmp_path: Path, monkeypatch):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-runtime-once")
    discover_calls = 0
    recover_calls = 0
    original_discover = store.discover_with_errors
    original_recover = store.recover_phase4_replacement

    def counted_discover():
        nonlocal discover_calls
        discover_calls += 1
        return original_discover()

    def counted_recover():
        nonlocal recover_calls
        recover_calls += 1
        return original_recover()

    async def inert_advance(path, _browser_context, *, scheduling_tasks=None):
        assert scheduling_tasks is not None
        return store.load(path)

    class InertMaintainers:
        async def advance(self, tasks, _browser_context):
            assert len(tasks) == 1
            return False

    monkeypatch.setattr(store, "discover_with_errors", counted_discover)
    monkeypatch.setattr(store, "recover_phase4_replacement", counted_recover)
    monkeypatch.setattr(worker, "advance", inert_advance)
    worker.maintainers = InertMaintainers()

    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert discover_calls == 1
    assert recover_calls == 1


def test_runtime_worker_hung_dom_probe_revisits_expired_second_task_past_bound(
    tmp_path: Path,
    monkeypatch,
):
    config, store, first, worker = setup_task(tmp_path, task_id="task-runtime-hung-dom-a")
    second = store.create_task(
        "second task",
        requested_team="beta",
        task_id="task-runtime-hung-dom-b",
    )
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(first, now=0.0)
    worker.registry.update_task(second, now=0.0)

    class HangingPage:
        url = "https://chatgpt.com/c/hung"

        async def evaluate(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    timeout_seconds = 0.03
    hung_client = ChatGPTPage(HangingPage(), timeout_ms=int(timeout_seconds * 1000))
    receipt = SendReceipt(
        prompt="probe",
        prompt_sha256="probe-sha",
        binding=PageBinding("page-plan", "PLAN"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="test",
        session_id_before=None,
    )
    calls = {first["task_id"]: 0, second["task_id"]: 0}
    reconciliations = 0
    second_deadline = time.monotonic() + timeout_seconds / 2

    async def bounded_advance(path, _browser_context, *, scheduling_tasks=None):
        nonlocal reconciliations
        assert scheduling_tasks is not None
        state = store.load(path)
        task_id = state["task_id"]
        calls[task_id] += 1
        if task_id == first["task_id"]:
            with pytest.raises(TimeoutError):
                await hung_client.wait_snapshot(receipt)
        elif calls[task_id] >= 2 and time.monotonic() >= second_deadline:
            reconciliations += 1
        return state

    class InertMaintainers:
        async def advance(self, _tasks, _browser_context):
            return False

    monkeypatch.setattr(worker, "advance", bounded_advance)
    worker.maintainers = InertMaintainers()
    started = time.monotonic()

    async def scenario():
        await worker.run_once(SimpleNamespace(pages=[]))
        await worker.run_once(SimpleNamespace(pages=[]))

    asyncio.run(scenario())
    elapsed = time.monotonic() - started
    bound = 2 * timeout_seconds + 2 * worker_module.MINIMUM_DEADLINE_SECONDS

    assert calls[second["task_id"]] >= 2
    assert reconciliations == 1
    assert elapsed < bound


def test_runtime_worker_stale_result_preserves_newer_requested_control_until_command_finishes(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-runtime-stale-control"
    )
    path = Path(state["manifest_path"])
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(state, now=0.0)
    advance_started = asyncio.Event()
    release_advance = asyncio.Event()
    original_advance = worker.advance
    stale_result: dict[str, object] = {}

    async def stale_advance(path_arg, _browser_context, *, scheduling_tasks=None):
        assert scheduling_tasks is not None
        stale = json.loads(json.dumps(store.load(path_arg)))
        stale.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="stale browser-cycle result",
        )
        stale_result.clear()
        stale_result.update(stale)
        advance_started.set()
        await release_advance.wait()
        return stale

    monkeypatch.setattr(worker, "advance", stale_advance)
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: FakeActions(),
    )
    worker.runtime_db.enqueue_command(
        command_id="cmd-runtime-stale-control",
        idempotency_key="runtime-stale-control",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={"action": "stop", "reason": "prove stale result cannot erase control"},
    )

    async def scenario():
        cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
        await asyncio.wait_for(advance_started.wait(), timeout=1.0)
        delivered = worker.dispatch_command_once()
        assert delivered is not None and delivered["status"] == "running"
        requested = store.load(path)
        assert requested["controls"][-1]["status"] == "requested"
        assert requested["updated_at"] != stale_result["updated_at"]

        release_advance.set()
        await cycle

        current = worker.registry.tasks_by_id[state["task_id"]]
        assert current["updated_at"] == requested["updated_at"]
        assert current["controls"][-1]["status"] == "requested"
        assert state["task_id"] in worker.registry.due_task_ids(time.time() + 1.0)

        terminal = await original_advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=list(worker.registry.tasks_by_id.values()),
        )
        assert terminal is not None
        assert terminal["controls"][-1]["status"] == "applied"
        command = worker.runtime_db.get_command("cmd-runtime-stale-control")
        assert command is not None and command["status"] == "applied"

    asyncio.run(scenario())


def test_runtime_worker_consumes_new_requested_control_during_unrelated_slow_cycle(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, slow, worker = setup_task(
        tmp_path, task_id="task-runtime-slow-cycle"
    )
    slow_path = Path(slow["manifest_path"])
    target = store.create_task(
        "blocked target",
        requested_team="target-team",
        task_id="task-runtime-live-control",
    )
    target_path = Path(target["manifest_path"])
    target["status"] = "BLOCKED"
    target["kanban_column"] = "BLOCKED"
    target["block_code"] = "role_offline"
    target["block_retryable"] = False
    target["block_reason"] = "owned target PLAN tab is offline"
    target["roles"]["PLAN"].update(
        page_id="target-plan-page",
        page_url="https://chatgpt.com/c/target-plan",
        online=False,
        status="offline",
        last_error="page_missing",
    )
    target = store.save(target_path, target)

    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry._due_at[slow["task_id"]] = time.time() - 1.0
    assert target["task_id"] not in worker.registry.due_task_ids(time.time())

    slow_started = asyncio.Event()
    target_started = asyncio.Event()
    release_slow = asyncio.Event()
    original_advance = worker.advance
    reopen_calls = 0

    async def mixed_advance(path, browser_context, *, scheduling_tasks=None):
        resolved = Path(path).resolve()
        if resolved == slow_path.resolve():
            slow_started.set()
            await release_slow.wait()
            return store.load(path)
        if resolved == target_path.resolve():
            assert target["task_id"] in worker.registry.due_task_ids(time.time())
            target_started.set()
        return await original_advance(
            path,
            browser_context,
            scheduling_tasks=scheduling_tasks,
        )

    class ControlActions(FakeActions):
        async def reopen(
            self,
            _state,
            _role,
            *,
            require_clean_ready=True,
            foreground=True,
        ):
            nonlocal reopen_calls
            reopen_calls += 1
            assert foreground is True
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id="target-plan-page",
                url="https://chatgpt.com/c/target-plan",
                created=False,
                new_chat=False,
            )

    monkeypatch.setattr(worker, "advance", mixed_advance)
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: ControlActions(),
    )
    worker.runtime_db.enqueue_command(
        command_id="cmd-runtime-live-control",
        idempotency_key="runtime-live-control",
        kind="task_control",
        task_id=target["task_id"],
        expected_task_version=None,
        payload={
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact blocked role",
        },
    )

    async def scenario():
        cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
        await asyncio.wait_for(slow_started.wait(), timeout=1.0)

        delivered = worker.dispatch_command_once()
        assert delivered is not None and delivered["status"] == "running"
        requested = store.load(target_path)
        assert requested["controls"][-1]["status"] == "requested"

        await asyncio.wait_for(target_started.wait(), timeout=1.5)
        for _ in range(20):
            command = worker.runtime_db.get_command("cmd-runtime-live-control")
            if command is not None and command["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert command is not None and command["status"] == "applied"
        applied = store.load(target_path)
        assert applied["controls"][-1]["status"] == "applied"
        assert reopen_calls == 1
        assert cycle.done() is False

        release_slow.set()
        await asyncio.wait_for(cycle, timeout=1.0)

    asyncio.run(scenario())


def test_runtime_worker_inventory_timeout_does_not_starve_requested_control(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-runtime-inventory-stall"
    )
    path = Path(state["manifest_path"])
    store.update(
        path,
        lambda current: worker._block(
            current,
            "owned PLAN tab is offline",
            code="role_offline",
            retryable=False,
        ),
    )
    worker.hydrate_runtime()
    previous_projection = {
        "connected": True,
        "page_count": 1,
        "pages": [{"page_id": "previous-page", "url": "https://chatgpt.com/"}],
    }
    worker._browser_projection = previous_projection
    worker._last_browser_inventory_at = 0.0
    worker._last_browser_page_count = 1
    inventory_started = asyncio.Event()
    inventory_cancelled = asyncio.Event()
    release_inventory = asyncio.Event()
    inventory_calls = 0

    async def hung_inventory(*_args, **_kwargs):
        nonlocal inventory_calls
        inventory_calls += 1
        inventory_started.set()
        try:
            await release_inventory.wait()
        except asyncio.CancelledError:
            inventory_cancelled.set()
            await release_inventory.wait()
            raise

    monkeypatch.setattr(worker_module, "build_browser_projection", hung_inventory)
    worker.runtime_db.enqueue_command(
        command_id="cmd-runtime-inventory-stall",
        idempotency_key="runtime-inventory-stall",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={"action": "stop", "reason": "prove inventory cannot starve controls"},
    )
    delivered = worker.dispatch_command_once()
    assert delivered is not None and delivered["status"] == "running"
    requested = store.load(path)
    assert requested["controls"][-1]["status"] == "requested"

    async def scenario():
        cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
        await asyncio.wait_for(inventory_started.wait(), timeout=1.0)
        done, _pending = await asyncio.wait({cycle}, timeout=2.2)
        first_cycle_bounded = cycle in done

        second_cycle_bounded = False
        second_cycle = None
        if first_cycle_bounded:
            second_cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
            done, _pending = await asyncio.wait({second_cycle}, timeout=0.8)
            second_cycle_bounded = second_cycle in done

        release_inventory.set()
        await asyncio.wait_for(cycle, timeout=1.0)
        if second_cycle is not None:
            await asyncio.wait_for(second_cycle, timeout=1.0)
        await asyncio.sleep(0)
        return first_cycle_bounded, second_cycle_bounded

    first_cycle_bounded, second_cycle_bounded = asyncio.run(scenario())

    assert first_cycle_bounded is True
    assert second_cycle_bounded is True
    assert inventory_calls == 1
    assert inventory_cancelled.is_set() is False
    assert getattr(worker, "_browser_inventory_task", None) is None
    command = worker.runtime_db.get_command("cmd-runtime-inventory-stall")
    assert command is not None and command["status"] == "applied"
    applied = store.load(path)
    assert applied["controls"][-1]["status"] == "applied"
    assert worker._browser_projection == previous_projection


def test_heartbeat_ticker_keeps_long_advance_online_without_task_mutation(
    tmp_path: Path, monkeypatch
):
    from dataclasses import replace

    from playwright_auto.dashboard_api import DashboardAPI

    config, store, state, worker = setup_task(tmp_path, task_id="task-heartbeat-live")
    worker.config = replace(config, heartbeat_seconds=0.01, worker_stale_seconds=0.04)
    worker.hydrate_runtime()
    manifest_path = Path(state["manifest_path"])
    manifest_before = manifest_path.read_bytes()
    advance_started = asyncio.Event()

    async def slow_advance(path, _browser_context, *, scheduling_tasks=None):
        assert scheduling_tasks is not None
        advance_started.set()
        await asyncio.sleep(0.10)
        return store.load(path)

    monkeypatch.setattr(worker, "advance", slow_advance)
    api = DashboardAPI(worker.config)

    async def scenario():
        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        advance_task = asyncio.create_task(
            worker.advance(
                manifest_path,
                SimpleNamespace(pages=[]),
                scheduling_tasks=[store.load(manifest_path)],
            )
        )
        try:
            await asyncio.wait_for(advance_started.wait(), timeout=1.0)
            await asyncio.sleep(0.07)
            health = api.worker_health()
            assert health["worker_online"] is True
            assert health["worker_stale"] is False
            await advance_task
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    try:
        asyncio.run(scenario())
        assert manifest_path.read_bytes() == manifest_before
    finally:
        api.db.close()
        worker.runtime_db.close()


def test_stopped_heartbeat_ticker_becomes_stale(tmp_path: Path):
    from dataclasses import replace

    from playwright_auto.dashboard_api import DashboardAPI

    config, _store, _state, worker = setup_task(tmp_path, task_id="task-heartbeat-stale")
    worker.config = replace(config, heartbeat_seconds=0.01, worker_stale_seconds=0.04)
    worker.hydrate_runtime()
    api = DashboardAPI(worker.config)

    async def scenario():
        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        await asyncio.sleep(0.03)
        assert api.worker_health()["worker_online"] is True
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await asyncio.sleep(0.06)
        assert api.worker_health()["worker_stale"] is True

    try:
        asyncio.run(scenario())
    finally:
        api.db.close()
        worker.runtime_db.close()


def test_dashboard_actions_snapshot_tracks_browser_and_command_changes(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-dashboard-actions"
    )
    worker.hydrate_runtime()

    initial = worker.runtime_db.get_snapshot("dashboard_actions")
    assert initial is not None
    assert initial["payload"]["resume_teams"] == []
    assert initial["payload"]["dependency_teams"][0]["team"] == state["team"]

    asyncio.run(worker._publish_browser_inventory(SimpleNamespace(pages=[])))
    connected = worker.runtime_db.get_snapshot("dashboard_actions")["payload"]
    assert connected["resume_teams"] == [
        {
            "team": state["team"],
            "task_id": state["task_id"],
            "title": state["task_title"],
            "status": "INBOX",
            "reason": "offline",
        }
    ]

    worker._publish_browser_disconnected()
    disconnected = worker.runtime_db.get_snapshot("dashboard_actions")["payload"]
    assert disconnected["resume_teams"] == []

    worker.runtime_db.enqueue_command(
        command_id="cmd-dashboard-actions-create",
        idempotency_key="dashboard-actions-create",
        kind="create_task",
        task_id="task-dashboard-actions-created",
        expected_task_version=None,
        payload={
            "task": "Created dashboard action dependency",
            "requested_team": "mailbox",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["PLAN", "REVIEW"],
        },
    )
    assert worker._apply_next_command()["status"] == "applied"
    after_create = worker.runtime_db.get_snapshot("dashboard_actions")["payload"]
    assert {item["team"] for item in after_create["dependency_teams"]} == {
        state["team"],
        "mailbox",
    }


def test_runtime_create_command_applies_once_and_publishes_projection(tmp_path: Path):
    config, store, _state, worker = setup_task(tmp_path, task_id="task-existing-command")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-once",
        idempotency_key="create-once",
        kind="create_task",
        task_id="task-created-command",
        expected_task_version=None,
        payload={
            "task": "Created through mailbox",
            "requested_team": "mailbox",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["REVIEW", "PLAN"],
        },
    )

    result = worker._apply_next_command()

    assert result["status"] == "applied"
    created = store.load_task_id("task-created-command")
    assert created is not None
    assert created["applied_command_ids"] == ["cmd-create-once"]
    assert list(created["roles"]) == ["PLAN", "REVIEW"]
    assert worker.runtime_db.get_task_detail("task-created-command")["task_id"] == "task-created-command"

    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL "
            "WHERE command_id = ?",
            ("cmd-create-once",),
        )
    reconciled = worker._apply_next_command()
    assert reconciled["status"] == "applied"
    assert reconciled["result"]["reconciled"] is True
    assert store.load_task_id("task-created-command")["applied_command_ids"] == [
        "cmd-create-once"
    ]









def test_missing_snapshot_bootstrap_rebases_to_current_default(tmp_path: Path):
    config, store, state, worker = setup_task(tmp_path, task_id="task-bootstrap-account-switch")
    old = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    state["bootstrap"] = old
    BootstrapCatalog(tmp_path).delete(old["bootstrap_id"])
    current = BootstrapCatalog(tmp_path).upsert(
        bootstrap_record(bootstrap_id="g8-bootstrap")
    )

    assert worker._bootstrap_for_state(state) == current


def test_runtime_create_snapshots_bootstrap_and_replay_ignores_catalog_drift(tmp_path: Path):
    config, store, _state, worker = setup_task(
        tmp_path, task_id="task-existing-bootstrap-command"
    )
    worker.hydrate_runtime()
    anchor = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-bootstrap",
        idempotency_key="create-bootstrap",
        kind="create_task",
        task_id="task-created-bootstrap",
        expected_task_version=None,
        payload={
            "task": "Created with bootstrap",
            "requested_team": "bootstrap-mailbox",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["PLAN", "DEV", "REVIEW"],
            "bootstrap_id": anchor["bootstrap_id"],
        },
    )

    result = worker._apply_next_command()
    assert result["status"] == "applied"
    created = store.load_task_id("task-created-bootstrap")
    assert created is not None
    assert created["bootstrap"] == anchor

    BootstrapCatalog(tmp_path).delete(anchor["bootstrap_id"])
    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            ("cmd-create-bootstrap",),
        )
    replay = worker._apply_next_command()
    assert replay["status"] == "applied"
    assert replay["result"]["reconciled"] is True
    assert store.load_task_id("task-created-bootstrap")["bootstrap"] == anchor

    command = worker.runtime_db.get_command("cmd-create-bootstrap")
    changed = {**command, "payload": {**command["payload"], "bootstrap_id": "other-bootstrap"}}
    with pytest.raises(RuntimeError, match="bootstrap"):
        worker._command_replay_state(changed)

    disabled = BootstrapCatalog(tmp_path).upsert(
        bootstrap_record(bootstrap_id="disabled-bootstrap", enabled=False)
    )
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-disabled-bootstrap",
        idempotency_key="create-disabled-bootstrap",
        kind="create_task",
        task_id="task-disabled-bootstrap",
        expected_task_version=None,
        payload={
            "task": "Must fail",
            "requested_team": "disabled-bootstrap",
            "repository": str(tmp_path),
            "report_mode": "file",
            "bootstrap_id": disabled["bootstrap_id"],
        },
    )
    failed = worker._apply_next_command()
    assert failed["status"] == "failed"
    assert store.load_task_id("task-disabled-bootstrap") is None

    worker.runtime_db.enqueue_command(
        command_id="cmd-create-explicit-fresh",
        idempotency_key="create-explicit-fresh",
        kind="create_task",
        task_id="task-explicit-fresh",
        expected_task_version=None,
        payload={
            "task": "Explicit Fresh remains supported",
            "requested_team": "explicit-fresh",
            "repository": str(tmp_path),
            "report_mode": "file",
            "bootstrap_id": None,
        },
    )
    fresh_result = worker._apply_next_command()
    assert fresh_result["status"] == "applied"
    fresh = store.load_task_id("task-explicit-fresh")
    assert fresh is not None
    assert fresh.get("bootstrap") is None

    worker.runtime_db.enqueue_command(
        command_id="cmd-create-inline-bootstrap",
        idempotency_key="create-inline-bootstrap",
        kind="create_task",
        task_id="task-inline-bootstrap",
        expected_task_version=None,
        payload={
            "task": "Create bootstrap inline",
            "requested_team": "inline-bootstrap-team",
            "repository": str(tmp_path),
            "report_mode": "file",
            "bootstrap_id": "inline-bootstrap",
            "bootstrap_definition": {
                "bootstrap_id": "inline-bootstrap",
                "name": "Inline Bootstrap",
                "source_conversation_id": None,
                "prewarm_prompt": "Reusable inline prewarm.",
                "max_backups": 5,
            },
        },
    )
    inline_result = worker._apply_next_command()
    assert inline_result["status"] == "applied"
    inline = store.load_task_id("task-inline-bootstrap")
    assert inline is not None
    assert inline["bootstrap"]["bootstrap_id"] == "inline-bootstrap"
    assert inline["bootstrap"]["prewarm_prompt"] == "Reusable inline prewarm."
    assert inline["bootstrap"]["max_backups"] == 5
    assert BootstrapCatalog(tmp_path).get("inline-bootstrap") == inline["bootstrap"]


def test_bootstrap_projection_is_sanitized_and_distinguishes_fallbacks(tmp_path: Path):
    from playwright_auto.cdpa_projection import build_task_projection

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "projection bootstrap",
        requested_team="projection-bootstrap",
        task_id="task-bootstrap-projection",
        roles=("PLAN", "DEV", "REVIEW"),
        bootstrap=bootstrap_record(),
    )
    state["roles"]["PLAN"]["context_source"] = "bootstrap_donor"
    state["roles"]["DEV"]["context_source"] = "bootstrap_native"
    state["roles"]["REVIEW"]["context_source"] = "bootstrap_ui"
    state["status"] = "BLOCKED"
    state["block_reason"] = "locator timeout for bootstrap donor"

    detail = build_task_projection(state, tasks=[state]).detail
    context = detail["bootstrap_context"]
    assert context["bootstrap_id"] == "general-team-bootstrap"
    assert context["name"] == "General Team Bootstrap"
    assert context["roles"]["PLAN"] != context["roles"]["DEV"]
    assert context["roles"]["DEV"] != context["roles"]["REVIEW"]
    serialized = json.dumps(detail, sort_keys=True)
    assert bootstrap_record()["conversation_id"] not in serialized
    assert bootstrap_record()["terminal_assistant_message_id"] not in serialized

    fresh = store.create_task(
        "projection fresh", requested_team="projection-fresh", task_id="task-fresh-projection"
    )
    fresh_detail = build_task_projection(fresh, tasks=[fresh]).detail
    assert "bootstrap_context" not in fresh_detail


def test_independent_activation_refreshes_registry_and_projection(tmp_path: Path):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-maintainers-refresh"
    )
    store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "role_offline",
            "block_reason": "blocked",
            "updated_at": "2026-07-26T00:00:00+00:00",
        },
    )
    store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "blocked_at": "2026-07-26T00:00:00+00:00",
        },
    )
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    standby = store.update(
        standby["manifest_path"],
        lambda current: {
            **current,
            "independent": {
                **current["independent"],
                "watermarks": {
                    **current["independent"]["watermarks"],
                    "recovery_enabled_at": "2026-07-26T00:00:00+00:00",
                },
            },
        },
    )
    worker.hydrate_runtime()

    worker._activate_independent_agents()

    assert worker.registry.tasks_by_id[standby["task_id"]]["status"] == "RUNNING"
    projected = worker.runtime_db.get_task_detail(standby["task_id"])
    assert projected["status"] == "RUNNING"
    assert projected["column"] == "INDEPENDENT_AGENTS"
    assert projected["agent"]["target_task_id"] == state["task_id"]















def test_active_accepted_waiting_offline_reopens_without_resend_or_clean_composer(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-auto-recover-waiting"
    )
    hop = _active_hop(state)
    exact_url = "https://chatgpt.com/c/waiting-exact"
    original_request_id = hop["request_id"]
    receipt = {
        "binding": {"page_id": "closed-page", "role": "alpha-plan"},
        "accepted_via": "user_message_identity",
        "user_message_id": "accepted-user",
        "user_turn_id": "accepted-turn",
    }
    hop.update(state="waiting", conversation_url=exact_url, receipt=receipt)
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url=exact_url,
        online=False,
    )
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="closed-page",
        url=exact_url,
        created=True,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.reopen_calls = []
            self.wake_calls = []

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(
            self,
            *_args,
            require_clean_ready=True,
            foreground=True,
            **_kwargs,
        ):
            self.reopen_calls.append((require_clean_ready, foreground))
            return acquired

        async def wake(self, exact):
            self.wake_calls.append(exact)

    actions = Actions()
    result = asyncio.run(worker._owned_or_block(state, "PLAN", actions))

    assert result == acquired
    assert actions.reopen_calls == [(False, False)]
    assert actions.wake_calls == [acquired]
    assert hop["state"] == "waiting"
    assert hop["request_id"] == original_request_id
    assert hop["receipt"] == receipt
    assert state["status"] != "BLOCKED"
    assert state.get("maintenance") in (None, {})


def test_change_goal_command_applies_once_and_reconciles_without_browser(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-change-goal")
    state = store.update(state["manifest_path"], lambda current: {**current, "status": "RUNNING"})
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-change-goal", idempotency_key="change-goal", kind="change_goal",
        task_id=state["task_id"], expected_task_version=None, payload={"goal": "Replacement from command"},
    )
    applied = worker.dispatch_command_once()
    assert applied["status"] == "applied"
    changed = store.load_task_id(state["task_id"])
    assert changed["effective_goal"] == "Replacement from command"
    assert len(changed["goal_revisions"]) == 1
    assert changed["hops"][0]["prompt"] is None
    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            ("cmd-change-goal",),
        )
    reconciled = worker.dispatch_command_once()
    assert reconciled["status"] == "applied"
    assert reconciled["result"]["reconciled"] is True
    assert len(store.load_task_id(state["task_id"])["goal_revisions"]) == 1


def test_change_goal_keeps_current_prompt_and_updates_first_later_hop(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-goal-prompt")
    state = store.update(state["manifest_path"], lambda current: {**current, "status": "RUNNING"})
    state = store.change_goal(
        state["manifest_path"], "Replacement for later roles",
        external_command_id="cmd-goal-prompt",
    )
    current = _active_hop(state)
    asyncio.run(worker._pre_send(state, current, FakeActions()))
    current_envelope, _ = json.JSONDecoder().raw_decode(
        current["prompt"].removeprefix("alpha · role: plan\n")
    )
    assert current_envelope["goal"] == "Implement exact production behavior"

    later = worker._append_hop(
        state, source_role="PLAN", target_role="DEV",
        handoff=".plan/alpha/alpha-plan_turn1_task-goal-prompt.md",
    )
    asyncio.run(worker._pre_send(state, later, FakeActions()))
    later_envelope, _ = json.JSONDecoder().raw_decode(
        later["prompt"].removeprefix("alpha · role: dev\n")
    )
    assert later_envelope["goal"] == "Replacement for later roles"


def test_dom_wait_transient_target_crash_runs_bounded_recovery_refresh(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-target-crash"
    )
    refresh_calls = []

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            raise RuntimeError("Page.evaluate: Target crashed")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, target, **kwargs):
            refresh_calls.append((target, kwargs))
            return target

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    assert hop["state"] == "waiting"
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_response"
    assert hop["wait"]["refresh_count"] == 1
    assert hop["wait"]["last_refresh_result"]["status"] == "completed"
    assert hop["wait"]["last_refresh_result"]["reason"] == "dom_observation_recovery"
    assert len(refresh_calls) == 1
    assert refresh_calls[0][1]["manifest"] is state
    assert refresh_calls[0][1]["logical_role"] == "PLAN"
    assert refresh_calls[0][1]["recover"] is True
    assert refresh_calls[0][1]["skip_precheck"] is True


def test_dom_periodic_refresh_target_crash_stays_resumable_without_replay(
    tmp_path: Path,
):
    from dataclasses import replace

    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-refresh-target-crash"
    )
    worker.config = replace(worker.config, response_refresh_after_seconds=0.0)
    worker._final_response_reconciliation = AsyncMock(return_value=False)
    accepted_user = MessageSnapshot(
        "user",
        receipt.user_message_id or "u1",
        receipt.user_turn_id or "t1",
        receipt.prompt,
        (),
    )
    snapshot = send_snapshot(
        messages=(accepted_user,),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def wait_for_response(self, *_args, **_kwargs):
            raise AssertionError("transient refresh failure must return to worker loop")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )
    calls = {"refresh": 0, "send": 0, "retry": 0}

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            calls["refresh"] += 1
            raise RuntimeError("Page.evaluate: Target crashed")

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("recovery must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("recovery must not Retry")

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    assert calls == {"refresh": 1, "send": 0, "retry": 0}
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_response"
    assert hop["state"] == "waiting"
    assert hop["wait"]["refresh_count"] == 1
    assert hop["wait"]["last_refresh_result"]["status"] == "failed"
    assert "Target crashed" in hop["wait"]["last_refresh_result"]["error"]


def test_dom_wait_expired_transient_observation_reconciles_despite_recent_recovery(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-expired-transient"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    hop["wait"]["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
    hop["wait"]["last_refresh_result"] = {
        "status": "completed",
        "reason": "dom_observation_recovery",
    }

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            raise RuntimeError("Page.evaluate: Target crashed")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )
    calls = {"refresh": 0, "send": 0, "retry": 0}

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            calls["refresh"] += 1
            raise AssertionError("recent recovery must suppress another reload")

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("deadline reconciliation must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("deadline reconciliation must not Retry")

    worker._final_response_reconciliation = AsyncMock(return_value=False)
    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    worker._final_response_reconciliation.assert_awaited_once()
    assert calls == {"refresh": 0, "send": 0, "retry": 0}
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "response_timeout"


def test_dom_wait_expired_recent_recovery_final_lifecycle_error_stays_resumable(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-expired-recent-final-lifecycle"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    hop["wait"]["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
    hop["wait"]["last_refresh_result"] = {
        "status": "completed",
        "reason": "dom_observation_recovery",
    }
    calls = {"snapshot": 0, "final": 0, "refresh": 0}

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            calls["snapshot"] += 1
            raise RuntimeError("Page.evaluate: Target crashed")

        async def wait_for_response(self, *_args, **_kwargs):
            calls["final"] += 1
            raise RuntimeError("Page.evaluate: Target crashed")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            calls["refresh"] += 1
            raise AssertionError("recent recovery must suppress another reload")

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    assert calls == {"snapshot": 1, "final": 1, "refresh": 0}
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert hop["state"] == "waiting"


def test_dom_wait_expired_closed_target_reconciles_on_reacquired_exact_page(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-expired-reacquired"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    calls = {"refresh": 0, "old_wait": 0, "new_wait": 0, "send": 0, "retry": 0}

    class OldClient:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            raise RuntimeError("Page has been closed")

        async def wait_for_response(self, *_args, **_kwargs):
            calls["old_wait"] += 1
            raise RuntimeError("Page has been closed")

    class ReacquiredClient:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_for_response(self, *_args, **_kwargs):
            calls["new_wait"] += 1
            raise TimeoutError("no terminal response after exact reacquire")

    old_acquired = AcquiredRole(
        OldClient(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )
    reacquired = AcquiredRole(
        ReacquiredClient(),
        receipt.binding.page_id,
        "https://chatgpt.com/c/test",
        False,
        False,
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return old_acquired

        async def refresh(self, target, **kwargs):
            assert target is old_acquired
            assert kwargs["recover"] is True
            assert kwargs["skip_precheck"] is True
            calls["refresh"] += 1
            return reacquired

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("recovery must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("recovery must not Retry")

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    assert calls == {
        "refresh": 1,
        "old_wait": 0,
        "new_wait": 1,
        "send": 0,
        "retry": 0,
    }
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "response_timeout"


def test_dom_wait_expired_transient_refresh_failure_does_not_reconcile_stale_target(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-expired-refresh-failure"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    calls = {"snapshot": 0, "refresh": 0, "final": 0, "send": 0, "retry": 0}

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            calls["snapshot"] += 1
            raise RuntimeError("Page.evaluate: Target crashed")

        async def wait_for_response(self, *_args, **_kwargs):
            calls["final"] += 1
            raise RuntimeError("Page.evaluate: Target crashed")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, target, **kwargs):
            assert target is acquired
            assert kwargs["recover"] is True
            assert kwargs["skip_precheck"] is True
            calls["refresh"] += 1
            raise RuntimeError("Page.reload: Target crashed")

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("recovery must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("recovery must not Retry")

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    assert calls == {
        "snapshot": 1,
        "refresh": 1,
        "final": 0,
        "send": 0,
        "retry": 0,
    }
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert hop["state"] == "waiting"
    assert hop["wait"]["last_refresh_result"]["status"] == "failed"
    assert "Target crashed" in hop["wait"]["last_refresh_result"]["error"]


def test_dom_wait_expired_clean_snapshot_final_lifecycle_error_stays_resumable(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-expired-clean-final-lifecycle"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    accepted_user = MessageSnapshot(
        "user",
        receipt.user_message_id or "u1",
        receipt.user_turn_id or "t1",
        receipt.prompt,
        (),
    )
    snapshot = send_snapshot(
        messages=(accepted_user,),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    calls = {"snapshot": 0, "final": 0, "refresh": 0, "send": 0, "retry": 0}

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            calls["snapshot"] += 1
            return snapshot

        async def wait_for_response(self, *_args, **_kwargs):
            calls["final"] += 1
            raise RuntimeError("Page.evaluate: Target crashed")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            calls["refresh"] += 1
            raise AssertionError("expired deadline must not reload after transient final reconciliation")

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("final reconciliation must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("final reconciliation must not Retry")

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    assert calls == {
        "snapshot": 1,
        "final": 1,
        "refresh": 0,
        "send": 0,
        "retry": 0,
    }
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert hop["state"] == "waiting"
    assert state["active_action"] == "wait_response"


def test_dom_wait_expired_clean_snapshot_unrelated_final_error_is_not_swallowed(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-expired-clean-final-unrelated"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    accepted_user = MessageSnapshot(
        "user",
        receipt.user_message_id or "u1",
        receipt.user_turn_id or "t1",
        receipt.prompt,
        (),
    )
    snapshot = send_snapshot(
        messages=(accepted_user,),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def wait_for_response(self, *_args, **_kwargs):
            raise RuntimeError("selector parser exploded")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            raise AssertionError("unrelated final-reconciliation error must fail closed")

    with pytest.raises(RuntimeError, match="selector parser exploded"):
        asyncio.run(worker._waiting_dom(state, hop, Actions(), path))


def test_dom_wait_wait_timeout_then_final_lifecycle_error_stays_resumable(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-wait-timeout-final-lifecycle"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    ).isoformat()
    accepted_user = MessageSnapshot(
        "user",
        receipt.user_message_id or "u1",
        receipt.user_turn_id or "t1",
        receipt.prompt,
        (),
    )
    snapshot = send_snapshot(
        messages=(accepted_user,),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    calls = {"snapshot": 0, "wait": 0, "refresh": 0, "send": 0, "retry": 0}

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            calls["snapshot"] += 1
            return snapshot

        async def wait_for_response(self, *_args, **_kwargs):
            calls["wait"] += 1
            if calls["wait"] == 1:
                hop["wait"]["deadline_at"] = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat()
                raise TimeoutError("poll reached deadline")
            raise RuntimeError("Page.evaluate: Target crashed")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            calls["refresh"] += 1
            raise AssertionError("normal wait-timeout path must not refresh here")

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("final reconciliation must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("final reconciliation must not Retry")

    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    assert calls == {
        "snapshot": 1,
        "wait": 2,
        "refresh": 0,
        "send": 0,
        "retry": 0,
    }
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert hop["state"] == "waiting"
    assert state["active_action"] == "wait_response"


def test_dom_wait_expired_deadline_performs_final_reconciliation(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-expired-deadline"
    )
    hop["wait"]["deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    accepted_user = MessageSnapshot(
        "user",
        receipt.user_message_id or "u1",
        receipt.user_turn_id or "t1",
        receipt.prompt,
        (),
    )
    snapshot = send_snapshot(
        messages=(accepted_user,),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            raise AssertionError("expired deadline must reconcile before refresh")

    worker._final_response_reconciliation = AsyncMock(return_value=False)
    asyncio.run(worker._waiting_dom(state, hop, Actions(), path))

    worker._final_response_reconciliation.assert_awaited_once()
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "response_timeout"


def test_dom_wait_unrelated_runtime_error_is_not_recovered(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-dom-unrelated-error"
    )

    class Client:
        binding = receipt.binding
        page = SimpleNamespace()

        async def wait_snapshot(self, _receipt, **_kwargs):
            raise RuntimeError("selector parser exploded")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/test", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            raise AssertionError("unrelated errors must not trigger recovery refresh")

    with pytest.raises(RuntimeError, match="selector parser exploded"):
        asyncio.run(worker._waiting_dom(state, hop, Actions(), path))


def test_resume_reconciles_canonical_ledger_before_exact_page_validation(tmp_path: Path):
    from dataclasses import replace

    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-stale-web"
    )
    worker.runtime_db.ensure_schema()
    provisional_url = "https://chatgpt.com/c/WEB:stale-resume"
    canonical_id = "canonical-resume"
    canonical_url = f"https://chatgpt.com/c/{canonical_id}"
    hop["conversation_url"] = provisional_url
    state["roles"]["PLAN"]["page_url"] = provisional_url
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id=canonical_id).to_dict(),
    )
    control = {
        "control_id": 1,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=20)
    ).isoformat()
    current_receipt = replace(receipt, conversation_id=canonical_id)
    accepted_user = MessageSnapshot(
        "user",
        current_receipt.user_message_id or "accepted-user",
        current_receipt.user_turn_id or "accepted-turn",
        current_receipt.prompt,
        (),
    )
    snapshot = replace(
        send_snapshot(
            messages=(accepted_user,),
            state=ChatGPTState.RESPONDING,
            task_id=state["task_id"],
            team=state["team"],
        ),
        stop_visible=True,
    )
    client = SimpleNamespace(
        assert_ownership=AsyncMock(return_value=snapshot),
        wait_for_response=AsyncMock(side_effect=TimeoutError()),
    )
    acquired = AcquiredRole(
        client, current_receipt.binding.page_id, canonical_url, False, False
    )
    seen = []

    class Actions:
        async def backend_stream_status(self, conversation_id):
            seen.append(conversation_id)
            return {"status": "IS_STREAMING"}

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("Resume must not fetch the conversation graph")

        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("exact owned Resume should not reopen source")

    asyncio.run(worker._recover_resume_waiting(state, hop, control, Actions()))

    assert seen == []
    assert control["status"] == "applied"
    assert control["result"]["action"] == "observe_progress"
    assert control["result"]["postcondition"] == "generation_progress"
    assert hop["receipt"]["conversation_id"] == canonical_id
    assert hop["conversation_url"] == canonical_url
    assert state["roles"]["PLAN"]["page_url"] == canonical_url
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1

    worker.runtime_db.put_snapshot("settings", {"dom_only": True})
    control = {
        "control_id": 2,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    current_receipt = SendReceipt.from_dict(hop["receipt"])
    accepted_user = MessageSnapshot(
        "user",
        current_receipt.user_message_id or "accepted-user",
        current_receipt.user_turn_id or "accepted-turn",
        current_receipt.prompt,
        (),
    )
    snapshot = replace(
        send_snapshot(
            messages=(accepted_user,),
            state=ChatGPTState.RESPONDING,
            task_id=state["task_id"],
            team=state["team"],
        ),
        stop_visible=True,
    )
    client = SimpleNamespace(
        assert_ownership=AsyncMock(return_value=snapshot),
        wait_for_response=AsyncMock(side_effect=TimeoutError()),
    )
    acquired = AcquiredRole(
        client,
        current_receipt.binding.page_id,
        canonical_url,
        False,
        False,
    )
    on_actions = SimpleNamespace(
        backend_stream_status=AsyncMock(
            side_effect=AssertionError("DOM-only Resume must not call stream_status")
        ),
        backend_conversation=AsyncMock(
            side_effect=AssertionError("DOM-only Resume must not fetch conversation graph")
        ),
        locate_owned=AsyncMock(return_value=acquired),
        reopen=AsyncMock(
            side_effect=AssertionError("exact owned DOM Resume must not reopen source")
        ),
        send=AsyncMock(side_effect=AssertionError("Resume must not resend")),
        restart=AsyncMock(side_effect=AssertionError("Resume must not restart")),
        acquire=AsyncMock(side_effect=AssertionError("Resume must not create a continuation")),
    )
    worker._discover_accepted_conversation_identity = AsyncMock(
        side_effect=AssertionError("DOM-only Resume must not perform backend identity search")
    )

    asyncio.run(worker._recover_resume_waiting(state, hop, control, on_actions))

    assert seen == []
    assert on_actions.backend_stream_status.await_count == 0
    assert on_actions.backend_conversation.await_count == 0
    assert worker._discover_accepted_conversation_identity.await_count == 0
    assert on_actions.locate_owned.await_count == 1
    assert client.wait_for_response.await_count == 1
    assert on_actions.reopen.await_count == 0
    assert on_actions.send.await_count == 0
    assert on_actions.restart.await_count == 0
    assert on_actions.acquire.await_count == 0
    assert control["status"] == "applied"
    assert control["result"]["action"] == "observe_progress"
    assert control["result"]["postcondition"] == "generation_progress"
    assert hop["receipt"]["conversation_id"] == canonical_id
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_waiting_reconciles_conversation_id_from_exact_ledger_without_status_poll(tmp_path: Path):
    from dataclasses import replace
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-conversation-reconcile"
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id="conversation-1").to_dict(),
    )

    worker._reconcile_hop_conversation_identity(state, hop)

    assert hop["receipt"]["conversation_id"] == "conversation-1"
    assert ledger.get(hop["request_id"]).attempts == 1



def test_one_shot_stream_status_is_reserved_for_recovery_boundary(tmp_path: Path):
    _store, _state, worker, _path, _hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-expired-backend-first"
    )
    canonical_id = "expired-backend"
    receipt = replace(receipt, conversation_id=canonical_id)
    calls = []

    class Actions:
        async def backend_stream_status(self, conversation_id):
            calls.append(("status", conversation_id))
            return {"status": "COMPLETE"}

        async def backend_conversation(self, conversation_id):
            calls.append(("graph", conversation_id))
            raise AssertionError("one-shot status must not fetch the full graph")

    status = asyncio.run(worker._one_shot_stream_status(Actions(), receipt))

    assert status == "COMPLETE"
    assert calls == [("status", canonical_id)]


def test_record_response_reconciles_late_conversation_id_and_conflict_fails_closed(tmp_path: Path):
    from dataclasses import replace
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-conversation-late"
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id="conversation-late").to_dict(),
    )
    response = MessageSnapshot("assistant", "a1", "ta1", "answer", ())
    worker._record_response(state, hop, response)
    assert hop["state"] == "responded"
    assert hop["receipt"]["conversation_id"] == "conversation-late"

    hop["state"] = "waiting"
    hop["receipt"] = replace(receipt, conversation_id="conversation-old").to_dict()
    with pytest.raises(RuntimeError, match="conversation identity"):
        worker._record_response(state, hop, response)
    assert hop["state"] == "waiting"


def test_worker_completion_is_dom_first_with_status_only_in_recovery_helpers():
    import inspect

    waiting_source = inspect.getsource(CDPAWorker._waiting)
    assert "_waiting_dom" in waiting_source
    assert "_waiting_backend_step" not in waiting_source
    assert "backend_stream_status" not in waiting_source

    one_shot_source = inspect.getsource(CDPAWorker._one_shot_stream_status)
    assert "backend_stream_status" in one_shot_source
    assert "backend_conversation" not in one_shot_source

    resume_source = inspect.getsource(CDPAWorker._recover_resume_waiting)
    assert "retry_generation(" not in resume_source

    method_source = inspect.getsource(CDPAWorker._waiting_dom)
    assert ".wait_for_response(" in method_source
    assert "retry_generation(" not in method_source


def test_identity_landing_during_dom_wait_reconciles_before_responded(tmp_path: Path):
    from dataclasses import replace

    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-conversation-during-wait"
    )
    ledger = RequestLedger(hop["ledger_path"])
    response = MessageSnapshot("assistant", "a1", "ta1", "answer", ())
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
        response_activity_turn_id=None,
        response_activity_text="",
        response_activity_structure="",
        response_activity_length=0,
    )

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot
        async def wait_for_response(self, _receipt, **_kwargs):
            async def land_identity():
                ledger.update(
                    hop["request_id"],
                    receipt=replace(receipt, conversation_id="conversation-during-wait").to_dict(),
                )
            asyncio.create_task(land_identity())
            return response
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("backend completion must stay dormant")
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("graph completion must stay dormant")

    acquired = AcquiredRole(Client(), receipt.binding.page_id, "https://chatgpt.com/c/x", False, False)
    class Actions:
        async def locate_owned(self, _state, _role): return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert hop["state"] == "responded"
    assert hop["receipt"]["conversation_id"] == "conversation-during-wait"


def test_final_reconciliation_completion_is_dom_only(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-final-reconcile-dom-only"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-final-reconcile-dom-only.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("final reconciliation", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-final-dom",
        "ta-final-dom",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )

    class Client:
        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("final reconciliation must remain DOM-only")
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("final reconciliation must remain DOM-only")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/final-dom", False, False
    )
    completed = asyncio.run(
        worker._final_response_reconciliation(state, hop, acquired, receipt, hop["wait"])
    )
    assert completed is True
    assert hop["state"] == "responded"
    assert hop["response"] == response.text


def donor_pool_record(*, donors=None, prewarm_prompt=None, max_backups=7):
    return {
        "bootstrap_id": "general-team-bootstrap",
        "name": "General Team Bootstrap",
        "description": "Reusable task-neutral context",
        "source_conversation_id": "11111111-1111-4111-8111-111111111111",
        "prewarm_prompt": prewarm_prompt,
        "max_backups": max_backups,
        "donors": list(
            donors
            if donors is not None
            else [
                {
                    "conversation_id": "11111111-1111-4111-8111-111111111111",
                    "assistant_message_id": "22222222-2222-4222-8222-222222222222",
                }
            ]
        ),
        "enabled": True,
        "tags": ["general"],
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:00+00:00",
    }


def _bootstrap_graph(assistant_id: str):
    return {
        "current_node": assistant_id,
        "mapping": {
            "bootstrap-user": {
                "id": "bootstrap-user",
                "message": {
                    "id": "bootstrap-user",
                    "author": {"role": "user"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["seed"]},
                },
                "parent": None,
                "children": [assistant_id],
            },
            assistant_id: {
                "id": assistant_id,
                "message": {
                    "id": assistant_id,
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["bootstrap"]},
                },
                "parent": "bootstrap-user",
                "children": [],
            },
        },
    }


def test_stream_status_poll_delay_is_randomized_at_or_above_30_seconds(tmp_path: Path):
    _, _, _, worker = setup_task(tmp_path, task_id="task-stream-jitter")
    samples = [worker._stream_status_poll_delay() for _ in range(100)]
    assert all(30.0 <= value <= 35.0 for value in samples)
    assert len({round(value, 4) for value in samples}) > 1


def test_bootstrap_keeper_compatibility_is_retired_from_worker():
    assert not hasattr(CDPAWorker, "_wait_for_bootstrap_repair")
    assert not hasattr(CDPAWorker, "_release_bootstrap_repair_wait")


def test_shared_automated_send_gate_enforces_remaining_spacing(tmp_path: Path, monkeypatch):
    _, _, _, worker = setup_task(tmp_path, task_id="task-send-spacing")
    worker._last_automated_send_at = worker_module.time.monotonic() - 5.0
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(worker_module.asyncio, "sleep", fake_sleep)
    calls = []

    async def send_once():
        calls.append("send")
        return "accepted"

    before = worker_module.time.monotonic()
    result = asyncio.run(worker._run_automated_send(send_once))

    assert result == "accepted"
    assert calls == ["send"]
    assert len(sleeps) == 1 and 4.5 <= sleeps[0] <= 5.0
    assert worker._last_automated_send_at is not None
    assert worker._last_automated_send_at >= before


def test_first_allocation_keeps_catalog_primary_before_task_backup(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    root_donor = donor_pool_record()["donors"][0]
    record = catalog.upsert(donor_pool_record())
    store = TaskStore(config)
    state = store.create_task(
        "prefer stable primary donor",
        requested_team="donor-preference",
        task_id="task-donor-preference",
        bootstrap=record,
    )
    child_donor = {
        "conversation_id": "33333333-3333-4333-8333-333333333333",
        "assistant_message_id": "44444444-4444-4444-8444-444444444444",
    }
    state["bootstrap_task_donors"] = [child_donor]
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branches = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            donor = child_donor if conversation_id == child_donor["conversation_id"] else root_donor
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branches.append((source_conversation_id, assistant_message_id))
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id=f"branch-{role.lower()}",
                url=f"https://chatgpt.com/c/{source_conversation_id}",
                created=True,
                new_chat=True,
            )

    actions = Actions()
    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", actions))

    assert acquired is not None
    assert actions.branches == [
        (root_donor["conversation_id"], root_donor["assistant_message_id"])
    ]
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"


def test_active_rate_limit_gate_blocks_ui_bootstrap_before_new_page(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "block UI bootstrap during cooldown",
        requested_team="ui-bootstrap-gated",
        task_id="task-ui-bootstrap-gated",
        bootstrap=donor_pool_record(),
    )
    worker = CDPAWorker(config, store=store)
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-08-09T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }
    donor = donor_pool_record()["donors"][0]

    class Actions:
        browser_context = SimpleNamespace(
            new_page=lambda: pytest.fail("cooldown must block UI page creation")
        )

    with pytest.raises(RateLimitBlockedError, match="cooldown"):
        asyncio.run(worker._branch_from_bootstrap_ui(state, "PLAN", Actions(), donor))


def test_transient_donor_backend_failure_retries_without_removing_or_blocking(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record())
    store = TaskStore(config)
    state = store.create_task(
        "transient donor read",
        requested_team="donor-transient",
        task_id="task-donor-transient",
        bootstrap=record,
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, _conversation_id):
            raise worker_module.BackendUnavailableError(502, "conversation")

    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", Actions()))

    assert acquired is None
    assert state.get("block_code") is None
    assert state["active_action"] == "bootstrap_retry"
    assert catalog.get(record["bootstrap_id"])["donors"] == record["donors"]


def test_source_only_bootstrap_requires_local_materialization_without_graph_fetch(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(donors=[]))
    store = TaskStore(config)
    state = store.create_task(
        "materialize source donor",
        requested_team="donor-source",
        task_id="task-donor-source",
        bootstrap=record,
    )
    worker = CDPAWorker(config, store=store)
    assistant_id = "22222222-2222-4222-8222-222222222222"

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branches = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, _conversation_id):
            raise AssertionError("source-only bootstrap must not fetch conversation graph")

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branches.append((source_conversation_id, assistant_message_id))
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id=f"branch-{role.lower()}",
                url=f"https://chatgpt.com/c/{source_conversation_id}",
                created=True,
                new_chat=True,
            )

    actions = Actions()
    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", actions))

    assert acquired is None
    assert actions.branches == []
    assert catalog.get(record["bootstrap_id"])["donors"] == []
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "bootstrap_unavailable"
    assert state["bootstrap_source_requires_local_materialization"] == record["source_conversation_id"]


def test_active_rate_limit_gate_blocks_prewarm_before_global_role_acquisition(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(
        donor_pool_record(donors=[], prewarm_prompt="Load reusable bootstrap context only.")
    )
    store = TaskStore(config)
    state = store.create_task(
        "do not prewarm during cooldown",
        requested_team="donor-prewarm-gated",
        task_id="task-donor-prewarm-gated",
        bootstrap=record,
    )
    state["bootstrap_source_exhausted"] = True
    worker = CDPAWorker(config, store=store)
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-08-09T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }

    class Actions(FakeActions):
        async def acquire_global_role(self, physical_role, *, allow_create=True):
            assert physical_role == "BOOTSTRAP"
            assert allow_create is False
            raise RoleOwnershipError("global role has no open tab")

    updated = asyncio.run(worker._regenerate_bootstrap_donor(state, record, Actions()))

    assert updated is None
    assert state["active_action"] == "rate_limit_cooldown"


def test_prewarm_regenerates_donor_through_shared_send_gate(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(
        donor_pool_record(donors=[], prewarm_prompt="Load reusable bootstrap context only.")
    )
    store = TaskStore(config)
    state = store.create_task(
        "regenerate donor",
        requested_team="donor-prewarm",
        task_id="task-donor-prewarm",
        bootstrap=record,
    )
    state["bootstrap_source_exhausted"] = True
    worker = CDPAWorker(config, store=store)
    calls = []

    class Client:
        binding = SimpleNamespace(role="BOOTSTRAP")
        page = SimpleNamespace(url="https://chatgpt.com/c/33333333-3333-4333-8333-333333333333")

        async def new_chat(self, **_kwargs):
            calls.append("new_chat")
            return "https://chatgpt.com/"

        async def assert_ownership(self):
            return SimpleNamespace(
                page_role="BOOTSTRAP",
                page_task_id=None,
                page_team=None,
                url="https://chatgpt.com/c/33333333-3333-4333-8333-333333333333",
            )

    class Actions(FakeActions):
        async def acquire_global_role(self, physical_role, *, allow_create=True):
            assert allow_create is True
            calls.append(("acquire_global_role", physical_role))
            return AcquiredRole(
                client=Client(),
                page_id="bootstrap-global",
                url="https://chatgpt.com/",
                created=True,
                new_chat=False,
            )

    class FakeBlock:
        def __init__(self, prompt, **kwargs):
            source_context = kwargs["source_context"]
            assert source_context["kind"] == "bootstrap_prewarm"
            assert source_context["origin_task_id"] == state["task_id"]
            assert "task_id" not in source_context
            assert "team" not in source_context
            calls.append(("block", prompt, kwargs))

        async def run(self, context):
            calls.append(("block.run", context.client.binding.role))
            return {
                "receipt": {
                    "conversation_id": "33333333-3333-4333-8333-333333333333"
                },
                "response": {
                    "message_id": "44444444-4444-4444-8444-444444444444",
                    "role": "assistant",
                    "turn_id": "55555555-5555-4555-8555-555555555555",
                    "text": "bootstrap ready",
                    "attachments": [],
                },
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeBlock)
    original_gate = worker._run_automated_send

    async def tracked_gate(operation):
        calls.append("send_gate")
        return await original_gate(operation)

    monkeypatch.setattr(worker, "_run_automated_send", tracked_gate)

    updated = asyncio.run(
        worker._regenerate_bootstrap_donor(state, record, Actions())
    )

    donor = {
        "conversation_id": "33333333-3333-4333-8333-333333333333",
        "assistant_message_id": "44444444-4444-4444-8444-444444444444",
    }
    assert updated is not None
    assert updated["donors"] == [donor]
    assert catalog.get(record["bootstrap_id"])["donors"] == [donor]
    assert state["bootstrap_prewarm_donor"] == donor
    assert ("acquire_global_role", "BOOTSTRAP") in calls
    assert "new_chat" in calls
    assert "send_gate" in calls


def test_exhausted_source_without_prewarm_blocks_without_fresh_fallback(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(donors=[]))
    store = TaskStore(config)
    state = store.create_task(
        "lost donors",
        requested_team="donor-lost",
        task_id="task-donor-lost",
        bootstrap=record,
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, _conversation_id):
            raise worker_module.BackendUnavailableError(404, "conversation")

    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", Actions()))

    assert acquired is None
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "bootstrap_unavailable"
    assert state["bootstrap_source_requires_local_materialization"] == record["source_conversation_id"]
    assert "select another bootstrap" in state["block_reason"].lower()


def test_accepted_first_role_self_clones_child_local_bootstrap_donor(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(max_backups=2))
    store = TaskStore(config)
    state = store.create_task(
        "capture descendant donor",
        requested_team="donor-capture",
        task_id="task-donor-capture",
        bootstrap=record,
    )
    role = state["roles"]["PLAN"]
    role["context_source"] = "bootstrap_donor"
    role["conversation_generation"] = 1
    role["bootstrap_source_donor"] = dict(record["donors"][0])
    hop = _active_hop(state)
    child_conversation = "33333333-3333-4333-8333-333333333333"
    inherited_assistant = "44444444-4444-4444-8444-444444444444"
    accepted_user = "55555555-5555-4555-8555-555555555555"
    role_response = "66666666-6666-4666-8666-666666666666"
    hop["receipt"] = {
        "conversation_id": child_conversation,
        "user_message_id": accepted_user,
    }
    worker = CDPAWorker(config, store=store)
    snapshot = SimpleNamespace(
        messages=(
            MessageSnapshot("assistant", inherited_assistant, "turn-bootstrap", "bootstrap prefix", ()),
            MessageSnapshot("user", accepted_user, "turn-user", "PLAN prompt", ()),
            MessageSnapshot("assistant", role_response, "turn-response", "PLAN response", ()),
        )
    )
    client = SimpleNamespace(assert_ownership=AsyncMock(return_value=snapshot))
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url=f"https://chatgpt.com/c/{child_conversation}",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

        async def backend_conversation(self, _conversation_id):
            raise AssertionError("bootstrap donor capture must use local transcript state")

    captured = asyncio.run(worker._capture_bootstrap_role_donor(state, hop, Actions()))

    donor = {
        "conversation_id": child_conversation,
        "assistant_message_id": inherited_assistant,
    }
    assert captured is True
    assert state["bootstrap_task_donors"][0] == donor
    assert role["bootstrap_donor"] == donor
    assert catalog.get(record["bootstrap_id"])["donors"] == [record["donors"][0], donor]


def test_aliased_role_conversation_is_not_persisted_as_descendant_bootstrap_donor(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(max_backups=2))
    source_donor = dict(record["donors"][0])
    store = TaskStore(config)
    state = store.create_task(
        "reject aliased descendant donor",
        requested_team="donor-alias",
        task_id="task-donor-alias",
        bootstrap=record,
    )
    role = state["roles"]["PLAN"]
    role["context_source"] = "bootstrap_donor"
    role["conversation_generation"] = 1
    role["bootstrap_source_donor"] = source_donor
    hop = _active_hop(state)
    hop["receipt"] = {
        "conversation_id": source_donor["conversation_id"],
        "user_message_id": "55555555-5555-4555-8555-555555555555",
    }
    worker = CDPAWorker(config, store=store)

    class Actions:
        async def backend_conversation(self, _conversation_id):
            raise AssertionError("aliased donor must be rejected before backend capture")

    captured = asyncio.run(worker._capture_bootstrap_role_donor(state, hop, Actions()))

    assert captured is False
    assert state.get("bootstrap_task_donors") in (None, [])
    assert role.get("bootstrap_donor") is None
    assert catalog.get(record["bootstrap_id"])["donors"] == record["donors"]


def _accept_self_route_guard_decision(worker, state, route: str):
    hop = _active_hop(state)
    state["roles"][str(hop["target_role"])]["turn"] = int(hop["turn"])
    handoff = (
        f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_"
        f"{state['task_id']}.md"
    )
    hop["expected_report_path"] = handoff
    report = Path(str(state["repository"])) / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"# {hop['physical_role']} turn {hop['turn']} report\n", encoding="utf-8")
    hop["response"] = json.dumps({"route": route, "handoff": handoff})
    hop["state"] = "responded"
    worker._responded(state, hop)
    return hop


def test_consecutive_self_route_guard_blocks_third_without_child_or_send_after_reload(
    tmp_path: Path, monkeypatch
):
    config, store, state, worker = setup_task(tmp_path, task_id="task-self-route-limit")

    first = _accept_self_route_guard_decision(worker, state, "PLAN")
    second = _accept_self_route_guard_decision(worker, state, "PLAN")
    third = _accept_self_route_guard_decision(worker, state, "PLAN")

    assert first["state"] == second["state"] == third["state"] == "routed"
    assert len(state["hops"]) == 3
    assert state["active_hop_id"] == third["hop_id"]
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "consecutive_self_route_limit"
    assert state["block_retryable"] is False
    assert "PLAN" in state["block_reason"]
    assert "3" in state["block_reason"]
    assert "operator Resume" in state["block_reason"]
    assert [item.get("kind") for item in state["route_timeline"]] == [None, None, None]
    assert len(state["reports"]) == 3
    assert not any(hop["request_id"].endswith("-hop4") for hop in state["hops"])

    path = Path(state["manifest_path"])
    store.save(path, state)
    restarted = CDPAWorker(config, store=store)

    async def forbidden_pre_send(*_args, **_kwargs):
        raise AssertionError("guarded reload must not reach _pre_send")

    monkeypatch.setattr(restarted, "_pre_send", forbidden_pre_send)
    reloaded = asyncio.run(restarted.advance(path, SimpleNamespace(pages=[])))

    assert reloaded["status"] == "BLOCKED"
    assert reloaded["block_code"] == "consecutive_self_route_limit"
    assert len(reloaded["hops"]) == 3
    assert len(reloaded["reports"]) == 3
    assert len(reloaded["route_timeline"]) == 3
    assert RequestLedger(third["ledger_path"]).peek("task-self-route-limit-hop4") is None


def test_consecutive_self_route_guard_resets_on_normal_role_change(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-self-route-role-reset",
        roles=("PLAN", "DEV"),
    )

    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "DEV")
    assert state["active_role"] == "DEV"

    _accept_self_route_guard_decision(worker, state, "DEV")
    _accept_self_route_guard_decision(worker, state, "DEV")
    third_dev = _accept_self_route_guard_decision(worker, state, "DEV")

    assert state["status"] == "BLOCKED"
    assert state["active_hop_id"] == third_dev["hop_id"]
    assert len([item for item in state["route_timeline"] if item.get("source_role") == "DEV"]) == 3


def test_consecutive_self_route_guard_applies_to_custom_workflow_role(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    custom = store.create_workflow_agent(
        display_name="Custom loop role",
        system_prompt="Perform the assigned custom workflow role.",
        external_command_id="create-custom-loop-role",
    )["route_key"]
    state = store.create_task(
        "Exercise custom self-route guard",
        requested_team="custom-loop",
        task_id="task-custom-self-route-limit",
        roles=("PLAN", custom),
    )
    worker = CDPAWorker(config, store=store)

    _accept_self_route_guard_decision(worker, state, custom)
    _accept_self_route_guard_decision(worker, state, custom)
    _accept_self_route_guard_decision(worker, state, custom)
    third = _accept_self_route_guard_decision(worker, state, custom)

    assert custom.startswith("WF_")
    assert state["status"] == "BLOCKED"
    assert state["active_hop_id"] == third["hop_id"]
    assert custom in state["block_reason"]


def test_route_repair_artifacts_neither_count_nor_reset_self_route_streak(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-self-route-repair")

    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    malformed = _active_hop(state)
    malformed["response"] = "not json"
    malformed["state"] = "responded"
    worker._responded(state, malformed)
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"

    _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["status"] == "RUNNING"
    assert _active_hop(state)["kind"] == "handoff"

    third_normal = _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["status"] == "BLOCKED"
    assert state["active_hop_id"] == third_normal["hop_id"]


def test_goal_revision_resets_self_route_streak_at_applies_from_hop_boundary(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-self-route-goal-reset")

    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    boundary = int(state["active_hop_id"])
    state["goal_revisions"] = [
        {
            "revision": 1,
            "changed_at": utc_now(),
            "applies_from_hop_id": boundary,
            "goal": "Revised goal",
            "external_command_id": "goal-reset-self-route",
        }
    ]
    state["effective_goal"] = "Revised goal"

    _accept_self_route_guard_decision(worker, state, "PLAN")

    assert state["status"] == "RUNNING"
    assert state["active_hop_id"] == boundary + 1


@pytest.mark.parametrize("operational_kind", ["role_restart", "control"])
def test_operational_hop_decision_neither_counts_nor_resets_self_route_streak(
    tmp_path: Path, operational_kind: str
):
    _, _, state, worker = setup_task(
        tmp_path, task_id=f"task-self-route-{operational_kind}"
    )
    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")

    operational = _active_hop(state)
    operational["kind"] = operational_kind
    _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["status"] == "RUNNING"

    third_normal = _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "consecutive_self_route_limit"
    assert state["active_hop_id"] == third_normal["hop_id"]


@pytest.mark.parametrize("action", ["open_tab", "new_chat"])
def test_rate_limit_defers_only_missing_tab_control(tmp_path: Path, action: str):
    _, _, state, worker = setup_task(
        tmp_path, task_id=f"task-rate-limit-missing-{action}"
    )
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-08-17T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }
    state["controls"] = [{
        "control_id": 1, "action": action, "role": "PLAN",
        "reason": "new tab is paused", "confirmed": False,
        "status": "requested", "requested_at": "2026-08-17T00:00:00+00:00",
        "applied_at": None, "result": None,
    }]

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return None
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("cooldown must not reopen/create a missing tab")
        async def new_chat(self, *_args, **_kwargs):
            raise AssertionError("cooldown must not create a missing tab")

    assert asyncio.run(worker._apply_control(state, Actions())) is True
    control = state["controls"][0]
    assert control["status"] == "applied"
    assert control["result"] == {"deferred": True, "reason": "rate_limit_cooldown"}
    assert state["active_action"] == "rate_limit_cooldown"


def test_active_rate_limit_blocks_ui_bootstrap_new_page_only(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "block only new UI page", requested_team="ui-gated", task_id="task-ui-gated",
        bootstrap=donor_pool_record(),
    )
    worker = CDPAWorker(config, store=store)
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-08-17T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }
    donor = donor_pool_record()["donors"][0]
    class Actions:
        browser_context = SimpleNamespace(
            new_page=lambda: pytest.fail("cooldown must block new_page")
        )
    with pytest.raises(RateLimitBlockedError, match="cooldown"):
        asyncio.run(worker._branch_from_bootstrap_ui(state, "PLAN", Actions(), donor))


def test_active_mcp_allow_falls_back_to_plain_visible_allow(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-active-plain-allow"
    )
    hop["wait"]["mcp_allow_seen_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=6)
    ).isoformat()
    calls = []

    class Client:
        def passive_observation(self, **_kwargs):
            return {"coverage": "unknown"}

        async def mcp_allow_visible(self):
            return True

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            calls.append(passive_action)
            return {"method": "dom_click", "target_message_id": "", "remember_answer": "false"}

    snapshot = SimpleNamespace(stop_visible=False, messages=())
    handled = asyncio.run(
        worker._mcp_allow_interrupt(state, hop, Client(), receipt, snapshot)
    )

    assert handled is True
    assert calls == [None]
    assert hop["wait"].get("mcp_allow_clicked_at")
    assert state["active_action"] == "wait_mcp_allow_continuation"


def test_mcp_allow_disappearance_without_continuation_refreshes_after_five_seconds(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-allow-disappear-refresh"
    )
    hop["wait"]["mcp_allow_clicked_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=6)
    ).isoformat()
    refreshed = []

    class Client:
        async def mcp_allow_visible(self):
            return False

        async def refresh(self):
            refreshed.append(True)

    snapshot = SimpleNamespace(
        stop_visible=False,
        messages=(),
        response_activity_turn_id=None,
        response_activity_length=0,
        response_activity_text="",
    )
    handled = asyncio.run(
        worker._mcp_allow_interrupt(state, hop, Client(), receipt, snapshot)
    )

    assert handled is True
    assert refreshed == [True]
    assert state["active_action"] == "wait_response"


def test_mcp_allow_stop_after_dispatch_is_real_continuation_progress(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-allow-stop-progress"
    )
    hop["wait"]["mcp_allow_clicked_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=6)
    ).isoformat()
    refreshed = []

    class Client:
        async def mcp_allow_visible(self):
            return False

        async def refresh(self):
            refreshed.append(True)

    snapshot = SimpleNamespace(
        stop_visible=True,
        messages=(),
        response_activity_turn_id=None,
        response_activity_length=0,
        response_activity_text="",
    )
    handled = asyncio.run(
        worker._mcp_allow_interrupt(state, hop, Client(), receipt, snapshot)
    )

    assert handled is False
    assert refreshed == []
    assert "mcp_allow_clicked_at" not in hop["wait"]


def test_stale_response_state_refreshes_after_ten_minutes_without_activity(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stale-response-refresh"
    )
    old = datetime.now(timezone.utc) - timedelta(seconds=601)
    hop["wait"]["controller_state"] = "RESPONSE"
    hop["wait"]["controller_state_since"] = old.isoformat()
    hop["wait"]["activity_changed_at"] = old.isoformat()
    refreshed = []

    class Client:
        async def refresh(self):
            refreshed.append(True)

    snapshot = SimpleNamespace(
        retry_visible=False,
        stop_visible=False,
        response_activity_turn_id=None,
        messages=(MessageSnapshot("assistant", "a-stale", None, "partial", ()),),
    )
    handled = asyncio.run(
        worker._refresh_stalled_controller_state(state, hop, Client(), snapshot, receipt)
    )

    assert handled is True
    assert refreshed == [True]


def test_recent_response_activity_prevents_ten_minute_refresh_even_if_stop_stays_visible(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-recent-activity-no-refresh"
    )
    hop["wait"]["controller_state"] = "STOP"
    hop["wait"]["controller_state_since"] = (
        datetime.now(timezone.utc) - timedelta(seconds=601)
    ).isoformat()
    hop["wait"]["activity_changed_at"] = datetime.now(timezone.utc).isoformat()
    refreshed = []

    class Client:
        async def refresh(self):
            refreshed.append(True)

    snapshot = SimpleNamespace(
        retry_visible=False,
        stop_visible=True,
        response_activity_turn_id="streaming-turn",
        messages=(),
    )
    handled = asyncio.run(
        worker._refresh_stalled_controller_state(state, hop, Client(), snapshot, receipt)
    )

    assert handled is False
    assert refreshed == []


@pytest.mark.parametrize("status", ["DONE", "STOPPED", "PAUSED", "BLOCKED"])
def test_ambient_old_registered_tabs_keep_auto_allow(status: str, tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id=f"task-ambient-history-{status.lower()}"
    )
    task_id = f"task-ambient-history-{status.lower()}"
    worker.registry = SimpleNamespace(tasks_by_id={task_id: {"status": status}})
    calls = []

    class Page:
        url = "https://chatgpt.com/c/ambient-history"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({"taskId": task_id})

    page = Page()

    class Client:
        def __init__(self, _page, *, timeout_ms):
            self.page = _page

        def install_ambient_observer(self):
            calls.append("listen")

        async def read_wait_probe(self):
            return SimpleNamespace(
                page_task_id=task_id,
                page_id="page-history",
                page_role="PLAN",
                stop_visible=False,
                last_assistant_message_id=None,
            )

        def ambient_permission_action(self):
            return {
                "type": "allow",
                "target_message_id": "history-call",
                "remember_answer": True,
                "label": "Allow mcp-g8 for this conversation",
            }

        async def mcp_allow_visible(self):
            return False

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            calls.append(("allow", passive_action["target_message_id"]))
            return {"method": "react_handler", "target_message_id": passive_action["target_message_id"]}

        def clear_ambient_permission_action(self):
            calls.append("clear")

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    worker._ambient_allow_seen[id(page)] = time.monotonic() - 6
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[page])))

    assert ("allow", "history-call") in calls
    assert "clear" in calls


def test_ambient_dom_fallback_is_sparse_not_command_loop_rate(tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-sparse"
    )
    calls = []

    class Page:
        url = "https://chatgpt.com/c/ambient-sparse"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            calls.append("binding")
            return worker_module.WINDOW_NAME_PREFIX + "{}"

    page = Page()

    class Client:
        def __init__(self, _page, *, timeout_ms):
            pass

        def install_ambient_observer(self):
            calls.append("listen")

        async def read_wait_probe(self):
            calls.append("probe")
            return SimpleNamespace(
                page_task_id=None,
                page_id=None,
                page_role=None,
                stop_visible=False,
                last_assistant_message_id=None,
            )

        def ambient_permission_action(self):
            return None

        async def mcp_allow_visible(self):
            return False

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    context = SimpleNamespace(pages=[page])
    asyncio.run(worker._maintain_ambient_page_automation(context))
    asyncio.run(worker._maintain_ambient_page_automation(context))

    assert calls.count("probe") == 1


def test_ambient_old_tab_dom_only_visible_allow_is_clicked_without_passive_event(tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-history-dom-allow"
    )
    task_id = "task-ambient-history-dom-allow"
    worker.registry = SimpleNamespace(tasks_by_id={task_id: {"status": "DONE"}})
    calls = []

    class Page:
        url = "https://chatgpt.com/c/ambient-history-dom"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": task_id,
                "pageId": "page-history-dom",
                "role": "cdpa-history-dom-plan",
            })

    page = Page()

    class Client:
        def __init__(self, _page, *, timeout_ms):
            self.binding = None

        def install_ambient_observer(self):
            pass

        async def read_wait_probe(self):
            return SimpleNamespace(
                page_task_id=task_id,
                page_id="page-history-dom",
                page_role="cdpa-history-dom-plan",
                stop_visible=False,
                last_assistant_message_id=None,
                mcp_permission_allow_count=1,
            )

        def ambient_permission_action(self):
            return None

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            assert self.binding == PageBinding("page-history-dom", "cdpa-history-dom-plan")
            calls.append(passive_action)
            return {"method": "dom_click", "target_message_id": "", "remember_answer": "false"}

        def clear_ambient_permission_action(self):
            pass

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    worker._ambient_allow_seen[id(page)] = time.monotonic() - 6
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[page])))

    assert calls == [None]


def test_ambient_page_local_ownership_error_does_not_reconnect_worker(tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-local-ownership-error"
    )
    task_id = "task-ambient-local-ownership-error"
    worker.registry = SimpleNamespace(tasks_by_id={task_id: {"status": "DONE"}})

    class Page:
        url = "https://chatgpt.com/c/ambient-local-error"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": task_id,
                "pageId": "page-local-error",
                "role": "cdpa-local-error-plan",
            })

    page = Page()

    class Client:
        def __init__(self, _page, *, timeout_ms):
            self.binding = None

        def install_ambient_observer(self):
            pass

        async def read_wait_probe(self):
            return SimpleNamespace(
                page_task_id=task_id,
                page_id="page-local-error",
                page_role="cdpa-local-error-plan",
                stop_visible=False,
                last_assistant_message_id=None,
                mcp_permission_allow_count=0,
            )

        def ambient_permission_action(self):
            return None

        async def refresh(self):
            raise PageOwnershipError("stale historical page binding")

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    worker._ambient_post_click[id(page)] = (time.monotonic() - 6, None)

    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[page])))

    assert id(page) not in worker._ambient_post_click
