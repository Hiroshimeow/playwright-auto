from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    PageOwnershipError,
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
        "allowed-routes": ["PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"],
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

    class Button:
        def __init__(self, page, label):
            self.page = page
            self.label = label

        async def click(self):
            calls.append(("click", self.label))
            if self.label == "Branch in new chat":
                self.page.url = "https://chatgpt.com/c/ui-branched-conversation"

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

        async def goto(self, url, *, wait_until, timeout):
            self.url = url
            calls.append(("goto", url, wait_until, timeout))

        def locator(self, selector):
            calls.append(("assistant-selector", selector))
            return Assistant(self)

        def get_by_role(self, role, *, name, exact):
            calls.append(("page-role", role, name, exact))
            return Button(self, name)

        async def wait_for_url(self, predicate, *, wait_until, timeout):
            calls.append(("wait-url", wait_until, timeout))
            assert predicate(self.url)

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()

    class Context:
        async def new_page(self):
            return page

    class Client:
        def __init__(self):
            self.binding = PageBinding("ui-page", "alpha-plan")

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
    acquired = asyncio.run(
        worker._branch_from_bootstrap_ui(
            state,
            "PLAN",
            SimpleNamespace(browser_context=Context()),
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


def test_pre_send_limits_allowed_routes_to_selected_workflow_roles(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, roles=("PLAN", "REVIEW"))
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    envelope, _ = json.JSONDecoder().raw_decode(
        hop["prompt"].removeprefix("alpha · role: plan\n")
    )
    assert envelope["allowed-routes"] == ["PLAN", "REVIEW", "DONE"]
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




def test_path_only_report_routes_without_local_file_io(tmp_path: Path, monkeypatch):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    handoff = ".plan/remote-team/remote-plan_turn1_task-a.md"
    report_target = tmp_path / handoff
    original_stat = Path.stat
    original_read_bytes = Path.read_bytes
    original_is_file = Path.is_file

    def reject_report_stat(path, *args, **kwargs):
        if path == report_target:
            raise AssertionError("normal routing must not stat the report target")
        return original_stat(path, *args, **kwargs)

    def reject_report_read_bytes(path, *args, **kwargs):
        if path == report_target:
            raise AssertionError("normal routing must not read the report target")
        return original_read_bytes(path, *args, **kwargs)

    def reject_report_is_file(path, *args, **kwargs):
        if path == report_target:
            raise AssertionError("normal routing must not inspect report file type")
        return original_is_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", reject_report_stat)
    monkeypatch.setattr(Path, "read_bytes", reject_report_read_bytes)
    monkeypatch.setattr(Path, "is_file", reject_report_is_file)
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["state"] == "routed"
    assert hop["report_path"] == handoff
    assert hop["report_sha256"] is None
    assert hop["report_size"] is None
    assert child["target_role"] == "DEV"
    assert child["state"] == "pre_send"
    assert child["handoff"] == handoff
    assert state["active_role"] == "DEV"
    assert state["roles"]["DEV"]["status"] == "pending"
    assert state["reports"] == [
        {
            "report_id": 1,
            "role": "PLAN",
            "physical_role": hop["physical_role"],
            "turn": hop["turn"],
            "path": handoff,
            "sha256": None,
            "size": None,
            "created_at": state["reports"][0]["created_at"],
        }
    ]
    assert state["route_timeline"][-1]["report_path"] == handoff






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
    assert _active_hop(waiting)["state"] == "waiting"
    assert _active_hop(waiting)["request_id"] == original_request_id
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


def test_backend_primary_is_streaming_uses_status_cadence_without_dom_or_graph(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stream-primary"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    calls = {"status": 0, "graph": 0, "dom": 0, "source": 0}

    class Actions:
        async def backend_stream_status(self, conversation_id):
            assert conversation_id == "conversation-1"
            calls["status"] += 1
            return {"status": "IS_STREAMING"}
        async def backend_conversation(self, _conversation_id):
            calls["graph"] += 1
            raise AssertionError("IS_STREAMING must not fetch full conversation")
        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("healthy stream-status polling must not depend on source tab")
        async def locate_owned(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("healthy stream-status polling must not inspect source DOM")
        async def reopen(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("healthy stream-status polling must not reopen source")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert calls == {"status": 1, "graph": 0, "dom": 0, "source": 0}
    assert hop["state"] == "waiting"
    assert hop["wait"]["completion_mode"] == "stream_status"
    assert hop["wait"]["stream_status_poll_count"] == 1


def test_is_streaming_does_not_reopen_missing_source_while_backend_is_healthy(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stream-source-reopen"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    calls = {"status": 0, "source": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "IS_STREAMING"}
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("IS_STREAMING must not fetch graph")
        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("healthy backend wait must tolerate a closed source tab")
        async def locate_owned(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("healthy backend wait must tolerate a closed source tab")
        async def reopen(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("healthy backend wait must not reopen source")

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert calls == {"status": 1, "source": 0}
    assert hop["state"] == "waiting"
    assert state["status"] != "BLOCKED"


def test_backend_complete_waits_terminal_settle_before_one_graph_get(tmp_path: Path):
    from dataclasses import replace

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stream-terminal-settle"
    )
    worker.config = replace(
        worker.config, response_stream_status_terminal_settle_seconds=5.0
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    calls = {"status": 0, "graph": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}
        async def backend_conversation(self, _conversation_id):
            calls["graph"] += 1
            raise AssertionError("graph must wait for the terminal settle deadline")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))
    assert calls == {"status": 1, "graph": 0}
    wait = hop["wait"]
    seen_at = worker_module.parse_time(wait["terminal_complete_seen_at"])
    ready_at = worker_module.parse_time(wait["terminal_graph_ready_at"])
    assert ready_at is not None and seen_at is not None
    assert 4.9 <= (ready_at - seen_at).total_seconds() <= 5.1
    assert "terminal_graph_request_id" not in wait
    persisted_wait = _active_hop(store.load(path))["wait"]
    assert persisted_wait["terminal_graph_ready_at"] == wait["terminal_graph_ready_at"]

    asyncio.run(worker._waiting(state, hop, actions, path))
    assert calls == {"status": 1, "graph": 0}


def test_backend_complete_persists_graph_guard_then_records_exact_response_without_source_reopen(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stream-complete"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    report_relative = hop["expected_report_path"]
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("backend terminal response", encoding="utf-8")
    response_text = json.dumps({"route": "REVIEW", "handoff": report_relative})
    calls = {"status": 0, "graph": 0, "locate": 0, "reopen": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}
        async def backend_conversation(self, _conversation_id):
            calls["graph"] += 1
            persisted_wait = _active_hop(store.load(path))["wait"]
            assert persisted_wait["terminal_graph_request_id"] == hop["request_id"]
            return _backend_graph(receipt.user_message_id, "assistant-final", response_text)
        async def locate_owned(self, *_args, **_kwargs):
            calls["locate"] += 1
            raise AssertionError("exact backend success must not inspect source")
        async def reopen(self, *_args, **_kwargs):
            calls["reopen"] += 1
            raise AssertionError("exact backend success must not reopen source")

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert calls == {"status": 1, "graph": 1, "locate": 0, "reopen": 0}
    assert hop["state"] == "responded"
    assert hop["response"] == response_text
    assert hop["response_record"]["message_id"] == "assistant-final"
    assert hop["response_record"]["turn_id"] is None

    worker._responded(state, hop)
    child = _active_hop(state)
    assert child["target_role"] == "REVIEW"
    assert child["state"] == "pre_send"

    class TargetActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.acquired_roles = []
        async def acquire(self, current, role):
            self.acquired_roles.append(role)
            return await super().acquire(current, role)

    target_actions = TargetActions()
    asyncio.run(worker._pre_send(state, child, target_actions))
    assert target_actions.acquired_roles == ["REVIEW"]
    assert child["state"] == "sending"


def test_advance_complete_backend_result_survives_pregraph_guard_outer_persistence(
    tmp_path: Path,
    monkeypatch,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stream-advance-complete"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    report_relative = hop["expected_report_path"]
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("advance backend terminal response", encoding="utf-8")
    response_text = json.dumps({"route": "REVIEW", "handoff": report_relative})

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            return {"status": "COMPLETE"}
        async def backend_conversation(self, _conversation_id):
            persisted_wait = _active_hop(store.load(path))["wait"]
            assert persisted_wait["terminal_graph_request_id"] == hop["request_id"]
            return _backend_graph(receipt.user_message_id, "assistant-advance", response_text)
        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("backend success must not inspect source")
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("backend success must not reopen source")

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())
    saved = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    saved_hop = _active_hop(saved)

    assert saved == store.load(path)
    assert saved_hop["state"] == "responded"
    assert saved_hop["response"] == response_text
    assert saved_hop["wait"]["terminal_graph_request_id"] == saved_hop["request_id"]


def test_complete_graph_not_ready_retries_after_two_minutes_without_source_wake(tmp_path: Path):
    from playwright_auto.chatgpt_graph import BackendNotReadyError

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stale-complete"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    calls = {"status": 0, "graph": 0, "source": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}
        async def backend_conversation(self, _conversation_id):
            calls["graph"] += 1
            raise BackendNotReadyError("terminal assistant response is not materialized yet")
        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("terminal graph retry must not inspect source")
        async def locate_owned(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("terminal graph retry must not wake source")
        async def reopen(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("terminal graph retry must not reopen source")
        async def wake(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("terminal graph retry must not wake source")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    wait = hop["wait"]
    assert calls == {"status": 1, "graph": 1, "source": 0}
    assert wait["completion_mode"] == "terminal_graph_retry"
    assert wait["backend_fallback_category"] == "graph_not_ready"
    assert wait["terminal_graph_attempts"] == 1
    retry_at = worker_module.parse_time(wait["terminal_graph_ready_at"])
    assert retry_at is not None
    assert 119 <= (retry_at - datetime.now(timezone.utc)).total_seconds() <= 121

    asyncio.run(worker._waiting(state, hop, actions, path))
    assert calls == {"status": 1, "graph": 1, "source": 0}


@pytest.mark.parametrize(
    ("error_factory", "expected_category"),
    [
        (lambda: worker_module.BackendUnavailableError(429, "conversation"), "graph_unavailable"),
        (lambda: worker_module.BackendUnavailableError(0, "conversation"), "graph_unavailable"),
        (lambda: worker_module.BackendAuthError("auth failed"), "graph_auth"),
        (lambda: worker_module.BackendSchemaError("schema changed"), "graph_schema"),
        (lambda: worker_module.GraphIdentityError("identity ambiguous"), "graph_identity"),
    ],
)
def test_complete_graph_failure_classes_use_bounded_retry_without_source(
    tmp_path: Path,
    error_factory,
    expected_category: str,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id=f"task-graph-retry-{expected_category}"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    calls = {"status": 0, "graph": 0, "source": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}
        async def backend_conversation(self, _conversation_id):
            calls["graph"] += 1
            raise error_factory()
        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("graph retry must not inspect source")
        async def locate_owned(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("graph retry must not inspect source DOM")
        async def reopen(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("graph retry must not reopen source")
        async def wake(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("graph retry must not wake source")

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert calls == {"status": 1, "graph": 1, "source": 0}
    assert state["status"] != "BLOCKED"
    assert hop["wait"]["backend_fallback_category"] == expected_category
    assert hop["wait"]["completion_mode"] == "terminal_graph_retry"
    assert hop["wait"]["terminal_graph_attempts"] == 1


def test_interrupted_terminal_graph_attempt_resumes_bounded_retry(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-graph-marker-restart"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    hop["wait"].update(
        completion_mode="terminal_graph_retry",
        terminal_graph_request_id=hop["request_id"],
        terminal_graph_attempted_at=utc_now(),
        terminal_graph_attempts=1,
        terminal_graph_ready_at=(datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
    )
    state = store.save(path, state)
    hop = _active_hop(state)
    calls = {"status": 0, "graph": 0, "source": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            raise AssertionError("interrupted graph retry must not repoll status")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("interrupted graph retry must wait for its next deadline")
        async def locate_owned(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("interrupted graph retry must not inspect source")

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert calls == {"status": 0, "graph": 0, "source": 0}
    assert hop["wait"]["completion_mode"] == "terminal_graph_retry"
    assert "terminal_graph_request_id" not in hop["wait"]
    assert hop["wait"]["terminal_graph_attempts"] == 1


def test_stream_status_failure_enters_recovery_without_dom_or_source(tmp_path: Path):
    from playwright_auto.chatgpt_graph import BackendUnavailableError

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-status-recovery"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    calls = {"status": 0, "graph": 0, "source": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            raise BackendUnavailableError(429, "stream_status")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("first status failure must wait five minutes before graph probe")
        async def locate_owned(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("status recovery must not inspect source")
        async def reopen(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("status recovery must not reopen source")
        async def wake(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("status recovery must not wake source")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    wait = hop["wait"]
    assert calls == {"status": 1, "graph": 0, "source": 0}
    assert wait["completion_mode"] == "status_recovery"
    assert wait["backend_fallback_category"] == "status_unavailable"
    next_status = worker_module.parse_time(wait["stream_status_next_poll_at"])
    next_graph = worker_module.parse_time(wait["status_recovery_graph_next_at"])
    now = datetime.now(timezone.utc)
    assert next_status is not None and 29 <= (next_status - now).total_seconds() <= 31
    assert next_graph is not None and 299 <= (next_graph - now).total_seconds() <= 301

    asyncio.run(worker._waiting(state, hop, actions, path))
    assert calls == {"status": 1, "graph": 0, "source": 0}


def test_status_recovery_returns_to_normal_randomized_poll_when_status_recovers(tmp_path: Path):
    from playwright_auto.chatgpt_graph import BackendUnavailableError

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-status-recovers"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    replies = [BackendUnavailableError(429, "stream_status"), {"status": "IS_STREAMING"}]
    calls = {"status": 0, "graph": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("recovered IS_STREAMING must not fetch graph")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))
    hop["wait"]["stream_status_next_poll_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    asyncio.run(worker._waiting(state, hop, actions, path))

    wait = hop["wait"]
    assert calls == {"status": 2, "graph": 0}
    assert wait["completion_mode"] == "stream_status"
    assert "status_recovery_graph_next_at" not in wait
    next_status = worker_module.parse_time(wait["stream_status_next_poll_at"])
    assert next_status is not None
    assert 9 <= (next_status - datetime.now(timezone.utc)).total_seconds() <= 15


def test_status_recovery_graph_probe_routes_after_five_minutes(tmp_path: Path):
    from playwright_auto.chatgpt_graph import BackendUnavailableError

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-status-graph-route"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    report_relative = hop["expected_report_path"]
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("status recovery graph response", encoding="utf-8")
    response_text = json.dumps({"route": "REVIEW", "handoff": report_relative})
    calls = {"status": 0, "graph": 0, "source": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            raise BackendUnavailableError(429, "stream_status")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            return _backend_graph(receipt.user_message_id, "assistant-recovery", response_text)
        async def locate_owned(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("successful recovery graph must not inspect source")
        async def reopen(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("successful recovery graph must not reopen source")
        async def wake(self, *_args, **_kwargs):
            calls["source"] += 1
            raise AssertionError("successful recovery graph must not wake source")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))
    hop["wait"]["status_recovery_graph_next_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    state = store.save(path, state)
    hop = _active_hop(state)
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert calls == {"status": 1, "graph": 1, "source": 0}
    assert hop["state"] == "responded"
    assert hop["response"] == response_text


def test_terminal_graph_third_failure_enters_dom_fallback_and_wakes_source(tmp_path: Path):
    from playwright_auto.chatgpt_graph import BackendUnavailableError

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-terminal-graph-max"
    )
    state, hop, receipt = _enable_backend_wait_identity(store, state, path, hop, receipt)
    calls = {"status": 0, "graph": 0, "locate": 0, "wake": 0}
    acquired = AcquiredRole(
        SimpleNamespace(), receipt.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            return {"status": "COMPLETE"}
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise BackendUnavailableError(0, "conversation")
        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["locate"] += 1
            return acquired
        async def reopen(self, *_args, **kwargs):
            raise AssertionError(f"existing source should be reused: {kwargs}")
        async def wake(self, exact):
            assert exact is acquired
            calls["wake"] += 1

    actions = Actions()
    for attempt in range(3):
        if attempt:
            hop["wait"]["terminal_graph_ready_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
        state = store.save(path, state)
        hop = _active_hop(state)
        asyncio.run(worker._waiting(state, hop, actions, path))

    assert calls == {"status": 1, "graph": 3, "locate": 1, "wake": 1}
    assert hop["wait"]["terminal_graph_attempts"] == 3
    assert hop["wait"]["completion_mode"] == "dom_fallback"
    assert hop["wait"]["backend_fallback_category"] == "graph_unavailable"
    assert worker_module.parse_time(hop["wait"]["dom_fallback_ready_at"]) > datetime.now(timezone.utc)


def test_complete_graph_not_ready_refreshes_once_then_blocks_without_replay(
    tmp_path: Path,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-terminal-continuation-unresolved"
    )
    state, hop, receipt = _enable_backend_wait_identity(
        store, state, path, hop, receipt
    )
    snapshot = send_snapshot(
        messages=(
            MessageSnapshot("user", receipt.user_message_id, receipt.user_turn_id, receipt.prompt, ()),
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
            "response_activity_text": "",
            "response_activity_structure": "",
            "response_activity_turn_id": None,
            "response_activity_length": 0,
        }
    )
    calls = {
        "status": 0,
        "graph": 0,
        "locate": 0,
        "wake": 0,
        "refresh": 0,
        "wait": 0,
        "send": 0,
        "retry": 0,
        "restart": 0,
        "new_chat": 0,
    }

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            calls["wait"] += 1
            raise TimeoutError("no terminal assistant continuation")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            return {"status": "COMPLETE"}

        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise worker_module.BackendNotReadyError(
                "terminal assistant response is not materialized yet"
            )

        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["locate"] += 1
            return acquired

        async def locate_owned(self, *_args, **_kwargs):
            calls["locate"] += 1
            return acquired

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("the exact owned page must remain bound")

        async def wake(self, exact):
            assert exact is acquired
            calls["wake"] += 1

        async def refresh(self, exact):
            assert exact is acquired
            calls["refresh"] += 1

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("recovery must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("recovery must not Retry")

        async def restart(self, *_args, **_kwargs):
            calls["restart"] += 1
            raise AssertionError("recovery must not Restart")

        async def new_chat(self, *_args, **_kwargs):
            calls["new_chat"] += 1
            raise AssertionError("recovery must not open New Chat")

    actions = Actions()
    for attempt in range(3):
        if attempt:
            hop["wait"]["terminal_graph_ready_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
        state = store.save(path, state)
        hop = _active_hop(state)
        asyncio.run(worker._waiting(state, hop, actions, path))

    wait = hop["wait"]
    assert wait["completion_mode"] == "dom_fallback"
    assert wait["backend_fallback_category"] == "graph_not_ready"
    assert wait["terminal_continuation_unresolved"]["request_id"] == hop["request_id"]
    assert wait["terminal_continuation_unresolved"]["refresh_baseline"] == 0

    wait["dom_fallback_ready_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert calls["refresh"] == 1
    assert wait["refresh_count"] == 1
    assert worker_module.parse_time(
        wait["terminal_continuation_unresolved"]["block_ready_at"]
    ) > datetime.now(timezone.utc)
    assert state["status"] == "RUNNING"

    state = store.load(path)
    hop = _active_hop(state)
    hop["wait"]["dom_fallback_ready_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    hop["wait"]["terminal_continuation_unresolved"]["block_ready_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    hop["wait"]["refresh_in_progress"] = {
        "started_at": (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        "status": "started",
    }
    state = store.save(path, state)
    hop = _active_hop(state)
    assert hop["wait"]["completion_mode"] == "dom_fallback"
    assert hop["wait"]["refresh_count"] == 1
    assert hop["wait"]["terminal_continuation_unresolved"][
        "refresh_baseline"
    ] == 0
    assert worker_module.parse_time(hop["wait"]["dom_fallback_ready_at"]) < datetime.now(
        timezone.utc
    )
    assert worker_module.parse_time(
        hop["wait"]["terminal_continuation_unresolved"]["block_ready_at"]
    ) < datetime.now(timezone.utc)
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "terminal_continuation_unresolved"
    assert state["block_retryable"] is False
    assert hop["wait"]["last_refresh_result"]["status"] == "interrupted"
    assert calls["refresh"] == 1
    assert calls["send"] == calls["retry"] == calls["restart"] == calls["new_chat"] == 0
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


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


def test_normal_wait_routes_path_only_response_without_repair(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-normal-wait-path-only"
    )
    handoff = ".plan/remote-team/remote-plan_turn1_task-normal-wait-path-only.md"
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
    assert hop["report_sha256"] is None
    assert hop["report_size"] is None
    assert child["kind"] == "handoff"
    assert child["target_role"] == "TEST"
    assert child["handoff"] == handoff
    assert all(item.get("kind") != "route_repair" for item in state["hops"])
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


def test_waiting_requires_valid_route_report_and_two_samples_before_hop_response(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-valid-gate"
    )
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


def test_cross_workspace_file_report_routes_as_opaque_path(tmp_path: Path):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-worker-execution"
    execution_repository.mkdir()
    store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-cross-workspace-file",
        repository=execution_repository,
    )
    original_request_id = hop["request_id"]
    handoff = ".plan/windows-team/windows-plan_turn1_task-cross-workspace-file.md"
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
    assert not (execution_repository / handoff).exists()
    assert not (tmp_path / handoff).exists()
    assert RequestLedger(hop["ledger_path"]).get(original_request_id).attempts == 1

    asyncio.run(worker._waiting(state, hop, Actions(), Path(state["manifest_path"])))
    assert hop["state"] == "responded"
    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["report_path"] == handoff
    assert hop["report_size"] is None
    assert hop["report_sha256"] is None
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


def test_waiting_reconciles_conversation_id_from_exact_ledger_before_backend_wait(tmp_path: Path):
    from dataclasses import replace
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-conversation-reconcile"
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id="conversation-1").to_dict(),
    )
    hop["timestamps"]["sent_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=10)
    ).isoformat()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    state["roles"]["PLAN"]["page_url"] = "https://chatgpt.com/c/conversation-1"
    hop["conversation_url"] = "https://chatgpt.com/c/conversation-1"
    _store.save(path, state)
    seen = []

    acquired = AcquiredRole(
        SimpleNamespace(), receipt.binding.page_id, "https://chatgpt.com/c/conversation-1", False, False
    )
    class Actions:
        async def backend_stream_status(self, conversation_id):
            seen.append(conversation_id)
            return {"status": "IS_STREAMING"}
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("IS_STREAMING must not fetch graph")
        async def locate_owned(self, _state, _role):
            return acquired
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("existing source should be reused")

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert seen == ["conversation-1"]
    assert hop["receipt"]["conversation_id"] == "conversation-1"


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


def test_worker_completion_is_stream_status_primary_with_dom_fallback_preserved():
    import inspect

    waiting_source = inspect.getsource(CDPAWorker._waiting)
    assert "backend_stream_status" in waiting_source
    assert "backend_conversation" in waiting_source
    assert "resolve_terminal_assistant" in waiting_source
    assert "_waiting_dom" in waiting_source
    assert ".wait_for_response(" not in waiting_source

    for name in ("_final_response_reconciliation", "_waiting_dom", "_recover_resume_waiting"):
        method_source = inspect.getsource(getattr(CDPAWorker, name))
        assert ".wait_for_response(" in method_source


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


def test_stream_status_poll_delay_is_randomized_within_10_to_15_seconds(tmp_path: Path):
    _, _, _, worker = setup_task(tmp_path, task_id="task-stream-jitter")
    samples = [worker._stream_status_poll_delay() for _ in range(100)]
    assert all(10.0 <= value <= 15.0 for value in samples)
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


def test_first_allocation_prefers_current_task_donor_before_catalog(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    root_donor = donor_pool_record()["donors"][0]
    record = catalog.upsert(donor_pool_record())
    store = TaskStore(config)
    state = store.create_task(
        "prefer descendant donor",
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
        (child_donor["conversation_id"], child_donor["assistant_message_id"])
    ]
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"


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


def test_source_only_bootstrap_materializes_first_donor_without_fresh_fallback(tmp_path: Path):
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

        async def backend_conversation(self, conversation_id):
            assert conversation_id == record["source_conversation_id"]
            return _bootstrap_graph(assistant_id)

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
    donor = {
        "conversation_id": record["source_conversation_id"],
        "assistant_message_id": assistant_id,
    }
    assert actions.branches == [(donor["conversation_id"], donor["assistant_message_id"])]
    assert catalog.get(record["bootstrap_id"])["donors"] == [donor]
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"


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
        async def acquire_global_role(self, physical_role):
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
    assert state["bootstrap_source_exhausted"] is True
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
    hop = _active_hop(state)
    child_conversation = "33333333-3333-4333-8333-333333333333"
    inherited_assistant = "44444444-4444-4444-8444-444444444444"
    accepted_user = "55555555-5555-4555-8555-555555555555"
    role_response = "66666666-6666-4666-8666-666666666666"
    hop["receipt"] = {
        "conversation_id": child_conversation,
        "user_message_id": accepted_user,
    }
    graph = {
        "current_node": role_response,
        "mapping": {
            inherited_assistant: {
                "id": inherited_assistant,
                "message": {
                    "id": inherited_assistant,
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["bootstrap prefix"]},
                },
                "parent": None,
                "children": [accepted_user],
            },
            accepted_user: {
                "id": accepted_user,
                "message": {
                    "id": accepted_user,
                    "author": {"role": "user"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["PLAN prompt"]},
                },
                "parent": inherited_assistant,
                "children": [role_response],
            },
            role_response: {
                "id": role_response,
                "message": {
                    "id": role_response,
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["PLAN response"]},
                },
                "parent": accepted_user,
                "children": [],
            },
        },
    }
    worker = CDPAWorker(config, store=store)

    class Actions:
        async def backend_conversation(self, conversation_id):
            assert conversation_id == child_conversation
            return graph

    captured = asyncio.run(worker._capture_bootstrap_role_donor(state, hop, Actions()))

    donor = {
        "conversation_id": child_conversation,
        "assistant_message_id": inherited_assistant,
    }
    assert captured is True
    assert state["bootstrap_task_donors"][0] == donor
    assert role["bootstrap_donor"] == donor
    assert catalog.get(record["bootstrap_id"])["donors"][0] == donor
