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
    RoleOwnershipError,
    TeamCloseError,
)
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


def setup_task(tmp_path: Path, *, task_id="task-a", report_mode="file"):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id=task_id,
        report_mode=report_mode,
    )
    return config, store, state, CDPAWorker(config, store=store)


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
    envelope = json.loads(
        hop["prompt"].split("\n\n# PLAN", 1)[0].removeprefix(
            "alpha · role: plan\n"
        )
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


def test_route_repair_send_keeps_transport_identity_out_of_actual_payload(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-repair-payload")
    original = _active_hop(state)
    asyncio.run(worker._pre_send(state, original, FakeActions()))
    original["response"] = "not json"
    original["state"] = "responded"
    worker._responded(state, original)
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    client = RecordingCDPASendClient(
        task_id=state["task_id"],
        team=state["team"],
    )

    asyncio.run(worker._sending(state, repair, RecordingCDPASendActions(client)))

    record = RequestLedger(repair["ledger_path"]).get(repair["request_id"])
    assert client.send_calls == [repair["prompt"]]
    assert repair["receipt"]["prompt"] == repair["prompt"]
    assert record is not None
    assert record.rendered_prompt == repair["prompt"]
    for forbidden in (
        "ROLE_REQUEST_ID",
        repair["request_id"],
        repair["ledger_path"],
        state["manifest_path"],
        repair["expected_report_path"],
        "controller_id",
        "run_id",
        "prompt_sha256",
        "created_at",
        "updated_at",
        "page_id",
        "Implement exact production behavior",
    ):
        assert forbidden not in client.send_calls[0]
        assert forbidden not in repair["receipt"]["prompt"]


def test_valid_report_routes_to_lazy_dev_child_and_records_sha(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    hop["response"] = '{"route":"DEV","handoff":".plan/alpha/alpha-plan_turn1_task-a.md"}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["state"] == "routed"
    assert child["target_role"] == "DEV"
    assert child["state"] == "pre_send"
    assert state["active_role"] == "DEV"
    assert state["roles"]["DEV"]["status"] == "pending"
    assert len(state["reports"]) == 1
    assert len(state["reports"][0]["sha256"]) == 64


def test_cross_repository_task_routes_reports_from_control_plane_plan_root(
    tmp_path: Path,
):
    control_root = tmp_path / "control"
    target_root = tmp_path / "target"
    control_root.mkdir(parents=True)
    target_root.mkdir(parents=True)
    config = load_cdpa_config(
        write_config(control_root), repository_root=control_root
    )
    store = TaskStore(config)
    state = store.create_task(
        "Cross repository task",
        requested_team="alpha",
        task_id="task-cross-repo",
        repository=target_root,
    )
    worker = CDPAWorker(config, store=store)

    plan_hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, plan_hop, FakeActions()))
    plan_report = (
        control_root
        / ".plan"
        / "alpha"
        / "alpha-plan_turn1_task-cross-repo.md"
    )
    plan_report.parent.mkdir(parents=True, exist_ok=True)
    plan_report.write_text("plan evidence", encoding="utf-8")
    plan_hop["response"] = (
        '{"route":"DEV","handoff":'
        '".plan/alpha/alpha-plan_turn1_task-cross-repo.md"}'
    )
    plan_hop["state"] = "responded"

    worker._responded(state, plan_hop)

    dev_hop = _active_hop(state)
    assert plan_hop["state"] == "routed"
    assert plan_hop["report_path"] == str(plan_report.resolve())
    assert dev_hop["target_role"] == "DEV"
    assert state["repository"] == str(target_root.resolve())

    asyncio.run(worker._pre_send(state, dev_hop, FakeActions()))
    dev_report = (
        control_root
        / ".plan"
        / "alpha"
        / "alpha-dev_turn1_task-cross-repo.md"
    )
    dev_report.write_text("dev evidence", encoding="utf-8")
    dev_hop["response"] = (
        '{"route":"TEST","handoff":'
        '".plan/alpha/alpha-dev_turn1_task-cross-repo.md"}'
    )
    dev_hop["state"] = "responded"

    worker._responded(state, dev_hop)

    test_hop = _active_hop(state)
    assert dev_hop["state"] == "routed"
    assert dev_hop["route"] == "TEST"
    assert dev_hop["report_path"] == str(dev_report.resolve())
    assert test_hop["target_role"] == "TEST"
    assert test_hop["handoff"] == ".plan/alpha/alpha-dev_turn1_task-cross-repo.md"


def test_successful_route_clears_stale_block_state(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "stale transport error"
    state["pause_reason"] = "stale pause"
    hop["response"] = '{"route":"DEV","handoff":".plan/alpha/alpha-plan_turn1_task-a.md"}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["status"] == "RUNNING"
    assert state["block_reason"] is None
    assert state["pause_reason"] is None


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


@pytest.mark.parametrize("role", ["PLAN", "DEV", "TEST", "REVIEW"])
def test_identical_missing_file_repair_routes_once_to_plan_without_replay(
    tmp_path: Path,
    role: str,
):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id=f"task-missing-report-{role.lower()}",
    )
    retained_path = ".plan/alpha/retained-report.md"
    state["reports"] = [
        {
            "report_id": 1,
            "role": "PLAN",
            "physical_role": "alpha-plan",
            "turn": 1,
            "path": retained_path,
            "sha256": "a" * 64,
            "size": 17,
            "created_at": utc_now(),
        }
    ]
    reports_before = json.loads(json.dumps(state["reports"]))
    if role == "PLAN":
        hop = _active_hop(state)
    else:
        parent = _active_hop(state)
        parent["state"] = "routed"
        parent["route"] = role
        hop = worker._append_hop(
            state,
            source_role="PLAN",
            target_role=role,
            handoff=retained_path,
        )
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    response = json.dumps(
        {"route": "PLAN", "handoff": hop["expected_report_path"]},
        separators=(",", ":"),
    )
    response_sha256 = worker_module.hashlib.sha256(response.encode("utf-8")).hexdigest()
    hop.update(
        state="responded",
        response=response,
        response_sha256=response_sha256,
        message_identity={"message_id": "assistant-first", "turn_id": "turn-first"},
        receipt={"user_message_id": "user-first", "user_turn_id": "user-turn-first"},
        conversation_url="https://chatgpt.com/c/exact-missing-file",
    )

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    repair.update(
        state="responded",
        response=response,
        response_sha256=response_sha256,
        message_identity={"message_id": "assistant-repair", "turn_id": "turn-repair"},
        receipt={"user_message_id": "user-repair", "user_turn_id": "user-turn-repair"},
        conversation_url="https://chatgpt.com/c/exact-missing-file",
    )
    preserved = {
        key: json.loads(json.dumps(repair.get(key)))
        for key in (
            "request_id",
            "receipt",
            "message_identity",
            "response",
            "response_sha256",
            "conversation_url",
        )
    }

    worker._responded(state, repair)

    continuation = _active_hop(state)
    assert repair["state"] == "abandoned"
    assert continuation["target_role"] == "PLAN"
    assert continuation["handoff"] == retained_path
    assert continuation["request_id"] != repair["request_id"]
    assert state["reports"] == reports_before
    assert {
        item["hop_id"]
        for item in state["hops"]
        if item.get("kind") == "route_repair"
    } == {repair["hop_id"]}
    assert state["route_timeline"][-1]["kind"] == "missing_file_fallback"
    assert not (tmp_path / repair["expected_report_path"]).exists()
    for key, value in preserved.items():
        assert repair.get(key) == value


def test_repeated_plan_missing_file_fallback_blocks_once_and_restart_is_idempotent(
    tmp_path: Path,
):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id="task-missing-report-fallback-ceiling",
    )
    path = Path(state["manifest_path"])
    retained_path = ".plan/alpha/retained-fallback-ceiling.md"
    state["reports"] = [
        {
            "report_id": 1,
            "role": "PLAN",
            "physical_role": "alpha-plan",
            "turn": 1,
            "path": retained_path,
            "sha256": "e" * 64,
            "size": 27,
            "created_at": utc_now(),
        }
    ]
    reports_before = json.loads(json.dumps(state["reports"]))
    conversation = "https://chatgpt.com/c/fallback-ceiling"

    first = _active_hop(state)
    asyncio.run(worker._pre_send(state, first, FakeActions()))
    first_response = json.dumps(
        {"route": "PLAN", "handoff": first["expected_report_path"]},
        separators=(",", ":"),
    )
    first.update(
        state="responded",
        response=first_response,
        response_sha256=worker_module.hashlib.sha256(
            first_response.encode("utf-8")
        ).hexdigest(),
        conversation_url=conversation,
    )
    worker._responded(state, first)
    first_repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, first_repair, FakeActions()))
    first_repair.update(
        state="responded",
        response=first_response,
        response_sha256=first["response_sha256"],
        conversation_url=conversation,
    )
    worker._responded(state, first_repair)

    continuation = _active_hop(state)
    assert continuation["kind"] == "missing_file_fallback"
    asyncio.run(worker._pre_send(state, continuation, FakeActions()))
    second_response = json.dumps(
        {"route": "PLAN", "handoff": continuation["expected_report_path"]},
        separators=(",", ":"),
    )
    continuation.update(
        state="responded",
        response=second_response,
        response_sha256=worker_module.hashlib.sha256(
            second_response.encode("utf-8")
        ).hexdigest(),
        conversation_url=conversation,
    )
    worker._responded(state, continuation)
    second_repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, second_repair, FakeActions()))

    ledger = RequestLedger(second_repair["ledger_path"])
    record = ledger.begin(
        role=second_repair["physical_role"],
        prompt=second_repair["prompt"],
        request_id=second_repair["request_id"],
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=second_repair["prompt"],
        prompt_sha256=prompt_digest(second_repair["prompt"]),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="fallback-ceiling",
        user_message_id="user-fallback-ceiling",
        user_turn_id="user-turn-fallback-ceiling",
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
        accepted_at=2.0,
        receipt=receipt.to_dict(),
    )
    second_repair["receipt"] = receipt.to_dict()
    second_repair["conversation_url"] = conversation
    worker._record_response(
        state,
        second_repair,
        MessageSnapshot(
            "assistant",
            "assistant-fallback-ceiling",
            "assistant-turn-fallback-ceiling",
            second_response,
            (),
        ),
    )
    preserved = {
        key: json.loads(json.dumps(second_repair.get(key)))
        for key in (
            "request_id",
            "receipt",
            "message_identity",
            "response",
            "response_sha256",
            "conversation_url",
        )
    }
    hop_ids_before = {item["hop_id"] for item in state["hops"]}

    worker._responded(state, second_repair)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "report_materialization_unavailable"
    assert state["block_retryable"] is False
    assert state["active_hop_id"] == second_repair["hop_id"]
    assert {item["hop_id"] for item in state["hops"]} == hop_ids_before
    assert sum(
        item.get("kind") == "missing_file_fallback" for item in state["hops"]
    ) == 1
    assert sum(item.get("kind") == "route_repair" for item in state["hops"]) == 2
    assert state["reports"] == reports_before
    for key, value in preserved.items():
        assert second_repair.get(key) == value
    completed = ledger.get(second_repair["request_id"])
    assert completed is not None
    assert completed.status is RequestStatus.COMPLETED
    assert completed.receipt == receipt.to_dict()
    assert completed.response["message_id"] == "assistant-fallback-ceiling"

    persisted = store.save(path, state)
    manifest_before = path.read_bytes()
    restarted = CDPAWorker(worker.config, store=store)
    result = asyncio.run(
        restarted.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=[persisted],
        )
    )

    assert result == persisted
    assert path.read_bytes() == manifest_before
    assert len(result["hops"]) == len(persisted["hops"])
    assert ledger.get(second_repair["request_id"]) == completed


def test_changed_conversation_cannot_reset_missing_file_fallback_ceiling(
    tmp_path: Path,
):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id="task-missing-report-changed-conversation-ceiling",
    )
    path = Path(state["manifest_path"])
    retained_path = ".plan/alpha/retained-changed-conversation.md"
    state["reports"] = [
        {
            "report_id": 1,
            "role": "PLAN",
            "physical_role": "alpha-plan",
            "turn": 1,
            "path": retained_path,
            "sha256": "f" * 64,
            "size": 31,
            "created_at": utc_now(),
        }
    ]
    reports_before = json.loads(json.dumps(state["reports"]))
    conversation_a = "https://chatgpt.com/c/fallback-ancestry-a"
    conversation_b = "https://chatgpt.com/c/fallback-ancestry-b"

    def respond_missing(hop: dict[str, Any], conversation: str, suffix: str) -> str:
        asyncio.run(worker._pre_send(state, hop, FakeActions()))
        response = json.dumps(
            {"route": "PLAN", "handoff": hop["expected_report_path"]},
            separators=(",", ":"),
        )
        hop.update(
            state="responded",
            response=response,
            response_sha256=worker_module.hashlib.sha256(
                response.encode("utf-8")
            ).hexdigest(),
            message_identity={
                "message_id": f"assistant-{suffix}",
                "turn_id": f"assistant-turn-{suffix}",
            },
            receipt={
                "user_message_id": f"user-{suffix}",
                "user_turn_id": f"user-turn-{suffix}",
            },
            conversation_url=conversation,
        )
        worker._responded(state, hop)
        return response

    first = _active_hop(state)
    respond_missing(first, conversation_a, "first")
    first_repair = _active_hop(state)
    respond_missing(first_repair, conversation_a, "first-repair")

    fallback = _active_hop(state)
    assert fallback["kind"] == "missing_file_fallback"
    second_response = respond_missing(fallback, conversation_a, "fallback")

    changed_conversation_repair = _active_hop(state)
    assert changed_conversation_repair["kind"] == "route_repair"
    asyncio.run(worker._pre_send(state, changed_conversation_repair, FakeActions()))
    changed_conversation_repair.update(
        state="responded",
        response=second_response,
        response_sha256=worker_module.hashlib.sha256(
            second_response.encode("utf-8")
        ).hexdigest(),
        conversation_url=conversation_b,
    )
    worker._responded(state, changed_conversation_repair)

    retry = _active_hop(state)
    assert retry["kind"] == "route_repair"
    assert retry["repair_attempt"] == 2
    assert retry["parent_hop_id"] == changed_conversation_repair["hop_id"]
    asyncio.run(worker._pre_send(state, retry, FakeActions()))
    ledger = RequestLedger(retry["ledger_path"])
    record = ledger.begin(
        role=retry["physical_role"],
        prompt=retry["prompt"],
        request_id=retry["request_id"],
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=retry["prompt"],
        prompt_sha256=prompt_digest(retry["prompt"]),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="fallback-ancestry",
        user_message_id="user-fallback-ancestry",
        user_turn_id="user-turn-fallback-ancestry",
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
        accepted_at=3.0,
        receipt=receipt.to_dict(),
    )
    retry["receipt"] = receipt.to_dict()
    retry["conversation_url"] = conversation_b
    worker._record_response(
        state,
        retry,
        MessageSnapshot(
            "assistant",
            "assistant-fallback-ancestry",
            "assistant-turn-fallback-ancestry",
            second_response,
            (),
        ),
    )
    preserved = {
        key: json.loads(json.dumps(retry.get(key)))
        for key in (
            "request_id",
            "receipt",
            "message_identity",
            "response",
            "response_sha256",
            "conversation_url",
        )
    }
    hop_ids_before = {item["hop_id"] for item in state["hops"]}

    worker._responded(state, retry)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "report_materialization_unavailable"
    assert state["block_retryable"] is False
    assert state["active_hop_id"] == retry["hop_id"]
    assert {item["hop_id"] for item in state["hops"]} == hop_ids_before
    assert sum(
        item.get("kind") == "missing_file_fallback" for item in state["hops"]
    ) == 1
    assert sum(item.get("kind") == "route_repair" for item in state["hops"]) == 3
    assert state["reports"] == reports_before
    for key, value in preserved.items():
        assert retry.get(key) == value
    completed = ledger.get(retry["request_id"])
    assert completed is not None
    assert completed.status is RequestStatus.COMPLETED
    assert completed.receipt == receipt.to_dict()
    assert completed.response["message_id"] == "assistant-fallback-ancestry"

    persisted = store.save(path, state)
    manifest_before = path.read_bytes()
    restarted = CDPAWorker(worker.config, store=store)
    result = asyncio.run(
        restarted.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=[persisted],
        )
    )

    assert result == persisted
    assert path.read_bytes() == manifest_before
    assert len(result["hops"]) == len(persisted["hops"])
    assert ledger.get(retry["request_id"]) == completed


def test_missing_file_repair_in_new_conversation_uses_normal_retry(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-missing-report-new-conversation",
    )
    state["reports"] = [
        {
            "report_id": 1,
            "role": "PLAN",
            "physical_role": "alpha-plan",
            "turn": 1,
            "path": ".plan/alpha/retained-new-conversation.md",
            "sha256": "d" * 64,
            "size": 28,
            "created_at": utc_now(),
        }
    ]
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    response = json.dumps(
        {"route": "PLAN", "handoff": hop["expected_report_path"]},
        separators=(",", ":"),
    )
    response_sha256 = worker_module.hashlib.sha256(response.encode("utf-8")).hexdigest()
    hop.update(
        state="responded",
        response=response,
        response_sha256=response_sha256,
        conversation_url="https://chatgpt.com/c/original-conversation",
    )
    worker._responded(state, hop)
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    repair.update(
        state="responded",
        response=response,
        response_sha256=response_sha256,
        conversation_url="https://chatgpt.com/c/new-conversation",
    )

    worker._responded(state, repair)

    next_repair = _active_hop(state)
    assert repair["state"] == "routed"
    assert next_repair["kind"] == "route_repair"
    assert next_repair["repair_attempt"] == 2
    assert not any(
        item.get("kind") == "missing_file_fallback"
        for item in state.get("route_timeline") or []
    )


def test_identical_missing_file_repair_without_retained_report_blocks_once(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-missing-report-initial-plan",
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    response = json.dumps(
        {"route": "PLAN", "handoff": hop["expected_report_path"]},
        separators=(",", ":"),
    )
    response_sha256 = worker_module.hashlib.sha256(response.encode("utf-8")).hexdigest()
    hop.update(state="responded", response=response, response_sha256=response_sha256)
    worker._responded(state, hop)
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    repair.update(state="responded", response=response, response_sha256=response_sha256)

    worker._responded(state, repair)
    hop_ids = {item["hop_id"] for item in state["hops"]}

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "report_materialization_unavailable"
    assert state["block_retryable"] is False
    assert state["active_hop_id"] == repair["hop_id"]
    events = canonical_recovery_events(state)
    assert len(events) == 1
    assert events[0]["failure_signature"].startswith(
        "report_materialization_unavailable:"
    )
    assert state["task_text"] not in [
        str(item.get("handoff") or "") for item in state["hops"] if item is not hop
    ]

    worker._responded(state, repair)

    assert {item["hop_id"] for item in state["hops"]} == hop_ids
    assert state["block_code"] == "report_materialization_unavailable"


def test_missing_file_fallback_preserves_completed_request_ledgers_and_identities(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-missing-report-ledger",
    )
    retained_path = ".plan/alpha/retained-ledger-report.md"
    state["reports"] = [
        {
            "report_id": 1,
            "role": "PLAN",
            "physical_role": "alpha-plan",
            "turn": 1,
            "path": retained_path,
            "sha256": "b" * 64,
            "size": 22,
            "created_at": utc_now(),
        }
    ]
    response = json.dumps(
        {"route": "PLAN", "handoff": hop["expected_report_path"]},
        separators=(",", ":"),
    )
    original_request_id = hop["request_id"]
    original_receipt = json.loads(json.dumps(hop["receipt"]))
    hop["conversation_url"] = "https://chatgpt.com/c/exact-repair-ledger"
    worker._record_response(
        state,
        hop,
        MessageSnapshot(
            "assistant",
            "assistant-original",
            "assistant-turn-original",
            response,
            (),
        ),
    )

    worker._responded(state, hop)

    original_record = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert original_record is not None
    assert original_record.status is RequestStatus.COMPLETED
    assert original_record.receipt == original_receipt
    assert original_record.response["message_id"] == "assistant-original"
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    repair_ledger = RequestLedger(repair["ledger_path"])
    repair_record = repair_ledger.begin(
        role=repair["physical_role"],
        prompt=repair["prompt"],
        request_id=repair["request_id"],
        render_request_marker=False,
    )
    repair_baseline = MessageBaseline(
        frozenset(), frozenset(), frozenset(), frozenset()
    )
    repair_receipt = SendReceipt(
        prompt=repair["prompt"],
        prompt_sha256=prompt_digest(repair["prompt"]),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=repair_baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="repair-session",
        user_message_id="user-repair",
        user_turn_id="user-turn-repair",
    )
    repair_ledger.update(
        repair_record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=repair_receipt.binding,
        baseline=repair_baseline,
    )
    repair_ledger.update(
        repair_record.request_id,
        status=RequestStatus.SENT,
        accepted_at=2.0,
        receipt=repair_receipt.to_dict(),
    )
    repair["receipt"] = repair_receipt.to_dict()
    repair["conversation_url"] = "https://chatgpt.com/c/exact-repair-ledger"
    worker._record_response(
        state,
        repair,
        MessageSnapshot(
            "assistant",
            "assistant-repair",
            "assistant-turn-repair",
            response,
            (),
        ),
    )
    repair_identity = json.loads(json.dumps(repair["message_identity"]))
    repair_conversation = repair["conversation_url"]

    worker._responded(state, repair)

    continuation = _active_hop(state)
    original_after = RequestLedger(hop["ledger_path"]).get(original_request_id)
    repair_after = repair_ledger.get(repair["request_id"])
    assert original_after == original_record
    assert repair_after is not None
    assert repair_after.status is RequestStatus.COMPLETED
    assert repair_after.receipt == repair_receipt.to_dict()
    assert repair_after.response["message_id"] == "assistant-repair"
    assert repair["message_identity"] == repair_identity
    assert repair["conversation_url"] == repair_conversation
    assert repair["state"] == "abandoned"
    assert continuation["handoff"] == retained_path
    assert continuation["request_id"] not in {original_request_id, repair["request_id"]}
    assert RequestLedger(continuation["ledger_path"]).get(
        continuation["request_id"]
    ) is None


@pytest.mark.parametrize(
    "action",
    ["pause", "stop", "clear_team", "restart_role", "new_chat"],
)
def test_pending_operator_lifecycle_control_suppresses_missing_file_fallback(
    tmp_path: Path,
    action: str,
):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id=f"task-missing-report-operator-{action}",
    )
    state["reports"] = [
        {
            "report_id": 1,
            "role": "PLAN",
            "physical_role": "alpha-plan",
            "turn": 1,
            "path": ".plan/alpha/operator-retained.md",
            "sha256": "c" * 64,
            "size": 24,
            "created_at": utc_now(),
        }
    ]
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    response = json.dumps(
        {"route": "PLAN", "handoff": hop["expected_report_path"]},
        separators=(",", ":"),
    )
    response_sha256 = worker_module.hashlib.sha256(response.encode("utf-8")).hexdigest()
    hop.update(state="responded", response=response, response_sha256=response_sha256)
    worker._responded(state, hop)
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    repair.update(state="responded", response=response, response_sha256=response_sha256)
    state["controls"] = [
        {
            "control_id": 1,
            "action": action,
            "role": "PLAN",
            "origin": "operator",
            "status": "requested",
        }
    ]
    hop_ids = {item["hop_id"] for item in state["hops"]}

    worker._responded(state, repair)

    assert {item["hop_id"] for item in state["hops"]} == hop_ids
    assert state["active_hop_id"] == repair["hop_id"]
    assert repair["state"] == "responded"
    assert state["controls"][0]["status"] == "requested"
    assert not any(
        item.get("kind") == "missing_file_fallback"
        for item in state.get("route_timeline") or []
    )


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


def test_block_deduplicates_an_unchanged_active_error(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-block-dedup")
    message = "recorded role tab is offline"

    worker._block(
        state,
        message,
        code="role_offline",
        retryable=False,
    )
    task_error_count = len(state["errors"])
    hop_error_count = len(_active_hop(state)["errors"])

    worker._block(
        state,
        message,
        code="role_offline",
        retryable=False,
    )

    assert len(state["errors"]) == task_error_count
    assert len(_active_hop(state)["errors"]) == hop_error_count


def test_resume_recheck_ownership_failure_reblocks_once(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-resume-reblock")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_code"] = "role_offline"
    state["block_retryable"] = False
    state["block_reason"] = "recorded 'alpha-plan' tab is offline"
    state["errors"] = [
        {"at": "2026-07-22T00:00:00+00:00", "error": state["block_reason"]}
    ]
    hop["errors"] = [state["block_reason"]]
    state["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
            "requested_at": "2026-07-22T00:00:01+00:00",
            "applied_at": None,
            "result": None,
        }
    ]
    store.save(path, state)

    class OfflineRecordedRoleActions(FakeActions):
        async def acquire(self, _state, _role):
            raise RoleOwnershipError(
                "recorded 'alpha-plan' tab is offline; use Open tab for controlled recovery"
            )

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: OfflineRecordedRoleActions(),
    )

    first = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    control = first["controls"][0]
    assert first["status"] == "BLOCKED"
    assert control["status"] == "recovery_required"
    assert control["command_state"] == "RECOVERY_REQUIRED"
    assert control["applied_at"] is not None
    assert control["result"]["outcome"] == "recovery_required"
    assert control["result"]["reason_code"] == "role_offline"
    assert control["result"]["postcondition"] is None
    assert control["result"]["next_safe_action"]
    assert _active_hop(first)["state"] == "pre_send"
    assert _active_hop(first).get("receipt") is None

    task_error_count = len(first["errors"])
    hop_error_count = len(_active_hop(first)["errors"])
    persisted_once = path.read_bytes()
    for _ in range(3):
        later = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
        assert later["controls"][0]["status"] == "recovery_required"
        assert len(later["errors"]) == task_error_count
        assert len(_active_hop(later)["errors"]) == hop_error_count
        assert path.read_bytes() == persisted_once


def test_terminal_idle_cleanup_preflight_failure_preserves_terminal_result(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-auto-cleanup-preflight")
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["kanban_column"] = "DONE_STOPPED"
    state["completed_at"] = "2026-07-20T00:00:00+00:00"
    state["last_role_activity_at"] = "2026-07-20T00:00:00+00:00"
    state["active_role"] = None
    state["active_hop_id"] = None
    state = store.save(path, state)

    class DuplicateActions(FakeActions):
        async def preflight_team(self, _state):
            self.preflight_calls += 1
            raise RuntimeError("duplicate exact role tabs")

    actions = DuplicateActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(
        worker.advance(
            path, SimpleNamespace(pages=[]), scheduling_tasks=[state]
        )
    )

    assert result["status"] == "DONE"
    assert result["terminal_state"] == "DONE"
    assert result["completed_at"] == "2026-07-20T00:00:00+00:00"
    assert result["cleanup"]["state"] == "ACTIVE"
    assert "duplicate exact role tabs" in result["cleanup"]["last_error"]
    assert result["block_code"] is None


def test_automatic_cleanup_preflight_failure_preserves_concurrent_control_atomically(
    tmp_path: Path,
    monkeypatch,
):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id="task-auto-cleanup-preflight-race",
    )
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["kanban_column"] = "DONE_STOPPED"
    state["completed_at"] = "2026-07-20T00:00:00+00:00"
    state["last_role_activity_at"] = "2026-07-20T00:00:00+00:00"
    state["active_role"] = None
    state["active_hop_id"] = None
    state = store.save(path, state)
    reports_before = list(state["reports"])

    class DuplicateActions(FakeActions):
        async def preflight_team(self, current):
            self.preflight_calls += 1
            store.request_control(
                current["manifest_path"],
                "clear_team",
                confirmed=True,
                reason="concurrent dashboard clear",
            )
            raise RuntimeError("duplicate exact role tabs")

    actions = DuplicateActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        worker.advance(
            path, SimpleNamespace(pages=[]), scheduling_tasks=[state]
        )
    )

    persisted = store.load(path)
    assert result == persisted
    assert persisted["status"] == "DONE"
    assert persisted["terminal_state"] == "DONE"
    assert persisted["completed_at"] == "2026-07-20T00:00:00+00:00"
    assert persisted["reports"] == reports_before
    assert persisted["cleanup"]["state"] == "ACTIVE"
    assert persisted["cleanup"]["retry_count"] == 1
    assert "duplicate exact role tabs" in persisted["cleanup"]["last_error"]
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "requested", "concurrent dashboard clear"),
    ]
    assert actions.preflight_calls == 1
    assert actions.stop_calls == 0
    assert actions.closed_teams == 0


def test_clear_team_preserves_done_terminal_state(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-clear-done")
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["kanban_column"] = "DONE_STOPPED"
    state["completed_at"] = "2026-07-22T00:00:00+00:00"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": False,
        "status": "requested",
    }]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    state = store.load(path)
    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert state["completed_at"] == "2026-07-22T00:00:00+00:00"
    assert state.get("stopped_at") is None
    assert state["cleanup"]["state"] == "CLEARED"
    assert actions.preflight_calls == 2
    assert actions.closed_teams == 1


def test_clear_team_preflight_failure_happens_before_stop_or_close(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-clear-preflight")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]
    state = store.save(path, state)

    class DuplicateActions(FakeActions):
        async def preflight_team(self, _state):
            self.preflight_calls += 1
            raise RuntimeError("duplicate exact role tabs")

    actions = DuplicateActions()
    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    assert state["controls"][0]["status"] == "rejected"
    assert actions.stop_calls == 0
    assert actions.closed_teams == 0
    assert state["cleanup"]["state"] == "ACTIVE"
    assert state["status"] == "INBOX"
    assert state["active_hop_id"] == 1


def test_clear_team_preflight_failure_preserves_concurrent_control_atomically(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)

    class DuplicateActions(FakeActions):
        async def preflight_team(self, state):
            self.preflight_calls += 1
            store.request_control(
                state["manifest_path"],
                "pause",
                reason="concurrent dashboard pause",
            )
            raise RuntimeError("duplicate exact role tabs")

    actions = DuplicateActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        worker.advance(
            owner["manifest_path"],
            SimpleNamespace(pages=[]),
            scheduling_tasks=[owner],
        )
    )

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["status"] == "INBOX"
    assert persisted["active_hop_id"] == 1
    assert persisted["cleanup"]["state"] == "ACTIVE"
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "rejected", None),
        (2, "pause", "requested", "concurrent dashboard pause"),
    ]
    assert "duplicate exact role tabs" in persisted["controls"][0]["result"]
    assert actions.preflight_calls == 1
    assert actions.stop_calls == 0
    assert actions.closed_teams == 0


def test_clear_team_stop_failure_persists_stopped_recoverable_state_and_resumes(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-clear-stop-failure")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]

    class StopFailureActions(FakeActions):
        async def stop_if_active(self, _acquired):
            self.stop_calls += 1
            raise RuntimeError("synthetic stop failure")

    failing = StopFailureActions()
    assert asyncio.run(worker._apply_control(state, failing, path)) is True
    persisted = store.load(path)
    assert persisted["status"] == "STOPPED"
    assert persisted["active_role"] is None
    assert persisted["active_hop_id"] is None
    assert persisted["hops"][0]["state"] == "abandoned"
    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "stop_pending"
    assert "synthetic stop failure" in persisted["cleanup"]["last_error"]
    assert persisted["controls"][0]["status"] == "cleanup_pending"

    resumed = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: resumed)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "STOPPED"
    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["retry_count"] == 1
    assert result["controls"][0]["status"] == "applied"
    assert resumed.stop_calls == 1
    assert resumed.closed_teams == 1
    assert result["block_code"] is None


def test_cleanup_stop_failure_preserves_concurrent_control_atomically(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-stop-race")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)

    class StopFailureActions(FakeActions):
        async def stop_if_active(self, _acquired):
            self.stop_calls += 1
            store.request_control(
                owner["manifest_path"],
                "pause",
                reason="concurrent dashboard pause",
            )
            raise RuntimeError("stop inspection failed")

    actions = StopFailureActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(owner["manifest_path"], SimpleNamespace(pages=[])))

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["status"] == "STOPPED"
    assert persisted["active_hop_id"] is None
    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "stop_pending"
    assert persisted["cleanup"]["retry_count"] == 1
    assert "stop inspection failed" in persisted["cleanup"]["last_error"]
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "cleanup_pending", None),
        (2, "pause", "requested", "concurrent dashboard pause"),
    ]
    assert actions.stop_calls == 1
    assert actions.closed_teams == 0


def test_cleanup_close_success_preserves_concurrent_control_atomically(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-close-race")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)

    class ConcurrentCloseActions(FakeActions):
        async def close_team(self, state, *, preflighted_pages=None):
            store.request_control(
                state["manifest_path"],
                "pause",
                reason="concurrent dashboard pause",
            )
            return await super().close_team(state, preflighted_pages=preflighted_pages)

    actions = ConcurrentCloseActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(owner["manifest_path"], SimpleNamespace(pages=[])))

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["cleanup"]["state"] == "CLEARED"
    assert persisted["cleanup"]["closed_tabs"] == 1
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "applied", None),
        (2, "pause", "requested", "concurrent dashboard pause"),
    ]
    assert actions.closed_teams == 1
    assert actions.pages == []


def test_cleanup_verification_preserves_concurrent_control_atomically(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-verify-race")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)

    class ConcurrentVerifyActions(FakeActions):
        async def preflight_team(self, state):
            self.preflight_calls += 1
            if self.closed_teams:
                store.request_control(
                    state["manifest_path"],
                    "pause",
                    reason="concurrent dashboard pause",
                )
            return list(self.pages)

    actions = ConcurrentVerifyActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(owner["manifest_path"], SimpleNamespace(pages=[])))

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["cleanup"]["state"] == "CLEARED"
    assert persisted["cleanup"]["closed_tabs"] == 1
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "applied", None),
        (2, "pause", "requested", "concurrent dashboard pause"),
    ]
    assert actions.preflight_calls == 2
    assert actions.closed_teams == 1


def test_clear_team_partial_close_failure_resumes_remaining_tabs(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-clear-close-failure")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]

    class PartialCloseActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.pages = ["page-one", "page-two"]
            self.fail_once = True

        async def preflight_team(self, _state):
            self.preflight_calls += 1
            return list(self.pages)

        async def close_team(self, _state, *, preflighted_pages=None):
            self.closed_teams += 1
            selected = list(preflighted_pages or self.pages)
            if self.fail_once:
                self.fail_once = False
                self.pages.remove(selected[0])
                raise TeamCloseError(
                    "synthetic second-tab close failure",
                    closed_tabs=1,
                )
            closed = len(selected)
            self.pages.clear()
            return closed

    actions = PartialCloseActions()
    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    persisted = store.load(path)
    assert persisted["status"] == "STOPPED"
    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "close_pending"
    assert persisted["cleanup"]["target_tabs"] == 2
    assert "second-tab close failure" in persisted["cleanup"]["last_error"]

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["closed_tabs"] == 2
    assert result["cleanup"]["retry_count"] == 1
    assert actions.pages == []
    assert result["active_hop_id"] is None


def test_resumed_cleanup_preflight_inspection_failure_remains_clearing(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleanup-inspection-unknown")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    state["status"] = "STOPPED"
    state["terminal_state"] = "STOPPED"
    state["kanban_column"] = "DONE_STOPPED"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": "close_pending",
        "status_before": "RUNNING",
        "active_role": "PLAN",
        "active_hop_id": 1,
        "target_tabs": 1,
        "closed_tabs": 0,
        "retry_count": 0,
    })
    store.save(path, state)

    class UnreadableActions(FakeActions):
        async def preflight_team(self, _state):
            self.preflight_calls += 1
            raise RuntimeError("transient cleanup inspection failure")

    actions = UnreadableActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARING"
    assert result["cleanup"]["phase"] == "close_pending"
    assert result["cleanup"]["closed_tabs"] == 0
    assert result["cleanup"]["retry_count"] == 1
    assert "inspection failure" in result["cleanup"]["last_error"]
    assert result["active_action"] == "cleanup_recoverable"
    assert actions.closed_teams == 0


def test_zero_remaining_tabs_with_historical_target_does_not_infer_closures(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleanup-no-inference")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    state["status"] = "STOPPED"
    state["terminal_state"] = "STOPPED"
    state["kanban_column"] = "DONE_STOPPED"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": "close_pending",
        "status_before": "RUNNING",
        "active_role": "PLAN",
        "active_hop_id": 1,
        "target_tabs": 2,
        "closed_tabs": 0,
        "retry_count": 0,
    })
    store.save(path, state)

    actions = FakeActions()
    actions.pages.clear()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["target_tabs"] == 2
    assert result["cleanup"]["closed_tabs"] == 0
    assert actions.closed_teams == 1
    assert actions.preflight_calls == 2


def test_post_close_verification_failure_preserves_evidence_and_resumes(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleanup-verify-failure")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]

    class VerifyFailureActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.fail_verify = True

        async def preflight_team(self, _state):
            self.preflight_calls += 1
            if self.fail_verify and self.closed_teams:
                self.fail_verify = False
                raise RuntimeError("post-close verification unavailable")
            return list(self.pages)

    actions = VerifyFailureActions()
    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    persisted = store.load(path)

    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "verify_pending"
    assert persisted["cleanup"]["closed_tabs"] == 1
    assert persisted["cleanup"]["retry_count"] == 1
    assert "verification unavailable" in persisted["cleanup"]["last_error"]
    assert persisted["controls"][0]["status"] == "cleanup_pending"

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["closed_tabs"] == 1
    assert result["cleanup"]["verified_empty_at"]
    assert result["cleanup"]["retry_count"] == 1
    assert result["controls"][0]["status"] == "applied"
    assert actions.closed_teams == 1


def test_legacy_half_cleared_running_manifest_is_normalized_before_cleanup_resume(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-legacy-clearing")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    state["status"] = "RUNNING"
    state["active_role"] = "PLAN"
    state["active_hop_id"] = 1
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": None,
        "status_before": None,
        "active_role": None,
        "active_hop_id": None,
    })
    store.save(path, state)

    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "STOPPED"
    assert result["active_role"] is None
    assert result["active_hop_id"] is None
    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["status_before"] == "RUNNING"
    assert result["cleanup"]["active_role"] == "PLAN"
    assert result["cleanup"]["active_hop_id"] == 1
    assert result["block_code"] is None


@pytest.mark.parametrize("phase", ["stop_pending", "close_pending", "closing", "verify_pending"])
def test_worker_restart_resumes_every_persisted_cleanup_phase(
    tmp_path: Path, monkeypatch, phase: str
):
    _, store, state, worker = setup_task(tmp_path, task_id=f"task-cleanup-{phase}")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    hop["abandon_reason"] = "cleanup restart fixture"
    state["status"] = "STOPPED"
    state["terminal_state"] = "STOPPED"
    state["kanban_column"] = "DONE_STOPPED"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": phase,
        "status_before": "RUNNING",
        "active_role": "PLAN",
        "active_hop_id": 1,
        "target_tabs": 1,
        "closed_tabs": 0,
        "retry_count": 0,
    })
    state = store.save(path, state)

    actions = FakeActions()
    if phase == "verify_pending":
        actions.pages.clear()
        state["cleanup"]["closed_tabs"] = 1
        state = store.save(path, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["status"] == "STOPPED"
    assert result["active_hop_id"] is None
    assert result["block_code"] is None
    assert actions.stop_calls == (1 if phase == "stop_pending" else 0)
    assert actions.closed_teams == (0 if phase == "verify_pending" else 1)


def test_waiting_upgrades_legacy_collapsed_receipt_in_hop_and_ledger(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-legacy-upgrade")
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
        accepted_via="stop_button",
        session_id_before=None,
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
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = datetime.now(timezone.utc).isoformat()
    worker._start_wait_budget_from_sent(hop)
    state["roles"]["PLAN"]["page_id"] = "page-alpha-plan"
    store.save(path, state)
    collapsed = MessageSnapshot(
        "user", "u-collapsed", "t-collapsed", prompt[:2000] + " Show more", ()
    )
    snapshot = SimpleNamespace(
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        messages=(collapsed,),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, receipt_value, **_kwargs):
            assert receipt_value.user_message_id == "u-collapsed"
            raise TimeoutError("continue")

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert hop["receipt"]["accepted_via"] == "user_message_identity"
    assert hop["receipt"]["user_message_id"] == "u-collapsed"
    assert hop["receipt"]["user_turn_id"] == "t-collapsed"
    persisted = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert persisted is not None
    assert persisted.receipt["user_message_id"] == "u-collapsed"
    assert persisted.receipt["user_turn_id"] == "t-collapsed"


def test_later_handoff_prompt_preserves_original_goal_and_source_role(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-envelope")
    plan_hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, plan_hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-envelope.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    plan_hop["response"] = json.dumps(
        {"route": "DEV", "handoff": ".plan/alpha/alpha-plan_turn1_task-envelope.md"}
    )
    plan_hop["state"] = "responded"
    worker._responded(state, plan_hop)
    dev_hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, dev_hop, FakeActions()))
    envelope = json.loads(
        dev_hop["prompt"].split("\n\n# DEV", 1)[0].removeprefix(
            "alpha · role: dev\n"
        )
    )
    assert envelope["role"] == "alpha-dev"
    assert envelope["source-role"] == "alpha-plan"
    assert envelope["handoff"] == ".plan/alpha/alpha-plan_turn1_task-envelope.md"
    assert envelope["goal"] == "Implement exact production behavior"
    assert dev_hop["expected_report_path"] == ".plan/alpha/alpha-dev_turn1_task-envelope.md"
    assert dev_hop["expected_report_path"] not in dev_hop["prompt"]
    for forbidden in (
        dev_hop["request_id"], dev_hop["ledger_path"], "prompt_sha256",
        "controller_id", "run_id", "page_id", "created_at",
    ):
        assert forbidden not in dev_hop["prompt"]


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

        async def reopen(self, _state, _role, *, require_clean_ready=True):
            assert require_clean_ready is True
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


def test_open_tab_recovers_waiting_exact_tab_with_drifted_accepted_page_id(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-waiting-page-drift"
    )
    hop = _active_hop(state)
    conversation_url = "https://chatgpt.com/c/owned-plan-drift"
    original_request_id = hop["request_id"]
    receipt = {
        "prompt": "accepted prompt",
        "prompt_sha256": "digest",
        "binding": {"page_id": "accepted-page", "role": "alpha-plan"},
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
        page_id="accepted-page",
        page_url=conversation_url,
        online=False,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "restore accepted page identity",
            "status": "requested",
        }
    ]

    drifted = AcquiredRole(
        client=SimpleNamespace(),
        page_id="drifted-live-page",
        url=conversation_url,
        created=False,
        new_chat=False,
    )
    restored = AcquiredRole(
        client=SimpleNamespace(),
        page_id="accepted-page",
        url=conversation_url,
        created=False,
        new_chat=False,
    )

    class DriftRecovery:
        def __init__(self):
            self.reopen_clean = []
            self.open_calls = 0

        async def locate_owned(self, *_args, **_kwargs):
            return drifted

        async def reopen(self, *_args, require_clean_ready=True, **_kwargs):
            self.reopen_clean.append(require_clean_ready)
            return restored

        async def open_tab(self, *_args, **_kwargs):
            self.open_calls += 1

    actions = DriftRecovery()

    assert asyncio.run(worker._apply_control(state, actions)) is True
    assert actions.reopen_clean == [False]
    assert actions.open_calls == 0
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["roles"]["PLAN"]["page_id"] == "accepted-page"
    assert state["roles"]["PLAN"]["page_url"] == conversation_url
    assert hop["state"] == "waiting"
    assert hop["request_id"] == original_request_id
    assert hop["receipt"] == receipt
    assert state["controls"][0]["status"] == "applied"
    assert state["controls"][0]["result"] == {
        "page_id": "accepted-page",
        "recovered": True,
        "resumed": True,
        "hop_id": hop["hop_id"],
        "request_id": original_request_id,
    }


@pytest.mark.parametrize("hop_state", ["sending", "sent", "waiting"])
def test_open_tab_rejects_inflight_role_offline_without_browser_mutation(
    tmp_path: Path,
    hop_state: str,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id=f"task-open-tab-{hop_state}"
    )
    hop = _active_hop(state)
    hop["state"] = hop_state
    if hop_state in {"sent", "waiting"}:
        hop["receipt"] = {
            "request_id": hop["request_id"],
            "binding": {"page_id": "closed-page"},
        }
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    state["roles"]["PLAN"].update(page_id="closed-page", online=False)
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact owned PLAN tab",
            "status": "requested",
        }
    ]

    class NoBrowserMutation:
        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("in-flight recovery must reject before browser mutation")

    assert asyncio.run(worker._apply_control(state, NoBrowserMutation())) is True
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "role_offline"
    assert _active_hop(state)["state"] == hop_state
    assert state["roles"]["PLAN"]["page_id"] == "closed-page"
    assert state["controls"][0]["status"] == "rejected"
    assert "pre_send" in state["controls"][0]["result"]


def test_blocked_unsent_restart_abandons_old_hop_and_creates_new_turn(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-restart")
    hop = _active_hop(state)
    old_request_id = hop["request_id"]
    original_handoff = hop["handoff"]
    state["roles"]["PLAN"]["turn"] = 1
    state["roles"]["PLAN"]["page_id"] = "closed-page"
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "owned PLAN tab is offline"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "restart_role",
            "role": "PLAN",
            "reason": "controlled recovery",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True

    old_hop = state["hops"][0]
    new_hop = _active_hop(state)
    assert old_hop["state"] == "abandoned"
    assert old_hop["request_id"] == old_request_id
    assert old_hop["receipt"] is None
    assert old_hop["abandon_reason"] == "controlled recovery"
    assert old_hop["timestamps"]["abandoned_at"]
    assert new_hop["hop_id"] == 2
    assert new_hop["parent_hop_id"] == 1
    assert new_hop["state"] == "pre_send"
    assert new_hop["kind"] == "role_restart"
    assert new_hop["turn"] == 2
    assert new_hop["request_id"] == "task-restart-hop2"
    assert new_hop["handoff"] == original_handoff
    assert state["status"] == "RUNNING"
    assert state["block_reason"] is None
    assert state["roles"]["PLAN"]["page_id"] == "restarted-alpha-plan"
    assert state["roles"]["PLAN"]["constructor_sent_generation"] is None
    assert state["controls"][0]["status"] == "applied"
    assert state["controls"][0]["result"] == {
        "old_hop_id": 1,
        "new_hop_id": 2,
        "old_page_id": "closed-page",
        "page_id": "restarted-alpha-plan",
        "new_chat": True,
    }
    assert state["route_timeline"][-1]["kind"] == "role_restart"
    assert state["route_timeline"][-1]["new_hop_id"] == 2


@pytest.mark.parametrize("hop_state", ["sending", "sent", "waiting"])
def test_blocked_inflight_restart_is_rejected_without_tab_mutation(
    tmp_path: Path,
    hop_state: str,
):
    _, _, state, worker = setup_task(tmp_path, task_id=f"task-{hop_state}")
    hop = _active_hop(state)
    hop["state"] = hop_state
    if hop_state in {"sent", "waiting"}:
        hop["receipt"] = {"request_id": hop["request_id"], "attempts": 1}
    state["roles"]["PLAN"]["page_id"] = "closed-page"
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "owned PLAN tab is offline"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "restart_role",
            "role": "PLAN",
            "status": "requested",
        }
    ]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert state["controls"][0]["status"] == "rejected"
    assert "in-flight" in state["controls"][0]["result"]
    assert actions.restart_roles == []
    assert state["status"] == "BLOCKED"
    assert state["active_hop_id"] == 1
    assert _active_hop(state)["state"] == hop_state


def test_blocked_restart_validates_selected_role_before_tab_mutation(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-wrong-role")
    hop = _active_hop(state)
    hop["state"] = "waiting"
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "owned PLAN tab is offline"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "restart_role",
            "role": "DEV",
            "status": "requested",
        }
    ]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert state["controls"][0]["status"] == "rejected"
    assert actions.restart_roles == []
    assert state["roles"]["DEV"]["page_id"] is None
    assert _active_hop(state)["state"] == "waiting"


def test_stop_targets_active_role_even_if_control_selects_another_role(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-stop-active")
    state["controls"] = [
        {
            "control_id": 1,
            "action": "stop",
            "role": "DEV",
            "status": "requested",
        }
    ]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert actions.located_roles == ["PLAN"]
    assert state["status"] == "STOPPED"
    assert state["controls"][0]["status"] == "applied"


def test_stop_control_preserves_later_clear_team_atomically(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-stop-control-race")
    store.request_control(
        owner["manifest_path"],
        "stop",
        reason="manual stop",
    )
    worker = CDPAWorker(config, store=store)

    class ConcurrentStopActions(FakeActions):
        async def stop_if_active(self, acquired):
            self.stop_calls += 1
            store.request_control(
                owner["manifest_path"],
                "clear_team",
                confirmed=True,
                reason="concurrent dashboard clear after stop",
            )
            return True

    actions = ConcurrentStopActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(owner["manifest_path"], SimpleNamespace(pages=[])))

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["status"] == "STOPPED"
    assert persisted["stop_reason"] == "manual stop"
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "stop", "applied", "manual stop"),
        (2, "clear_team", "requested", "concurrent dashboard clear after stop"),
    ]
    assert persisted["controls"][0]["result"] == {"stopped_response": True}
    assert actions.stop_calls == 1
    assert actions.closed_teams == 0

    cleared = asyncio.run(
        worker.advance(owner["manifest_path"], SimpleNamespace(pages=[]))
    )
    assert cleared == store.load(owner["manifest_path"])
    assert cleared["cleanup"]["state"] == "CLEARED"
    assert cleared["controls"][1]["status"] == "applied"
    assert actions.closed_teams == 1


def test_new_chat_control_preserves_later_pause_atomically(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-new-chat-control-race")
    store.request_control(owner["manifest_path"], "new_chat", role="PLAN")
    worker = CDPAWorker(config, store=store)

    class ConcurrentNewChatActions(FakeActions):
        async def new_chat(self, state, role):
            store.request_control(
                owner["manifest_path"],
                "pause",
                reason="concurrent dashboard pause",
            )
            return await super().new_chat(state, role)

    actions = ConcurrentNewChatActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(owner["manifest_path"], SimpleNamespace(pages=[])))

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["roles"]["PLAN"]["page_id"] == "new-chat-alpha-plan"
    assert persisted["roles"]["PLAN"]["conversation_generation"] == 1
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "new_chat", "applied", None),
        (2, "pause", "requested", "concurrent dashboard pause"),
    ]


def test_route_plan_abandons_superseded_unsent_hop(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-route-plan")
    plan_hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, plan_hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-route-plan.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    plan_hop["response"] = (
        '{"route":"DEV","handoff":".plan/alpha/alpha-plan_turn1_task-route-plan.md"}'
    )
    plan_hop["state"] = "responded"
    worker._responded(state, plan_hop)
    dev_hop = _active_hop(state)
    state["controls"] = [
        {
            "control_id": 1,
            "action": "route_plan",
            "role": "DEV",
            "reason": "manual reassignment",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True

    new_hop = _active_hop(state)
    assert dev_hop["state"] == "abandoned"
    assert dev_hop["abandon_reason"] == "manual reassignment"
    assert new_hop["target_role"] == "PLAN"
    assert new_hop["parent_hop_id"] == dev_hop["hop_id"]
    assert state["route_timeline"][-1]["kind"] == "control"
    assert state["route_timeline"][-1]["new_hop_id"] == new_hop["hop_id"]


def test_new_chat_remains_rejected_at_inflight_boundary(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-new-chat")
    hop = _active_hop(state)
    hop["state"] = "waiting"
    state["status"] = "RUNNING"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "new_chat",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True

    assert state["controls"][0]["status"] == "rejected"
    assert "in-flight" in state["controls"][0]["result"]
    assert _active_hop(state)["state"] == "waiting"


def test_terminal_task_without_due_cleanup_is_not_rewritten(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-terminal-idle")
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["completed_at"] = state["created_at"]
    state["last_role_activity_at"] = state["created_at"]
    state = store.save(path, state)
    before = path.read_bytes()

    result = asyncio.run(
        worker.advance(
            path, SimpleNamespace(pages=[]), scheduling_tasks=[state]
        )
    )

    assert result["status"] == "DONE"
    assert path.read_bytes() == before


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


def test_accepted_send_initializes_wait_budget_at_sent_timestamp(
    tmp_path: Path,
    monkeypatch,
):
    _, _, state, worker = setup_task(tmp_path, task_id="task-accepted")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    accepted_at = datetime.now(timezone.utc) - timedelta(seconds=3)

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/exact",
                url="https://chatgpt.com/c/exact",
            )

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-PLAN",
        url="https://chatgpt.com/",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    class FakeDurableSendBlock:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _context):
            return {
                "receipt": {"prompt_sha256": hop["prompt_sha256"]},
                "record": {"status": "sent", "accepted_at": accepted_at.timestamp()},
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeDurableSendBlock)

    asyncio.run(worker._sending(state, hop, Actions()))

    assert datetime.fromisoformat(hop["timestamps"]["sent_at"]) == accepted_at
    assert datetime.fromisoformat(hop["wait"]["started_at"]) == accepted_at
    assert datetime.fromisoformat(hop["wait"]["deadline_at"]) == accepted_at + timedelta(
        seconds=worker.config.response_timeout_seconds
    )


def test_recovered_sending_uses_original_sending_anchor_when_acceptance_time_missing(
    tmp_path: Path,
    monkeypatch,
):
    _, _, state, worker = setup_task(tmp_path, task_id="task-recovered-sending")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    sending_at = datetime.now(timezone.utc) - timedelta(hours=3)
    hop["timestamps"]["sending_at"] = sending_at.isoformat()

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/recovered",
                url="https://chatgpt.com/c/recovered",
            )

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-PLAN",
        url="https://chatgpt.com/",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    class FakeDurableSendBlock:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _context):
            return {
                "cached": True,
                "receipt": {"prompt_sha256": hop["prompt_sha256"]},
                "record": {"status": "sent", "accepted_at": None},
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeDurableSendBlock)

    asyncio.run(worker._sending(state, hop, Actions()))

    assert datetime.fromisoformat(hop["timestamps"]["sent_at"]) == sending_at
    assert datetime.fromisoformat(hop["wait"]["started_at"]) == sending_at
    assert datetime.fromisoformat(hop["wait"]["deadline_at"]) == sending_at + timedelta(
        seconds=worker.config.response_timeout_seconds
    )


def test_restart_boundaries_resume_without_restarting_prior_hops(tmp_path: Path):
    _, store, sent, worker = setup_task(tmp_path, task_id="task-sent")
    sent_path = Path(sent["manifest_path"])
    sent_hop = _active_hop(sent)
    request_id = sent_hop["request_id"]
    accepted_at = datetime.now(timezone.utc) - timedelta(hours=3)
    expected_deadline = accepted_at + timedelta(
        seconds=worker.config.response_timeout_seconds
    )
    sent_hop["state"] = "sent"
    sent_hop["receipt"] = {"request_id": request_id}
    sent_hop["timestamps"]["sent_at"] = accepted_at.isoformat()
    store.save(sent_path, sent)

    resumed_sent = asyncio.run(worker.advance(sent_path, SimpleNamespace(pages=[])))

    resumed_hop = _active_hop(resumed_sent)
    assert resumed_hop["state"] == "waiting"
    assert resumed_hop["request_id"] == request_id
    assert datetime.fromisoformat(resumed_hop["wait"]["started_at"]) == accepted_at
    assert datetime.fromisoformat(resumed_hop["wait"]["deadline_at"]) == expected_deadline

    _, store, responded, worker = setup_task(tmp_path, task_id="task-responded")
    responded_path = Path(responded["manifest_path"])
    responded_hop = _active_hop(responded)
    report_relative = (
        f".plan/{responded['team']}/"
        f"{responded['roles']['PLAN']['physical_role']}_turn1_task-responded.md"
    )
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("restart boundary evidence", encoding="utf-8")
    responded_hop["state"] = "responded"
    responded_hop["response"] = json.dumps(
        {"route": "DONE", "handoff": report_relative}
    )
    store.save(responded_path, responded)

    resumed_response = asyncio.run(
        worker.advance(responded_path, SimpleNamespace(pages=[]))
    )

    assert resumed_response["status"] == "DONE"
    assert resumed_response["active_hop_id"] is None
    assert len(resumed_response["reports"]) == 1


def test_inflight_offline_role_blocks_without_opening_or_resending(tmp_path: Path):
    for suffix, hop_state in enumerate(("sending", "waiting"), start=1):
        _, store, state, worker = setup_task(
            tmp_path, task_id=f"task-offline-{suffix}"
        )
        path = Path(state["manifest_path"])
        hop = _active_hop(state)
        hop["state"] = hop_state
        if hop_state == "waiting":
            hop["receipt"] = {"request_id": hop["request_id"]}
        store.save(path, state)

        result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

        assert result["status"] == "BLOCKED"
        assert "offline" in result["block_reason"]
        assert _active_hop(result)["state"] == hop_state


def test_corrupt_manifest_is_excluded_from_worker_loop_and_remains_diagnostic(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path)
    Path(state["manifest_path"]).write_text("{broken", encoding="utf-8")

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    tasks, errors = store.discover_with_errors()

    assert results == []
    assert tasks == []
    assert len(errors) == 1
    assert errors[0]["manifest_path"] == state["manifest_path"]
    assert errors[0]["error"].startswith("InvalidManifestError:")


def test_routed_parent_without_child_fails_closed_after_restart(tmp_path: Path):
    config, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "routed"
    store.save(path, state)

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "BLOCKED"
    assert "no durable child" in result["block_reason"]


def _prepare_sent_waiting_task(
    tmp_path: Path,
    *,
    task_id: str,
    report_mode: str = "file",
):
    _, store, state, worker = setup_task(
        tmp_path, task_id=task_id, report_mode=report_mode
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


def test_unchanged_wait_does_not_touch_manifest_or_republish_projection(
    tmp_path: Path,
    monkeypatch,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-unchanged-wait",
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.RESPONDING,
        stop_visible=True,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            MessageSnapshot("assistant", "a1", "ta1", "still working", ()),
        ),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    wait = hop["wait"]
    anchor = str(wait["started_at"])
    wait.update(
        {
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": anchor,
            "activity_observed_at": anchor,
            "continuous_responding_since": anchor,
            "transport_ui_active": False,
            "last_stop_visible": True,
        }
    )
    role = state["roles"]["PLAN"]
    role.update(
        {
            "status": "active",
            "page_id": "page-alpha-plan",
            "page_url": "https://chatgpt.com/c/exact",
            "online": True,
            "last_error": None,
        }
    )
    state["last_role_activity_at"] = role["last_activity_at"]
    state["active_action"] = "wait_response"
    state = store.save(path, state)
    worker.hydrate_runtime()

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_snapshot(self, _receipt, *, force_full=False):
            assert force_full is False
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise TimeoutError("unchanged wait")

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    before_generation = worker.runtime_db.get_board()["generation"]
    before_version = worker.runtime_db.get_task_version(state["task_id"])
    before_detail = worker.runtime_db.get_task_detail(state["task_id"])
    scheduling_tasks = list(worker.registry.tasks_by_id.values())

    persist_calls = 0
    load_calls = 0
    original_persist = worker._persist_transport_result
    original_load = store.load

    def counted_persist(*args, **kwargs):
        nonlocal persist_calls
        persist_calls += 1
        return original_persist(*args, **kwargs)

    def counted_load(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(worker, "_persist_transport_result", counted_persist)
    monkeypatch.setattr(store, "load", counted_load)

    for _index in range(2):
        result = asyncio.run(
            worker.advance(
                path,
                SimpleNamespace(pages=[]),
                scheduling_tasks=scheduling_tasks,
            )
        )
        assert result is not None
        affected = worker.registry.update_task(result, now=time.time())
        worker._publish_affected(affected)
        scheduling_tasks = list(worker.registry.tasks_by_id.values())

    assert persist_calls == 0
    assert load_calls == 0
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert worker.runtime_db.get_board()["generation"] == before_generation
    assert worker.runtime_db.get_task_version(state["task_id"]) == before_version
    after_detail = worker.runtime_db.get_task_detail(state["task_id"])
    assert after_detail["projection_sha256"] == before_detail["projection_sha256"]




def test_publish_affected_reprojects_all_waiting_ranks_after_dependency_transition(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task(
        "first",
        requested_team="first",
        task_id="rank-first",
    )
    second = store.create_task(
        "second",
        requested_team="second",
        task_id="rank-second",
        depends_on_task_ids=("rank-first",),
    )
    third = store.create_task(
        "third",
        requested_team="third",
        task_id="rank-third",
        depends_on_task_ids=("rank-second",),
    )

    def make_waiting(current: dict) -> dict:
        current["status"] = "WAITING"
        current["kanban_column"] = "WAITING"
        current["waiting_reason"] = "Waiting for dependency"
        return current

    first = store.update(first["manifest_path"], make_waiting)
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()

    assert worker.runtime_db.get_task_detail(first["task_id"])["waiting_order"]["rank"] == 1
    assert worker.runtime_db.get_task_detail(second["task_id"])["waiting_order"]["rank"] == 2
    assert worker.runtime_db.get_task_detail(third["task_id"])["waiting_order"]["rank"] == 3
    third_version = worker.runtime_db.get_task_version(third["task_id"])

    def complete(current: dict) -> dict:
        current["status"] = "DONE"
        current["terminal_state"] = "DONE"
        current["kanban_column"] = "DONE"
        current["completed_at"] = utc_now()
        current["active_role"] = None
        current["active_hop_id"] = None
        current["active_action"] = "completed"
        current["waiting_reason"] = None
        return current

    completed = store.update(first["manifest_path"], complete)
    affected = worker.registry.update_task(completed, now=time.time())
    assert third["task_id"] not in affected

    worker._publish_affected(affected)

    assert worker.runtime_db.get_task_detail(second["task_id"])["waiting_order"]["rank"] == 1
    assert worker.runtime_db.get_task_detail(third["task_id"])["waiting_order"]["rank"] == 2
    assert worker.runtime_db.get_task_version(third["task_id"]) > third_version


def test_manifest_cache_reuses_parse_until_file_identity_changes(
    tmp_path: Path, monkeypatch
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-manifest-cache-identity"
    )
    path = Path(state["manifest_path"])
    original_load = store.load
    load_calls = 0

    def counted_load(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(store, "load", counted_load)

    first = worker._load_manifest_cached(path)
    second = worker._load_manifest_cached(path)

    assert first is second
    assert load_calls == 1

    path.write_bytes(path.read_bytes() + b"\n")
    third = worker._load_manifest_cached(path)

    assert third is not first
    assert third == first
    assert load_calls == 2



def test_refresh_scheduling_rejects_stale_preloaded_state(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "parent", requested_team="parent", task_id="task-cache-parent"
    )
    child = store.create_task(
        "child",
        requested_team="child",
        task_id="task-cache-child",
        depends_on_task_ids=(parent["task_id"],),
    )
    child_path = Path(child["manifest_path"])
    stale = store.load(child_path)
    store.update(
        child_path,
        lambda state: {**state, "waiting_reason": "external writer touched task"},
    )
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "kanban_column": "DONE_STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "active_action": "done",
            "completed_at": utc_now(),
        },
    )
    tasks = [parent, store.load(child_path)]

    with pytest.raises(ValueError, match="task changed while refreshing scheduling state"):
        store.refresh_scheduling(child_path, tasks=tasks, state=stale)

def test_refresh_scheduling_uses_preloaded_running_state_without_reload(
    tmp_path: Path, monkeypatch
):
    _config, store, state, _worker = setup_task(
        tmp_path, task_id="task-preloaded-scheduling-state"
    )
    path = Path(state["manifest_path"])
    loaded = store.load(path)

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("preloaded scheduling state must not reload the manifest")

    monkeypatch.setattr(store, "load", forbidden_load)

    refreshed, changed = store.refresh_scheduling(
        path,
        tasks=[loaded],
        state=loaded,
    )

    assert changed is False
    assert refreshed is loaded

def test_waiting_copy_on_write_does_not_duplicate_large_inert_history():
    large_history = "x" * 4_000_000
    state = {
        "active_hop_id": 2,
        "hops": [
            {"hop_id": 1, "state": "routed", "prompt": large_history},
            {
                "hop_id": 2,
                "state": "waiting",
                "wait": {"activity_signature": "same"},
                "timestamps": {"sent_at": "2026-07-26T00:00:00+00:00"},
                "errors": [],
            },
        ],
        "roles": {"PLAN": {"status": "active", "online": True}},
        "errors": [{"error": "historic"}],
    }

    working = worker_module._waiting_working_copy(state)

    assert working is not state
    assert working["hops"] is not state["hops"]
    assert working["hops"][0] is state["hops"][0]
    assert working["hops"][0]["prompt"] is large_history
    assert working["hops"][1] is not state["hops"][1]
    assert working["hops"][1]["wait"] is not state["hops"][1]["wait"]
    assert working["roles"] is not state["roles"]
    assert working["roles"]["PLAN"] is not state["roles"]["PLAN"]
    assert working["errors"] is not state["errors"]


def test_running_refresh_scheduling_skips_full_manifest_clone(tmp_path: Path, monkeypatch):
    _config, store, state, _worker = setup_task(
        tmp_path, task_id="task-running-scheduling-no-clone"
    )
    path = Path(state["manifest_path"])
    state["hops"][0]["historic_payload"] = "y" * 2_000_000
    state = store.save(path, state)

    import playwright_auto.cdpa_store as store_module

    def forbidden_dumps(*_args, **_kwargs):
        raise AssertionError("RUNNING scheduling refresh must not serialize the full manifest")

    monkeypatch.setattr(store_module.json, "dumps", forbidden_dumps)

    refreshed, changed = store.refresh_scheduling(path, tasks=[state])

    assert changed is False
    assert refreshed == state

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


def test_waiting_records_stable_malformed_final_then_repairs_on_next_boundary(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-malformed-gate"
    )
    malformed = MessageSnapshot("assistant", "a-bad", "ta-bad", "final prose", ())
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), malformed),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise StableMalformedResponseError(
                malformed,
                worker_module.RouteContractError("response must contain only one route JSON object"),
            )

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert hop["state"] == "responded"
    assert hop["validation_error"] == "response must contain only one route JSON object"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["turn"] == 1


def test_timeout_banner_without_stop_refreshes_after_no_progress_and_accepts_rehydrated_route(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-timeout-f5"
    )
    old = sent_at - timedelta(minutes=21)
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-timeout-f5.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("rehydrated report", encoding="utf-8")
    progress = MessageSnapshot(
        "assistant",
        "a-progress",
        "ta-progress",
        "Inspecting durable block and searching for functions",
        (),
    )
    final = MessageSnapshot(
        "assistant",
        "a-final",
        "ta-final",
        '{"route":"TEST","handoff":".plan/alpha/alpha-plan_turn1_task-timeout-f5.md"}',
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.ERROR,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=("Message delivery timed out. Please try again.",),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), progress),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    hop["timestamps"]["sent_at"] = old.isoformat()
    state = store.save(path, state)
    hop = _active_hop(state)

    class Client:
        def __init__(self):
            self.calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("no acceptable candidate before refresh")
            kwargs["candidate_validator"](final)
            return final

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1
            persisted = store.load(path)
            persisted_wait = _active_hop(persisted)["wait"]
            assert persisted_wait["activity_signature"] == signature
            assert persisted_wait["activity_length"] == length
            assert persisted_wait["activity_changed_at"] == old.isoformat()
            assert persisted_wait["recovery_baseline"]["assistant_message_ids"] == ["a-progress"]

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert actions.refresh_calls == 1
    assert client.calls == 2
    assert hop["state"] == "responded"
    assert hop["response"] == final.text
    assert hop["request_id"] == "task-timeout-f5-hop1"
    assert hop["turn"] == 1


def test_advance_accepts_response_after_refresh_without_remerging_old_baseline(
    tmp_path: Path,
    monkeypatch,
):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-refresh-response-merge",
    )
    old = sent_at - timedelta(minutes=21)
    report = (
        tmp_path
        / ".plan"
        / "alpha"
        / "alpha-plan_turn1_task-refresh-response-merge.md"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("response after refresh", encoding="utf-8")
    progress = MessageSnapshot(
        "assistant",
        "a-progress",
        "ta-progress",
        "still working before refresh",
        (),
    )
    final = MessageSnapshot(
        "assistant",
        "a-final",
        "ta-final",
        '{"route":"TEST","handoff":".plan/alpha/alpha-plan_turn1_task-refresh-response-merge.md"}',
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.ERROR,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=("Message delivery timed out. Please try again.",),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            progress,
        ),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
            "recovery_baseline": {
                "assistant_message_ids": ["a-prior-refresh"],
                "assistant_turn_ids": ["ta-prior-refresh"],
                "assistant_fingerprints": ["f" * 64],
            },
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.wait_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise TimeoutError("no final response before refresh")
            kwargs["candidate_validator"](final)
            return final

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

        async def refresh(self, _acquired):
            return None

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result == store.load(path)
    assert result["status"] == "RUNNING"
    assert result["block_code"] is None
    active = _active_hop(result)
    assert active["state"] == "responded"
    assert active["response"] == final.text
    assert active["wait"]["recovery_baseline"] is None
    assert client.wait_calls == 2


def test_pause_requested_during_refresh_survives_refresh_checkpoints(
    tmp_path: Path,
    monkeypatch,
):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-refresh-control-race",
    )
    old = sent_at - timedelta(minutes=21)
    progress = MessageSnapshot(
        "assistant",
        "a-progress",
        "ta-progress",
        "still working",
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.ERROR,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=("Message delivery timed out. Please try again.",),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            progress,
        ),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.wait_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            self.wait_calls += 1
            raise TimeoutError("continue polling")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class ConcurrentRefreshActions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1
            store.request_control(
                path,
                "pause",
                reason="pause requested during refresh",
            )

    actions = ConcurrentRefreshActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    refreshed = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert refreshed == store.load(path)
    assert _active_hop(refreshed)["state"] == "waiting"
    assert _active_hop(refreshed)["wait"]["refresh_count"] == 1
    assert refreshed["controls"][0]["action"] == "pause"
    assert refreshed["controls"][0]["status"] == "requested"
    assert actions.refresh_calls == 1

    paused = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert paused == store.load(path)
    assert paused["status"] == "PAUSED"
    assert paused["controls"][0]["status"] == "applied"
    assert actions.refresh_calls == 1


def test_waiting_persists_pre_refresh_provenance_before_f5(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-refresh-baseline")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    prompt = hop["prompt"]
    binding = PageBinding("page-PLAN", "PLAN")
    receipt = SendReceipt(
        prompt=prompt,
        prompt_sha256=prompt_digest(prompt),
        binding=binding,
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="t1",
    )
    sent_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = sent_at.isoformat()
    hop["wait"].update(
        {
            "started_at": sent_at.isoformat(),
            "deadline_at": (sent_at + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": sent_at.isoformat(),
        }
    )
    state["roles"]["PLAN"]["page_id"] = "page-PLAN"
    state = store.save(path, state)
    hop = _active_hop(state)

    snapshot = SimpleNamespace(
        state=ChatGPTState.RESPONDING,
        stop_visible=True,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", prompt, ()),
            MessageSnapshot("assistant", "a1", "t1", "pre-refresh result", ()),
        ),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["wait"].update(
        {
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": sent_at.isoformat(),
            "activity_observed_at": sent_at.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.wait_kwargs = None
            self.wait_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.wait_calls += 1
            self.wait_kwargs = kwargs
            raise TimeoutError("continue polling")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-PLAN",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1
            persisted = store.load(path)
            persisted_hop = _active_hop(persisted)
            assert persisted_hop["request_id"] == hop["request_id"]
            assert persisted_hop["wait"]["recovery_baseline"] == {
                "assistant_message_ids": ["a1"],
                "assistant_turn_ids": ["t1"],
                "assistant_fingerprints": persisted_hop["wait"]["recovery_baseline"][
                    "assistant_fingerprints"
                ],
            }
            assert len(
                persisted_hop["wait"]["recovery_baseline"]["assistant_fingerprints"]
            ) == 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert actions.refresh_calls == 1
    assert client.wait_calls == 2
    assert hop["state"] == "waiting"
    assert hop["request_id"] == "task-refresh-baseline-hop1"
    assert client.wait_kwargs["active_reload_after_ms"] is None
    assert client.wait_kwargs["stale_response_baseline"] == hop["wait"][
        "recovery_baseline"
    ]
    assert hop["wait"]["refresh_count"] == 1
    assert hop["wait"]["recovery_baseline"]["assistant_message_ids"] == ["a1"]


def test_timeout_boundary_reconciles_valid_final_route_before_blocking(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-final-timeout-reconcile"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-final-timeout-reconcile.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("final timeout evidence", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-final-timeout",
        "t-final-timeout",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    expired = datetime.now(timezone.utc) - timedelta(
        seconds=worker.config.response_timeout_seconds + 5
    )
    hop["timestamps"]["sent_at"] = expired.isoformat()
    hop["wait"]["started_at"] = expired.isoformat()
    hop["wait"]["deadline_at"] = (
        expired + timedelta(seconds=worker.config.response_timeout_seconds)
    ).isoformat()
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), response),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["wait"].update(
        {
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": expired.isoformat(),
            "activity_observed_at": expired.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0
            self.kwargs = None

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            self.kwargs = kwargs
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/final-timeout",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert actions.refresh_calls == 0
    assert client.calls == 1
    assert client.kwargs["minimum_samples"] == 2
    assert client.kwargs["timeout_ms"] >= worker.config.response_stable_ms
    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert state["status"] == "RUNNING"
    assert state.get("block_code") is None
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT



def test_refresh_due_reconciles_previously_observed_valid_final_before_f5(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-refresh-reconcile-valid"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-refresh-reconcile-valid.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("valid pre-refresh evidence", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-refresh-valid",
        "t-refresh-valid",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    old = sent_at - timedelta(minutes=21)
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), response),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/refresh-valid",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.calls == 1
    assert actions.refresh_calls == 0
    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert hop["wait"]["refresh_count"] == 0
    assert state.get("block_code") is None



def test_candidate_appearing_during_pre_refresh_read_prevents_f5(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-late-pre-refresh-candidate"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-late-pre-refresh-candidate.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("late pre-refresh evidence", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-late",
        "t-late",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    old = sent_at - timedelta(minutes=21)
    initial_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    fresh_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            response,
        ),
    )
    signature, length = response_activity_signature(initial_snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.current = initial_snapshot
            self.wait_calls = 0
            self.normal_baseline = "unset"

        async def assert_ownership(self):
            return self.current

        async def wait_for_response(self, _receipt, **kwargs):
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.current = fresh_snapshot
                raise TimeoutError("candidate appeared too late to stabilize")
            self.normal_baseline = kwargs["stale_response_baseline"]
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/late-pre-refresh",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.wait_calls == 2
    assert actions.refresh_calls == 0
    assert client.normal_baseline is None
    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert hop["wait"]["refresh_count"] == 0
    assert state.get("block_code") is None


def test_pre_refresh_f5_baseline_uses_fresh_post_read_snapshot(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-fresh-pre-refresh-baseline"
    )
    old = sent_at - timedelta(minutes=21)
    current = MessageSnapshot("assistant", "a-current", "t-current", "same latest", ())
    hidden = MessageSnapshot("assistant", "a-hidden", "t-hidden", "earlier output", ())
    initial_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            current,
        ),
    )
    fresh_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            hidden,
            current,
        ),
    )
    initial_signature, length = response_activity_signature(initial_snapshot, receipt.baseline)
    fresh_signature, _ = response_activity_signature(fresh_snapshot, receipt.baseline)
    assert fresh_signature == initial_signature
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": initial_signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.current = initial_snapshot
            self.wait_calls = 0

        async def assert_ownership(self):
            return self.current

        async def wait_for_response(self, _receipt, **_kwargs):
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.current = fresh_snapshot
            raise TimeoutError("no stable candidate")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/fresh-baseline",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0
            self.persisted_baseline = None

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1
            persisted = store.load(path)
            self.persisted_baseline = _active_hop(persisted)["wait"]["recovery_baseline"]

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.wait_calls == 2
    assert actions.refresh_calls == 1
    assert actions.persisted_baseline["assistant_message_ids"] == ["a-current", "a-hidden"]
    assert actions.persisted_baseline["assistant_turn_ids"] == ["t-current", "t-hidden"]
    assert len(actions.persisted_baseline["assistant_fingerprints"]) == 2
    assert hop["state"] == "waiting"
    assert hop["wait"]["recovery_baseline"] == actions.persisted_baseline


def test_deadline_crossing_during_pre_refresh_read_blocks_without_f5(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-deadline-crosses-during-read"
    )
    old = sent_at - timedelta(minutes=21)
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)
    remaining_values = iter((1_000, 0))
    monkeypatch.setattr(worker_module, "remaining_timeout_ms", lambda _wait: next(remaining_values))

    class Client:
        def __init__(self):
            self.wait_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            self.wait_calls += 1
            raise TimeoutError("deadline crossed during bounded read")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/deadline-cross",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.wait_calls == 1
    assert actions.refresh_calls == 0
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "response_timeout"
    assert "final response reconciliation" in state["block_reason"]

def test_expired_wait_without_candidate_reconciles_once_then_blocks_without_f5(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-expired-no-candidate"
    )
    expired = datetime.now(timezone.utc) - timedelta(
        seconds=worker.config.response_timeout_seconds + 5
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
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = expired.isoformat()
    hop["wait"].update(
        {
            "started_at": expired.isoformat(),
            "deadline_at": (
                expired + timedelta(seconds=worker.config.response_timeout_seconds)
            ).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": expired.isoformat(),
            "activity_observed_at": expired.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            self.calls += 1
            raise TimeoutError("no final response")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/expired-no-candidate",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.calls == 1
    assert actions.refresh_calls == 0
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "response_timeout"
    assert "final response reconciliation" in state["block_reason"]


def test_pre_refresh_reconciliation_preserves_prior_f5_stale_baseline(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-prior-f5-stale"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-prior-f5-stale.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("stale response evidence", encoding="utf-8")
    stale = MessageSnapshot(
        "assistant",
        "a-prior-f5",
        "t-prior-f5",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    old = sent_at - timedelta(minutes=21)
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), stale),
    )
    stale_baseline = capture_response_recovery_baseline(snapshot.messages, receipt.baseline)
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
            "recovery_baseline": stale_baseline,
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0
            self.baselines = []

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            self.baselines.append(kwargs["stale_response_baseline"])
            assert stale.message_id in kwargs["stale_response_baseline"][
                "assistant_message_ids"
            ]
            raise TimeoutError("candidate remains stale")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/prior-f5-stale",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.calls == 2
    assert actions.refresh_calls == 1
    assert hop["state"] == "waiting"
    assert hop.get("response") is None
    assert hop["wait"]["recovery_baseline"] == stale_baseline
    assert all(baseline == stale_baseline for baseline in client.baselines)

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


def test_worker_reconnect_supervisor_preserves_inflight_identity(tmp_path: Path, monkeypatch):
    from contextlib import asynccontextmanager

    config, store, state, _worker = setup_task(
        tmp_path, task_id="task-supervisor-reconnect"
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "waiting"
    hop["receipt"] = {"request_id": hop["request_id"]}
    hop["timestamps"]["sent_at"] = datetime.now(timezone.utc).isoformat()
    _worker._start_wait_budget_from_sent(hop)
    store.save(path, state)
    identity = (
        state["task_id"],
        state["active_hop_id"],
        hop["request_id"],
        hop["turn"],
        len(state["hops"]),
        len(state["reports"]),
        len(state["controls"]),
    )
    TargetClosedError = type("TargetClosedError", (RuntimeError,), {})
    connections: list[int] = []
    observed: list[tuple[object, ...]] = []
    disconnects: list[str] = []

    @asynccontextmanager
    async def fake_connected_browser(_url):
        number = len(connections) + 1
        connections.append(number)
        yield SimpleNamespace(contexts=[SimpleNamespace(connection=number)])

    class FakeWorker:
        def __init__(self, _config):
            self.calls = 0
            self.command_loop_started = asyncio.Event()
            self.runtime_db = SimpleNamespace(close=lambda: None)

        def hydrate_runtime(self):
            return {"complete": True}

        def _publish_heartbeat(self, **_kwargs):
            return None

        def _publish_browser_disconnected(self):
            disconnects.append("disconnected")

        async def run_command_loop(self):
            self.command_loop_started.set()
            await asyncio.Event().wait()

        async def run_forever(self, _context):
            self.calls += 1
            current = TaskStore(config).load(path)
            current_hop = _active_hop(current)
            observed.append(
                (
                    current["task_id"],
                    current["active_hop_id"],
                    current_hop["request_id"],
                    current_hop["turn"],
                    len(current["hops"]),
                    len(current["reports"]),
                    len(current["controls"]),
                )
            )
            if self.calls == 1:
                raise TargetClosedError("browser has been closed")
            raise asyncio.CancelledError

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(worker_module, "connected_browser", fake_connected_browser)
    monkeypatch.setattr(worker_module, "CDPAWorker", FakeWorker)
    monkeypatch.setattr(worker_module.asyncio, "sleep", no_delay)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_module._run(config))

    assert connections == [1, 2]
    assert disconnects == ["disconnected"]
    assert observed == [identity, identity]
    current = store.load(path)
    assert _active_hop(current)["request_id"] == hop["request_id"]
    assert len(current["hops"]) == 1
    assert current["reports"] == []
    assert current["controls"] == []


def test_clear_team_reverifies_and_closes_tab_after_false_cleared_state(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleared-tab-recheck")
    path = Path(state["manifest_path"])
    state.update(
        {
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
        }
    )
    state["cleanup"].update(
        {
            "state": "CLEARED",
            "phase": "cleared",
            "cleared_at": datetime.now(timezone.utc).isoformat(),
            "verified_empty_at": datetime.now(timezone.utc).isoformat(),
            "closed_tabs": 0,
        }
    )
    state["controls"].append(
        {
            "control_id": 1,
            "action": "clear_team",
            "role": "PLAN",
            "reason": None,
            "confirmed": True,
            "status": "requested",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "applied_at": None,
            "result": None,
        }
    )
    store.save(path, state)
    actions = FakeActions()
    actions.pages = ["surviving-tab"]

    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    result = state

    assert actions.preflight_calls == 2
    assert actions.closed_teams == 1
    assert actions.pages == []
    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["verified_empty_at"]
    assert result["controls"][-1]["status"] == "applied"
    assert result["controls"][-1]["result"]["reverified"] is True


def test_clear_team_failed_reverify_clears_old_verification_evidence(
    tmp_path: Path,
    monkeypatch,
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleared-tab-recheck-failure")
    path = Path(state["manifest_path"])
    old_verified = datetime.now(timezone.utc).isoformat()
    state.update(
        {
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
        }
    )
    state["cleanup"].update(
        {
            "state": "CLEARED",
            "phase": "cleared",
            "cleared_at": old_verified,
            "verified_empty_at": old_verified,
            "closed_tabs": 0,
        }
    )
    state["controls"].append(
        {
            "control_id": 1,
            "action": "clear_team",
            "role": "PLAN",
            "reason": None,
            "confirmed": True,
            "status": "requested",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "applied_at": None,
            "result": None,
        }
    )
    store.save(path, state)

    class ReverifyFailureActions(FakeActions):
        async def close_team(self, _state, *, preflighted_pages=None):
            self.closed_teams += 1
            raise TeamCloseError("synthetic reverify close failure", closed_tabs=0)

    actions = ReverifyFailureActions()
    actions.pages = ["surviving-tab"]
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    persisted = store.load(path)

    assert result == persisted
    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "close_pending"
    assert persisted["cleanup"]["cleared_at"] is None
    assert persisted["cleanup"]["verified_empty_at"] is None
    assert persisted["cleanup"]["retry_count"] == 1
    assert "synthetic reverify close failure" in persisted["cleanup"]["last_error"]
    assert persisted["controls"][0]["status"] == "cleanup_pending"
    assert persisted["controls"][0]["result"].get("reverified") is None
    assert actions.pages == ["surviving-tab"]


def test_run_once_activates_independent_recovery_after_task_iteration(
    tmp_path: Path, monkeypatch
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-maintainer-run-once"
    )
    store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )

    async def fake_advance(
        manifest_path, _browser_context, *, scheduling_tasks=None
    ):
        current = store.load(manifest_path)
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "offline"
        return store.save(manifest_path, current)

    monkeypatch.setattr(worker, "advance", fake_advance)
    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    agent = next(
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item["independent"]["agent_name"] == "Maintainers"
    )

    assert len(results) == 1
    assert results[0]["status"] == "BLOCKED"
    assert agent["status"] == "RUNNING"
    assert agent["independent"]["active_event"]["target_task_id"] == state["task_id"]
    assert not hasattr(worker, "maintainers")

def _inline_response(route="DONE", body="# Inline report\n\nEvidence."):
    return f'{body}\n\n```json\n{{"route":"{route}","handoff":"INLINE"}}\n```'


def test_inline_pre_send_and_repair_guidance_never_expose_expected_path(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-prompt", report_mode="inline"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert "Do not create, edit, or write any role-report file" in hop["prompt"]
    assert "the worker owns report materialization" in hop["prompt"]
    assert '"handoff":"INLINE"' in hop["prompt"]
    assert hop["expected_report_path"] not in hop["prompt"]
    assert "Report naming rule:" not in hop["prompt"]

    hop["response"] = "bad"
    hop["state"] = "responded"
    worker._responded(state, hop)
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))

    assert "corrected inline Markdown" in repair["prompt"]
    assert '"handoff":"INLINE"' in repair["prompt"]
    assert repair["expected_report_path"] not in repair["prompt"]
    assert "Report naming rule:" not in repair["prompt"]


def test_inline_response_materializes_exact_report_and_routes(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-materialize", report_mode="inline"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
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
    next_hop = _active_hop(state)
    assert next_hop["target_role"] == "DEV"
    assert next_hop["handoff"] == hop["expected_report_path"]


def test_inline_materialization_failure_blocks_operationally_without_path_or_repair(
    tmp_path: Path, monkeypatch
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-write-fail", report_mode="inline"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = _inline_response(route="DEV")
    hop["state"] = "responded"
    internal = str((tmp_path / hop["expected_report_path"]).resolve()) + ".lock"
    monkeypatch.setattr(
        worker_module,
        "materialize_inline_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            worker_module.InlineReportMaterializationError(
                f"Permission denied: {internal}"
            )
        ),
    )

    worker._responded(state, hop)

    assert hop["report_path"] is None
    assert state["reports"] == []
    assert len(state["hops"]) == 1
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert state["block_reason"] == "inline report materialization failed"
    assert internal not in state["block_reason"]
    events = canonical_recovery_events(state)
    assert len(events) == 1
    assert events[0]["failure_signature"].startswith(
        "inline_report_materialization_failed:"
    )



@pytest.mark.parametrize("failure_point", ["parent_mkdir", "lock_is_symlink"])
def test_inline_raw_filesystem_failure_blocks_without_route_repair(
    tmp_path: Path,
    monkeypatch,
    failure_point: str,
):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id=f"task-inline-{failure_point}",
        report_mode="inline",
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    response = _inline_response(route="DEV")
    hop["response"] = response
    hop["state"] = "responded"
    target = (tmp_path / hop["expected_report_path"]).resolve()

    if failure_point == "parent_mkdir":
        original = Path.mkdir

        def fail_parent_mkdir(self, *args, **kwargs):
            if self == target.parent:
                raise PermissionError(13, "Permission denied", str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_parent_mkdir)
    else:
        lock = target.with_suffix(target.suffix + ".lock")
        original = Path.is_symlink

        def fail_lock_is_symlink(self):
            if self == lock:
                raise PermissionError(13, "Permission denied", str(self))
            return original(self)

        monkeypatch.setattr(Path, "is_symlink", fail_lock_is_symlink)

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert state["block_reason"] == "inline report materialization failed"
    assert len(state["hops"]) == 1
    assert hop["state"] == "responded"
    assert hop["response"] == response
    assert hop["report_path"] is None
    assert state["reports"] == []
    assert str(target) not in state["block_reason"]
    assert all(str(target) not in str(item) for item in hop.get("errors") or ())

    first = canonical_recovery_events(state)
    second = canonical_recovery_events(state)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith(
        "inline_report_materialization_failed:"
    )


@pytest.mark.parametrize("local_failure", ["different_bytes", "target_symlink"])
def test_inline_local_report_state_failure_preserves_sent_request_and_blocks(
    tmp_path: Path,
    local_failure: str,
):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id=f"task-inline-{local_failure}",
        report_mode="inline",
    )
    response = _inline_response(route="DEV")
    hop["response"] = response
    hop["state"] = "responded"
    target = (tmp_path / hop["expected_report_path"]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if local_failure == "different_bytes":
        target.write_text("different existing report", encoding="utf-8")
    else:
        outside = tmp_path / "outside-inline-report.md"
        outside.write_text("outside", encoding="utf-8")
        target.symlink_to(outside)

    ledger = RequestLedger(hop["ledger_path"])
    assert ledger.get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert state["block_reason"] == "inline report materialization failed"
    assert len(state["hops"]) == 1
    assert hop["state"] == "responded"
    assert hop["response"] == response
    assert hop["report_path"] is None
    assert state["reports"] == []
    assert ledger.get(hop["request_id"]).status is RequestStatus.SENT
    assert str(target) not in state["block_reason"]
    assert all(str(target) not in str(item) for item in hop.get("errors") or ())

    first = canonical_recovery_events(state)
    second = canonical_recovery_events(state)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith(
        "inline_report_materialization_failed:"
    )

def test_inline_materialization_is_restart_idempotent_and_ledger_completes_once(
    tmp_path: Path
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-inline-restart", report_mode="inline"
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=hop["prompt"],
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=hop["prompt"],
        prompt_sha256=prompt_digest(hop["prompt"]),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="inline-restart",
        user_message_id="u-inline",
        user_turn_id="t-inline",
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
    hop["response"] = _inline_response()
    hop["response_record"] = {
        "role": "assistant",
        "message_id": "a-inline",
        "turn_id": "ta-inline",
        "text": hop["response"],
        "actions": [],
        "image_count": 0,
    }
    hop["state"] = "responded"
    store.save(path, state)

    expected = (tmp_path / hop["expected_report_path"]).resolve()
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text("# Inline report\n\nEvidence.", encoding="utf-8")
    before_stat = expected.stat()

    restarted = CDPAWorker(worker.config, store=store)
    result = asyncio.run(restarted.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "DONE"
    assert len(result["reports"]) == 1
    assert expected.read_text(encoding="utf-8") == "# Inline report\n\nEvidence."
    assert expected.stat().st_mtime_ns == before_stat.st_mtime_ns
    persisted = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert persisted is not None
    assert persisted.status is RequestStatus.COMPLETED

    manifest_before = path.read_bytes()
    result_again = asyncio.run(
        restarted.advance(
            path, SimpleNamespace(pages=[]), scheduling_tasks=[result]
        )
    )
    assert result_again["status"] == "DONE"
    assert path.read_bytes() == manifest_before
    assert len(result_again["reports"]) == 1
    assert expected.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_exhausted_inline_validation_block_creates_one_maintenance_incident(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-exhausted", report_mode="inline"
    )
    hop = _active_hop(state)
    hop["repair_attempt"] = worker.config.route_repair_attempts
    worker._repair_route(state, hop, RouteContractError("inline Markdown report is empty"))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "route_validation_exhausted"
    first = canonical_recovery_events(state)
    second = canonical_recovery_events(state)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("route_validation_exhausted:")



def test_waiting_inline_candidate_uses_same_stability_gate_before_materialization(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-inline-stable-gate",
        report_mode="inline",
    )
    response = MessageSnapshot(
        "assistant",
        "a-inline-stable",
        "ta-inline-stable",
        _inline_response(route="TEST"),
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
    expected = (tmp_path / hop["expected_report_path"]).resolve()

    class Client:
        def __init__(self):
            self.kwargs = None

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.kwargs = kwargs
            kwargs["candidate_validator"](response)
            assert not expected.exists()
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
    assert not expected.exists()
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)

    assert expected.read_text(encoding="utf-8") == "# Inline report\n\nEvidence."
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED
    assert _active_hop(state)["target_role"] == "TEST"



@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, "other"])
def test_worker_rejects_explicit_invalid_report_mode(value: object):
    with pytest.raises(ValueError, match="report_mode"):
        _report_mode({"options": {"report_mode": value}})



def test_worker_defaults_missing_legacy_report_mode_to_file():
    assert _report_mode({"options": {}}) == "file"


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


def test_stopped_and_missing_dependencies_remain_waiting_across_restart(tmp_path: Path):
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
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "failed parent",
        },
    )
    path = Path(child["manifest_path"])

    stopped = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))
    assert stopped["status"] == "WAITING"
    assert stopped["waiting"]["stopped"] == ["task-parent"]
    assert stopped["waiting_code"] == "dependency_stopped"

    parent_path = Path(parent["manifest_path"])
    parent_path.unlink()
    missing = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))
    assert missing["status"] == "WAITING"
    assert missing["waiting"]["missing"] == ["task-parent"]
    assert missing["waiting_code"] == "dependency_missing"
    assert missing["hops"][0]["state"] == "pre_send"
    assert store.load(path)["waiting"]["missing"] == ["task-parent"]


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


def test_run_once_cannot_create_two_mixed_same_team_owners(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("parent", requested_team="parent", task_id="task-parent")
    first = store.create_task(
        "first",
        requested_team="alpha",
        task_id="task-first",
        depends_on_task_ids=(parent["task_id"],),
    )
    second = store.create_task(
        "second",
        reuse_team="alpha",
        task_id="task-second",
        depends_on_task_ids=(parent["task_id"],),
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
    worker = CDPAWorker(config, store=store)

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            assert len(tasks) == 3
            return False

    worker.maintainers = FakeCoordinator()
    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    first_state = store.load(first["manifest_path"])
    second_state = store.load(second["manifest_path"])
    states = [first_state, second_state]
    assert sum(state["status"] == "INBOX" for state in states) == 1
    assert sum(state["status"] == "WAITING" for state in states) == 1
    owner = next(state for state in states if state["status"] == "INBOX")
    waiting = next(state for state in states if state["status"] == "WAITING")
    assert owner["task_id"] == first["task_id"]
    assert waiting["waiting"]["blocked_by_task_id"] == owner["task_id"]
    assert waiting["hops"][0]["state"] == "pre_send"


def test_run_once_atomically_releases_only_one_same_team_queue_owner(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued_a = store.create_task("queued a", reuse_team="alpha", task_id="task-a")
    queued_b = store.create_task("queued b", reuse_team="alpha", task_id="task-b")
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

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            assert len(tasks) == 3
            return False

    worker.maintainers = FakeCoordinator()
    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    states = [store.load(queued_a["manifest_path"]), store.load(queued_b["manifest_path"])]
    assert sum(state["status"] == "INBOX" for state in states) == 1
    assert sum(state["status"] == "WAITING" for state in states) == 1
    owner_ids = [state["task_id"] for state in states if state["status"] == "INBOX"]
    waiting = next(state for state in states if state["status"] == "WAITING")
    assert waiting["waiting"]["blocked_by_task_id"] == owner_ids[0]


@pytest.mark.parametrize("owner_failure", ["missing", "invalid_json"])
def test_queue_owner_loss_stays_waiting_before_browser_and_opens_one_incident(
    tmp_path: Path,
    owner_failure: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    before = store.load(queued["manifest_path"])
    owner_path = Path(owner["manifest_path"])
    if owner_failure == "missing":
        owner_path.unlink()
    else:
        owner_path.write_text("{", encoding="utf-8")

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            queued["manifest_path"], ExplodingBrowserContext()
        )
    )

    assert result["status"] == "WAITING"
    assert result["waiting_code"] == "queue_release_failed"
    assert result["queue"] == before["queue"]
    assert result["queue"]["released_at"] is None
    assert result["active_hop_id"] == before["active_hop_id"]
    assert result["hops"] == before["hops"]
    first = canonical_recovery_events(result)
    second = canonical_recovery_events(result)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("queue_release_failed:")


def test_queue_catalog_mismatch_persists_across_two_worker_polls_without_reconciliation(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    before = store.load(queued["manifest_path"])
    owner_key = store._catalog_key(owner["manifest_path"])
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    catalog["entries"][owner_key]["status"] = "DONE"
    poisoned_owner_entry = json.loads(json.dumps(catalog["entries"][owner_key]))
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    worker = CDPAWorker(config, store=store)

    first = asyncio.run(
        worker.advance(queued["manifest_path"], ExplodingBrowserContext())
    )
    first_events = canonical_recovery_events(first)
    assert len(first_events) == 1
    first_event_key = first_events[0]["event_key"]

    after_first = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert after_first["entries"][owner_key] == poisoned_owner_entry
    assert first["status"] == "WAITING"
    assert first["waiting_code"] == "queue_release_failed"
    assert first["queue"]["released_at"] is None
    assert first["hops"] == before["hops"]

    second = asyncio.run(
        worker.advance(queued["manifest_path"], ExplodingBrowserContext())
    )
    second_events = canonical_recovery_events(second)

    after_second = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert after_second["entries"][owner_key] == poisoned_owner_entry
    assert second["status"] == "WAITING"
    assert second["waiting_code"] == "queue_release_failed"
    assert second["queue"]["released_at"] is None
    assert second["hops"] == before["hops"]
    assert len(second_events) == 1
    assert second_events[0]["event_key"] == first_event_key


def test_queue_release_is_restart_idempotent_and_preserves_unsent_plan_identity(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    original = store.load(queued["manifest_path"])
    original_hop = json.loads(json.dumps(original["hops"][0]))
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

    released = asyncio.run(
        CDPAWorker(config, store=store).advance(
            queued["manifest_path"], ExplodingBrowserContext()
        )
    )
    restarted_store = TaskStore(config)
    reloaded, changed = restarted_store.refresh_scheduling(queued["manifest_path"])

    assert changed is False
    assert reloaded["queue"]["released_at"] == released["queue"]["released_at"]
    assert sum(item["status"] == "RELEASED" for item in reloaded["queue_events"]) == 1
    assert reloaded["active_hop_id"] == original["active_hop_id"] == 1
    for field in ("hop_id", "turn", "request_id", "state", "handoff", "ledger_path"):
        assert reloaded["hops"][0][field] == original_hop[field]
    assert reloaded["hops"][0]["state"] == "pre_send"


def test_queue_owner_conflict_remains_waiting_and_opens_one_incident(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    store.create_task("owner", requested_team="alpha", task_id="task-owner")
    first = store.create_task("first", reuse_team="alpha", task_id="task-first")
    second = store.create_task("second", reuse_team="alpha", task_id="task-second")
    store.update(
        first["manifest_path"],
        lambda state: {
            **state,
            "status": "INBOX",
            "kanban_column": "INBOX",
            "active_action": "queued",
            "queue": {**state["queue"], "released_at": utc_now()},
            "waiting": {
                "reason": None,
                "waiting_on": [],
                "stopped": [],
                "missing": [],
                "blocked_by_task_id": None,
                "since": None,
            },
        },
    )

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            second["manifest_path"], ExplodingBrowserContext()
        )
    )

    assert result["status"] == "WAITING"
    assert result["waiting_code"] == "team_owner_conflict"
    assert result["queue"]["released_at"] is None
    first_events = canonical_recovery_events(result)
    second_events = canonical_recovery_events(result)
    assert len(first_events) == len(second_events) == 1
    assert first_events[0]["event_key"] == second_events[0]["event_key"]
    assert first_events[0]["failure_signature"].startswith("team_owner_conflict:")


def test_queue_release_failure_remains_waiting_and_opens_one_incident(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    worker = CDPAWorker(config, store=store)

    def fail_refresh(_path):
        raise RuntimeError("injected queue release failure")

    monkeypatch.setattr(store, "refresh_scheduling", fail_refresh)
    result = asyncio.run(
        worker.advance(queued["manifest_path"], ExplodingBrowserContext())
    )

    assert result["status"] == "WAITING"
    assert result["waiting_code"] == "queue_release_failed"
    first = canonical_recovery_events(result)
    second = canonical_recovery_events(result)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("queue_release_failed:")


@pytest.mark.parametrize(
    "phase",
    ["stop_pending", "close_pending", "closing", "verify_pending"],
)
def test_queue_waits_for_clearing_owner_until_verified_empty(tmp_path: Path, phase: str):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    owner = store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "team cleared",
            "cleanup": {
                **state["cleanup"],
                "state": "CLEARING",
                "phase": phase,
                "verified_empty_at": None,
            },
        },
    )
    before = store.load(queued["manifest_path"])

    first, changed = store.refresh_scheduling(queued["manifest_path"])
    second, changed_again = TaskStore(config).refresh_scheduling(queued["manifest_path"])

    assert changed is False or first["status"] == "WAITING"
    assert first["status"] == "WAITING"
    assert first["waiting_code"] == "team_busy"
    assert first["waiting"]["blocked_by_task_id"] == owner["task_id"]
    assert second["status"] == "WAITING"
    assert second["queue"]["released_at"] is None
    assert second["hops"] == before["hops"]
    assert changed_again is False
    assert len(second["queue_events"]) == len(first["queue_events"])

    store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "cleanup": {
                **state["cleanup"],
                "state": "CLEARED",
                "phase": "cleared",
                "cleared_at": utc_now(),
                "verified_empty_at": None,
            },
        },
    )
    unverified, changed = TaskStore(config).refresh_scheduling(
        queued["manifest_path"]
    )
    assert changed is False
    assert unverified["status"] == "WAITING"
    assert unverified["waiting"]["blocked_by_task_id"] == owner["task_id"]

    store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "cleanup": {
                **state["cleanup"],
                "verified_empty_at": utc_now(),
            },
        },
    )
    released, changed = TaskStore(config).refresh_scheduling(queued["manifest_path"])
    assert changed is True
    assert released["status"] == "INBOX"
    assert released["queue"]["released_at"]


def test_terminal_cleanup_is_suppressed_while_same_team_queue_exists(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    owner = store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": old,
            "last_role_activity_at": old,
        },
    )

    released, changed = store.refresh_scheduling(queued["manifest_path"])
    assert changed is True
    assert released["status"] == "INBOX"

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            owner["manifest_path"],
            ExplodingBrowserContext(),
            scheduling_tasks=[owner, released],
        )
    )

    assert result["status"] == "DONE"
    assert result["cleanup"]["state"] == "ACTIVE"


def test_terminal_cleanup_ignores_catalog_invalid_same_team_queue(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    owner = store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": old,
            "last_role_activity_at": old,
        },
    )
    poison_catalog_identity_entry(store, queued)
    tasks, _errors = store.discover_with_errors()
    assert [task["task_id"] for task in tasks] == [owner["task_id"]]
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            owner["manifest_path"],
            SimpleNamespace(pages=[]),
            scheduling_tasks=tasks,
        )
    )

    assert result["status"] == "DONE"
    assert result["cleanup"]["state"] == "CLEARED"
    assert actions.closed_teams == 1


def test_queue_rebind_failure_blocks_for_maintainers(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
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
    released = asyncio.run(
        worker.advance(queued["manifest_path"], ExplodingBrowserContext())
    )
    assert released["status"] == "INBOX"

    blocked = asyncio.run(
        worker.advance(queued["manifest_path"], ExplodingBrowserContext())
    )

    assert blocked["status"] == "BLOCKED"
    assert blocked["block_code"] == "queue_rebind_failed"
    first = canonical_recovery_events(blocked)
    second = canonical_recovery_events(blocked)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("queue_rebind_failed:")


def test_clear_team_rejects_terminal_cleanup_when_queue_exists(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    owner = store.update(
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
    released, changed = store.refresh_scheduling(queued["manifest_path"])
    assert changed is True
    assert released["status"] == "INBOX"
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            owner["manifest_path"], SimpleNamespace(pages=[])
        )
    )

    assert result["cleanup"]["state"] == "ACTIVE"
    assert result["controls"][-1]["status"] == "rejected"
    assert "queued exact-team work" in result["controls"][-1]["result"]
    assert actions.preflight_calls == 1
    assert actions.closed_teams == 0


def test_clear_team_released_queue_is_blocked_by_ordinary_same_team_waiter(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("parent", requested_team="parent", task_id="task-parent")
    waiter = store.create_task(
        "waiter",
        requested_team="alpha",
        task_id="task-waiter",
        depends_on_task_ids=(parent["task_id"],),
    )
    owner = store.create_task("owner", reuse_team="alpha", task_id="task-owner")
    owner, changed = store.refresh_scheduling(owner["manifest_path"])
    assert changed is True
    assert owner["status"] == "INBOX"
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            owner["manifest_path"],
            SimpleNamespace(pages=[]),
        )
    )

    assert store.load(waiter["manifest_path"])["status"] == "WAITING"
    assert result["status"] == "INBOX"
    assert result["cleanup"]["state"] == "ACTIVE"
    assert result["controls"][-1]["status"] == "rejected"
    assert "other nonterminal exact-team work" in result["controls"][-1]["result"]
    assert actions.closed_teams == 0


def test_clear_team_succeeds_for_active_released_queue_without_successor(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
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
    released, changed = store.refresh_scheduling(queued["manifest_path"])
    assert changed is True
    assert released["status"] == "INBOX"
    store.request_control(queued["manifest_path"], "clear_team", confirmed=True)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            queued["manifest_path"],
            SimpleNamespace(pages=[]),
        )
    )

    assert result["status"] == "STOPPED"
    assert result["cleanup"]["state"] == "CLEARED"
    assert result["controls"][-1]["status"] == "applied"
    assert actions.closed_teams == 1


def test_clear_team_ignores_catalog_invalid_same_team_queue(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    owner = store.update(
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
    poison_catalog_identity_entry(store, queued)
    tasks, _errors = store.discover_with_errors()
    assert [task["task_id"] for task in tasks] == [owner["task_id"]]
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            owner["manifest_path"],
            SimpleNamespace(pages=[]),
            scheduling_tasks=tasks,
        )
    )

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["controls"][-1]["status"] == "applied"
    assert actions.closed_teams == 1


def test_run_once_refreshes_clear_team_sibling_guard_after_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    original_advance = worker.advance
    queued = None

    async def create_after_snapshot(path, browser_context, *, scheduling_tasks=None):
        nonlocal queued
        if queued is None:
            queued = store.create_task(
                "queued",
                reuse_team="alpha",
                task_id="task-queued",
            )
        return await original_advance(
            path,
            browser_context,
            scheduling_tasks=scheduling_tasks,
        )

    class FakeCoordinator:
        async def advance(self, _tasks, _browser_context):
            return False

    monkeypatch.setattr(worker, "advance", create_after_snapshot)
    worker.maintainers = FakeCoordinator()

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert queued is not None
    assert store.load(queued["manifest_path"])["status"] == "WAITING"
    assert results[0]["status"] == "INBOX"
    assert results[0]["cleanup"]["state"] == "ACTIVE"
    assert results[0]["controls"][-1]["status"] == "rejected"
    assert "other nonterminal exact-team work" in results[0]["controls"][-1]["result"]
    assert actions.closed_teams == 0


def test_clear_team_preserves_concurrent_target_control_during_preflight(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)

    class ConcurrentControlActions(FakeActions):
        async def preflight_team(self, state):
            self.preflight_calls += 1
            if self.preflight_calls == 1:
                store.request_control(
                    state["manifest_path"],
                    "pause",
                    reason="concurrent dashboard pause",
                )
            return list(self.pages)

    actions = ConcurrentControlActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        worker.advance(
            owner["manifest_path"],
            SimpleNamespace(pages=[]),
            scheduling_tasks=[owner],
        )
    )

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["status"] == "INBOX"
    assert persisted["cleanup"]["state"] == "ACTIVE"
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "rejected", None),
        (2, "pause", "requested", "concurrent dashboard pause"),
    ]
    assert "task changed while cleanup was being prepared" in persisted["controls"][0]["result"]
    assert actions.closed_teams == 0


def test_clear_team_preserves_sibling_guard_control_through_atomic_transaction(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    sibling = store.create_task("sibling", reuse_team="alpha", task_id="task-sibling")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    original_begin = store.begin_team_cleanup

    def add_control_before_atomic_guard(*args, **kwargs):
        store.request_control(
            owner["manifest_path"],
            "pause",
            reason="concurrent dashboard pause",
        )
        return original_begin(*args, **kwargs)

    monkeypatch.setattr(store, "begin_team_cleanup", add_control_before_atomic_guard)

    result = asyncio.run(
        worker.advance(owner["manifest_path"], SimpleNamespace(pages=[]))
    )

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert store.load(sibling["manifest_path"])["status"] == "WAITING"
    assert persisted["cleanup"]["state"] == "ACTIVE"
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "rejected", None),
        (2, "pause", "requested", "concurrent dashboard pause"),
    ]
    assert "other nonterminal exact-team work" in persisted["controls"][0]["result"]
    assert actions.preflight_calls == 1
    assert actions.closed_teams == 0


def test_clear_team_preserves_post_transaction_control_without_redundant_save(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    store.request_control(owner["manifest_path"], "clear_team", confirmed=True)
    worker = CDPAWorker(config, store=store)

    class ConcurrentControlActions(FakeActions):
        async def preflight_team(self, state):
            self.preflight_calls += 1
            if self.preflight_calls == 1:
                store.request_control(
                    state["manifest_path"],
                    "resume",
                    reason="preflight conflict trigger",
                )
            return list(self.pages)

    actions = ConcurrentControlActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    original_begin = store.begin_team_cleanup

    def add_control_after_transaction(*args, **kwargs):
        saved, started = original_begin(*args, **kwargs)
        assert started is False
        store.request_control(
            owner["manifest_path"],
            "pause",
            reason="post-transaction dashboard pause",
        )
        return saved, started

    monkeypatch.setattr(store, "begin_team_cleanup", add_control_after_transaction)

    result = asyncio.run(
        worker.advance(owner["manifest_path"], SimpleNamespace(pages=[]))
    )

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["cleanup"]["state"] == "ACTIVE"
    assert [
        (item["control_id"], item["action"], item["status"], item.get("reason"))
        for item in persisted["controls"]
    ] == [
        (1, "clear_team", "rejected", None),
        (2, "resume", "requested", "preflight conflict trigger"),
        (3, "pause", "requested", "post-transaction dashboard pause"),
    ]
    assert actions.closed_teams == 0


def test_automatic_cleanup_preserves_concurrent_target_control_during_preflight(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    owner = store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
        },
    )
    worker = CDPAWorker(config, store=store)

    class ConcurrentControlActions(FakeActions):
        async def preflight_team(self, state):
            self.preflight_calls += 1
            if self.preflight_calls == 1:
                store.request_control(
                    state["manifest_path"],
                    "pause",
                    reason="concurrent dashboard pause",
                )
            return list(self.pages)

    actions = ConcurrentControlActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(
        worker.advance(
            owner["manifest_path"],
            SimpleNamespace(pages=[]),
            scheduling_tasks=[owner],
        )
    )

    persisted = store.load(owner["manifest_path"])
    assert result == persisted
    assert persisted["status"] == "DONE"
    assert persisted["terminal_state"] == "DONE"
    assert persisted["cleanup"]["state"] == "ACTIVE"
    assert persisted["controls"][-1]["action"] == "pause"
    assert persisted["controls"][-1]["status"] == "requested"
    assert persisted["controls"][-1]["reason"] == "concurrent dashboard pause"
    assert actions.closed_teams == 0


def test_run_once_refreshes_automatic_cleanup_sibling_guard_after_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    owner = store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
        },
    )
    worker = CDPAWorker(config, store=store)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    original_advance = worker.advance
    queued = None

    async def create_after_snapshot(path, browser_context, *, scheduling_tasks=None):
        nonlocal queued
        if queued is None:
            queued = store.create_task(
                "queued",
                reuse_team="alpha",
                task_id="task-queued",
            )
        return await original_advance(
            path,
            browser_context,
            scheduling_tasks=scheduling_tasks,
        )

    class FakeCoordinator:
        async def advance(self, _tasks, _browser_context):
            return False

    monkeypatch.setattr(worker, "advance", create_after_snapshot)
    worker.maintainers = FakeCoordinator()

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert queued is not None
    assert store.load(queued["manifest_path"])["status"] == "WAITING"
    assert results[0]["status"] == "DONE"
    assert results[0]["cleanup"]["state"] == "ACTIVE"
    assert results[0]["cleanup"]["cleared_at"] is None
    assert actions.closed_teams == 0


def test_run_once_skips_replaced_immutable_history_without_log_noise(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    from playwright_auto.cdpa_projection import build_task_projection

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team="parent", task_id="task-parent-history"
    )
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child-history",
        depends_on_task_ids=("task-parent-history",),
    )
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "kanban_column": "DONE_STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "active_action": "stopped",
            "stopped_at": utc_now(),
            "stop_reason": "replacement probe",
        },
    )
    result = store.replace_task_and_rewire(
        "task-parent-history",
        "Continue the parent safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-worker-immutable-history",
    )
    replacement = result["replacement"]
    parent_path = Path(parent["manifest_path"])
    before_bytes = parent_path.read_bytes()
    before_mtime = parent_path.stat().st_mtime_ns
    worker = CDPAWorker(config, store=store)
    advanced: list[str] = []
    maintenance_batches: list[list[str]] = []

    async def no_browser_advance(path, _browser_context, *, scheduling_tasks=None):
        state = store.load(path)
        advanced.append(state["task_id"])
        assert scheduling_tasks is not None
        assert {item["task_id"] for item in scheduling_tasks} == {
            "task-parent-history",
            "task-child-history",
            replacement["task_id"],
        }
        return state

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            maintenance_batches.append([state["task_id"] for _path, state in tasks])
            return False

    monkeypatch.setattr(worker, "advance", no_browser_advance)
    worker.maintainers = FakeCoordinator()

    first = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    second = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    expected_due = {replacement["task_id"]}
    assert {item["task_id"] for item in first if item} == expected_due
    assert second == []
    assert "task-child-history" not in advanced
    assert advanced.count("task-parent-history") == 0
    assert all("task-parent-history" not in batch for batch in maintenance_batches)
    assert parent_path.read_bytes() == before_bytes
    assert parent_path.stat().st_mtime_ns == before_mtime
    assert capsys.readouterr().err == ""
    assert store.load(child["manifest_path"])["depends_on_task_ids"] == [
        replacement["task_id"]
    ]

    tasks = store.discover()
    payload = build_task_projection(store.load(parent_path), tasks=tasks).detail
    assert payload["status"] == "STOPPED"
    assert payload["immutable_history"] is True
    assert payload["controls"] == []
    assert payload["replacement_task_id"] == replacement["task_id"]


def test_replacement_plan_first_prompt_contains_immutable_parent_context(tmp_path: Path):
    import hashlib

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
    result = store.replace_task_and_rewire(
        parent["task_id"],
        "Continue the original requested outcome safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-context-preservation",
    )
    replacement_path = Path(result["replacement"]["manifest_path"])
    restarted_store = TaskStore(config)
    replacement = restarted_store.load(replacement_path)
    worker = CDPAWorker(config, store=restarted_store)
    hop = _active_hop(replacement)

    asyncio.run(worker._pre_send(replacement, hop, FakeActions()))

    prompt = str(hop["prompt"])
    assert "task-parent-context" in prompt
    assert unique_goal in prompt
    assert str(report_path) in prompt
    assert "Continue the original requested outcome safely" in prompt
    assert hop["handoff"] == replacement["task_text"]


def test_worker_restores_sent_upload_after_source_changes_before_manifest_persist(
    tmp_path: Path,
    monkeypatch,
):
    from playwright_auto.upload import UploadReceipt, collect_file_identities
    from test_durable import FakeDurableClient, snapshot as durable_snapshot

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "context.txt"
    attachment.write_text("original", encoding="utf-8")
    state = store.create_task(
        "Recover accepted upload",
        requested_team="alpha",
        task_id="task-upload-sent-restart",
        upload_paths=[attachment],
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: FakeActions(),
    )
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    hop = _active_hop(prepared)
    role = prepared["roles"]["PLAN"]["physical_role"]
    binding = PageBinding("page-alpha-plan", role)
    identities = collect_file_identities([attachment])
    constructor = config.constructor_paths["PLAN"].read_text(encoding="utf-8")
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=role,
        prompt=hop["prompt"],
        source_context={
            "task_id": prepared["task_id"],
            "team": prepared["team"],
            "hop_id": hop["hop_id"],
            "manifest": prepared["manifest_path"],
        },
        role_prompt_hash=worker_module._sha(constructor),
        files=identities,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    upload_receipt = UploadReceipt(
        request_marker=record.rendered_prompt,
        method="input",
        files=identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    record = ledger.update(
        record.request_id,
        status=RequestStatus.UPLOAD_READY,
        upload_receipt=upload_receipt.to_dict(),
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    record = ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=binding,
        baseline=baseline,
        session_id_before="session-1",
    )
    send_receipt = SendReceipt(
        prompt=record.rendered_prompt,
        prompt_sha256=prompt_digest(record.rendered_prompt),
        binding=binding,
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="session-1",
        user_message_id="accepted-user",
        user_turn_id="accepted-turn",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=datetime.now(timezone.utc).timestamp(),
        receipt=send_receipt.to_dict(),
        binding=binding,
        baseline=baseline,
        session_id_before="session-1",
    )
    attachment.write_text("changed after accepted send", encoding="utf-8")
    client = FakeDurableClient(
        durable_snapshot(
            state=ChatGPTState.SUBMITTING,
            task_id=prepared["task_id"],
            team=prepared["team"],
        )
    )
    client.binding = binding

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id=binding.page_id,
                url="https://chatgpt.com/c/upload-sent-restart",
                created=False,
                new_chat=False,
            )

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: Actions(),
    )

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    restored = _active_hop(result)
    assert result["status"] == "RUNNING"
    assert result["block_code"] is None
    assert restored["state"] == "sent"
    assert restored["receipt"]["user_message_id"] == "accepted-user"
    assert result["roles"]["PLAN"]["attachments_uploaded_generation"] == 0
    assert client.upload_calls == []
    assert client.send_calls == []
    assert ledger.get(record.request_id).status is RequestStatus.SENT


def test_upload_mutated_after_worker_preflight_blocks_before_ledger_or_browser_mutation(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "context.txt"
    attachment.write_text("original", encoding="utf-8")
    state = store.create_task(
        "Analyze context",
        requested_team="alpha",
        task_id="task-upload-toctou",
        upload_paths=[attachment],
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: FakeActions(),
    )
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    prepared_hop = _active_hop(prepared)
    original_request_id = prepared_hop["request_id"]
    assert prepared_hop["state"] == "sending"

    original_preflight = worker._attachment_files_for_generation
    mutated = False

    def mutate_after_preflight(current, role):
        nonlocal mutated
        files = original_preflight(current, role)
        if files and not mutated:
            attachment.write_text("changed after worker preflight", encoding="utf-8")
            mutated = True
        return files

    monkeypatch.setattr(worker, "_attachment_files_for_generation", mutate_after_preflight)
    client = SimpleNamespace(
        binding=PageBinding("page-alpha-plan", "alpha-plan")
    )

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload-toctou",
                created=False,
                new_chat=False,
            )

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: Actions(),
    )

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    hop = _active_hop(result)
    assert mutated is True
    assert result["status"] == "BLOCKED"
    assert result["block_code"] == "attachment_identity_changed"
    assert "context.txt" in result["block_reason"]
    assert str(attachment.resolve()) not in result["block_reason"]
    assert hop["state"] == "sending"
    assert hop["request_id"] == original_request_id
    assert not Path(hop["ledger_path"]).exists()
    first = canonical_recovery_events(result)
    second = canonical_recovery_events(result)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("attachment_identity_changed:")


def test_changed_upload_file_blocks_without_legacy_maintenance_state(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "context.txt"
    attachment.write_text("original", encoding="utf-8")
    state = store.create_task(
        "Analyze context",
        requested_team="alpha",
        task_id="task-upload-changed",
        upload_paths=[attachment],
    )
    attachment.write_text("changed", encoding="utf-8")
    worker = CDPAWorker(config, store=store)

    class NoAcquire(FakeActions):
        def __init__(self):
            super().__init__()
            self.acquire_calls = 0

        async def acquire(self, state, role):
            self.acquire_calls += 1
            return await super().acquire(state, role)

    actions = NoAcquire()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(state["manifest_path"], SimpleNamespace(pages=[])))

    assert result == store.load(state["manifest_path"])
    assert result["status"] == "BLOCKED"
    assert result["block_code"] == "attachment_identity_changed"
    assert "context.txt" in result["block_reason"]
    assert str(attachment.resolve()) not in result["block_reason"]
    assert actions.acquire_calls == 0
    assert result.get("maintenance") in (None, {})
    assert not hasattr(worker, "maintainers")

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


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", "attachment_missing"),
        ("directory", "attachment_validation_failed"),
    ],
)
def test_attachment_preflight_rejects_missing_or_non_file_without_browser_mutation(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
    expected_code: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "evidence.txt"
    attachment.write_text("stable", encoding="utf-8")
    state = store.create_task(
        "Use evidence",
        requested_team="alpha",
        task_id=f"task-upload-{mutation}",
        upload_paths=[attachment],
    )
    attachment.unlink()
    if mutation == "directory":
        attachment.mkdir()
    worker = CDPAWorker(config, store=store)

    class NoAcquire(FakeActions):
        def __init__(self):
            super().__init__()
            self.acquire_calls = 0

        async def acquire(self, state, role):
            self.acquire_calls += 1
            return await super().acquire(state, role)

    actions = NoAcquire()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    result = asyncio.run(worker.advance(state["manifest_path"], SimpleNamespace(pages=[])))

    assert result["status"] == "BLOCKED"
    assert result["block_code"] == expected_code
    assert "evidence.txt" in result["block_reason"]
    assert str(tmp_path) not in result["block_reason"]
    assert actions.acquire_calls == 0
    assert _active_hop(result)["state"] == "pre_send"


def test_attachment_upload_failure_is_sanitized_and_creates_one_maintainers_incident(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "private-context.txt"
    attachment.write_text("stable", encoding="utf-8")
    state = store.create_task(
        "Upload evidence",
        requested_team="alpha",
        task_id="task-upload-failure",
        upload_paths=[attachment],
    )
    worker = CDPAWorker(config, store=store)
    path = Path(state["manifest_path"])
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: FakeActions())
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    assert _active_hop(prepared)["state"] == "sending"

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/upload-failure",
                url="https://chatgpt.com/c/upload-failure",
            )

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=Client(),
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload-failure",
                created=False,
                new_chat=False,
            )

    class FailingBlock:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _context):
            raise RuntimeError(f"unsafe raw path {attachment.resolve()}")

    monkeypatch.setattr(worker_module, "DurableSendBlock", FailingBlock)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result == store.load(path)
    assert result["status"] == "BLOCKED"
    assert result["block_code"] == "attachment_upload_failed"
    assert "private-context.txt" in result["block_reason"]
    assert str(attachment.resolve()) not in result["block_reason"]
    assert _active_hop(result)["state"] == "sending"
    first = canonical_recovery_events(result)
    second = canonical_recovery_events(result)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("attachment_upload_failed:")


def test_attachment_generation_marker_persists_after_normal_advance_and_restart(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "context.txt"
    attachment.write_text("stable", encoding="utf-8")
    state = store.create_task(
        "Upload once",
        requested_team="alpha",
        task_id="task-upload-persisted-marker",
        upload_paths=[attachment],
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)
    send_calls = 0
    captured_files = []

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/upload-marker",
                url="https://chatgpt.com/c/upload-marker",
            )

    client = Client()

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload-marker",
                created=False,
                new_chat=False,
            )

    class AcceptedBlock:
        def __init__(self, _prompt, *, files=(), **_kwargs):
            captured_files.append(tuple(str(item) for item in files))

        async def run(self, _context):
            nonlocal send_calls
            send_calls += 1
            return {
                "receipt": {"prompt_sha256": "a" * 64},
                "record": {"accepted_at": datetime.now(timezone.utc).timestamp()},
            }

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: FakeActions())
    first = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    assert _active_hop(first)["state"] == "sending"

    monkeypatch.setattr(worker_module, "DurableSendBlock", AcceptedBlock)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())
    sent = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert sent == store.load(path)
    assert _active_hop(sent)["state"] == "sent"
    assert sent["roles"]["PLAN"]["attachments_uploaded_generation"] == 0
    assert captured_files == [(str(attachment.resolve()),)]
    assert send_calls == 1

    restarted = CDPAWorker(config, store=TaskStore(config))
    waiting = asyncio.run(restarted.advance(path, SimpleNamespace(pages=[])))
    assert _active_hop(waiting)["state"] == "waiting"
    assert waiting["roles"]["PLAN"]["attachments_uploaded_generation"] == 0
    assert send_calls == 1


@pytest.mark.parametrize("visible_name", ["manual-unowned.txt", "expected-context.txt"])
def test_normal_worker_rejects_prompt_set_unowned_attachment_before_send(
    tmp_path: Path,
    monkeypatch,
    visible_name: str,
):
    import hashlib

    from playwright_auto.upload import collect_file_identities
    from test_durable import FakeDurableClient, snapshot as durable_snapshot

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "expected-context.txt"
    attachment.write_text("expected context", encoding="utf-8")
    state = store.create_task(
        "Use expected context",
        requested_team="alpha",
        task_id=f"task-upload-manual-{visible_name.replace('.', '-')}",
        upload_paths=[attachment],
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: FakeActions(),
    )
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    hop = _active_hop(prepared)
    assert hop["state"] == "sending"

    identities = collect_file_identities([attachment])
    constructor = config.constructor_paths["PLAN"].read_text(encoding="utf-8")
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=prepared["roles"]["PLAN"]["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": prepared["task_id"],
            "team": prepared["team"],
            "hop_id": hop["hop_id"],
            "manifest": prepared["manifest_path"],
        },
        role_prompt_hash=hashlib.sha256(constructor.encode("utf-8")).hexdigest(),
        files=identities,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)

    client = FakeDurableClient(
        durable_snapshot(
            text=record.rendered_prompt,
            attachments=(visible_name,),
            state=ChatGPTState.DRAFT,
        )
    )
    client.binding = PageBinding(
        "page-alpha-plan",
        prepared["roles"]["PLAN"]["physical_role"],
    )

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload-manual",
                created=False,
                new_chat=False,
            )

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: Actions(),
    )

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result == store.load(path)
    assert result["status"] == "BLOCKED"
    assert result["block_code"] == "attachment_upload_failed"
    assert str(attachment.resolve()) not in result["block_reason"]
    assert client.upload_calls == []
    assert client.send_calls == []
    assert result["roles"]["PLAN"]["attachments_uploaded_generation"] is None
    persisted = ledger.get(record.request_id)
    assert persisted is not None
    assert persisted.status is RequestStatus.PROMPT_SET
    assert persisted.upload_receipt is None
    first = canonical_recovery_events(result)
    second = canonical_recovery_events(result)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("attachment_upload_failed:")


@pytest.mark.parametrize(
    "race_kind",
    [
        "attachment",
        "attachment_instance",
        "attachment_dispatch",
        "prompt",
        "ownership",
        "page_state",
    ],
)
def test_normal_worker_blocks_locked_send_boundary_conflict(
    tmp_path: Path,
    monkeypatch,
    race_kind: str,
):
    import hashlib

    from playwright_auto.upload import UploadReceipt, collect_file_identities
    from test_durable import FakeDurableClient, snapshot as durable_snapshot

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    state = store.create_task(
        "Use durable context",
        requested_team="alpha",
        task_id="task-upload-locked-race",
        upload_paths=[attachment],
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: FakeActions(),
    )
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    hop = _active_hop(prepared)
    assert hop["state"] == "sending"

    identities = collect_file_identities([attachment])
    constructor = config.constructor_paths["PLAN"].read_text(encoding="utf-8")
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=prepared["roles"]["PLAN"]["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": prepared["task_id"],
            "team": prepared["team"],
            "hop_id": hop["hop_id"],
            "manifest": prepared["manifest_path"],
        },
        role_prompt_hash=hashlib.sha256(constructor.encode("utf-8")).hexdigest(),
        files=identities,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    upload_receipt = UploadReceipt(
        request_marker=record.rendered_prompt,
        method="input",
        files=identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.UPLOAD_READY,
        upload_receipt=upload_receipt.to_dict(),
    )

    class BoundaryRaceClient(FakeDurableClient):
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
            if race_kind in {
                "attachment_instance",
                "attachment_dispatch",
            }:
                assert expected_attachment_ownership_token == "fake-upload-token"
                raise ComposerConflictError(
                    "attachment ownership changed in locked send"
                )
            if race_kind == "prompt":
                self.current = durable_snapshot(
                    text="manual changed prompt",
                    attachments=self.current.attachment_markers,
                    messages=self.current.messages,
                    state=ChatGPTState.DRAFT,
                )
                raise ComposerConflictError("composer text changed in locked send")
            if race_kind == "ownership":
                raise PageOwnershipError("conversation changed in locked send")
            if race_kind == "page_state":
                raise UnsafePageStateError("blocking dialog appeared in locked send")
            self.current = durable_snapshot(
                text=self.current.composer_text,
                attachments=("manual-unowned.txt",),
                messages=self.current.messages,
                state=ChatGPTState.DRAFT,
            )
            if expected_attachment_names is not None and tuple(
                self.current.attachment_markers
            ) != tuple(expected_attachment_names):
                raise ComposerConflictError("attachment identity changed in locked send")
            return await super().send(
                text,
                wait_for_stop=wait_for_stop,
                max_attempts=max_attempts,
                recovery_reload=recovery_reload,
                expected_task_id=expected_task_id,
                expected_team=expected_team,
                expected_attachment_ownership_token=expected_attachment_ownership_token,
                expected_attachment_count=expected_attachment_count,
                expected_attachment_names=expected_attachment_names,
            )

    client = BoundaryRaceClient(
        durable_snapshot(
            text=record.rendered_prompt,
            attachments=("context.txt",),
            state=ChatGPTState.DRAFT,
            task_id=prepared["task_id"],
            team=prepared["team"],
        )
    )
    client.binding = PageBinding(
        "page-alpha-plan",
        prepared["roles"]["PLAN"]["physical_role"],
    )

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload-race",
                created=False,
                new_chat=False,
            )

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: Actions(),
    )

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result == store.load(path)
    assert result["status"] == "BLOCKED"
    assert result["block_code"] == "attachment_upload_failed"
    assert client.send_calls == []
    assert result["roles"]["PLAN"]["attachments_uploaded_generation"] is None
    persisted = ledger.get(record.request_id)
    assert persisted is not None
    assert persisted.status is RequestStatus.SENDING
    assert persisted.receipt is None
    first = canonical_recovery_events(result)
    second = canonical_recovery_events(result)
    assert len(first) == len(second) == 1
    assert first[0]["event_key"] == second[0]["event_key"]
    assert first[0]["failure_signature"].startswith("attachment_upload_failed:")


def test_normal_worker_valid_multi_attachment_sends_once_and_persists_generation(
    tmp_path: Path,
    monkeypatch,
):
    from playwright_auto.upload import UploadReceipt
    from test_durable import FakeDurableClient, snapshot as durable_snapshot

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    context_file = tmp_path / "context.txt"
    logs_file = tmp_path / "logs.txt"
    context_file.write_text("context", encoding="utf-8")
    logs_file.write_text("logs", encoding="utf-8")
    state = store.create_task(
        "Use canonical context and logs",
        requested_team="alpha",
        task_id="task-upload-canonical-multi",
        upload_paths=[context_file, logs_file],
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: FakeActions(),
    )
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    hop = _active_hop(prepared)
    assert hop["state"] == "sending"

    class CanonicalClient(FakeDurableClient):
        def __init__(self):
            super().__init__(
                durable_snapshot(
                    task_id=prepared["task_id"],
                    team=prepared["team"],
                )
            )
            self.exact_name_contracts = []

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
            expected_names = tuple(expected_attachment_names or ())
            self.exact_name_contracts.append(expected_names)
            assert tuple(self.current.attachment_markers) == expected_names
            return await super().send(
                text,
                wait_for_stop=wait_for_stop,
                max_attempts=max_attempts,
                recovery_reload=recovery_reload,
                expected_task_id=expected_task_id,
                expected_team=expected_team,
                expected_attachment_ownership_token=expected_attachment_ownership_token,
                expected_attachment_count=expected_attachment_count,
                expected_attachment_names=expected_attachment_names,
            )

    client = CanonicalClient()
    client.binding = PageBinding(
        "page-alpha-plan",
        prepared["roles"]["PLAN"]["physical_role"],
    )

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload-valid",
                created=False,
                new_chat=False,
            )

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: Actions(),
    )

    sent = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert sent == store.load(path)
    assert sent["status"] == "RUNNING"
    assert _active_hop(sent)["state"] == "sent"
    assert len(client.upload_calls) == 1
    assert client.upload_calls[0][0] == (
        str(context_file.resolve()),
        str(logs_file.resolve()),
    )
    assert len(client.send_calls) == 1
    assert client.exact_name_contracts == [("context.txt", "logs.txt")]
    assert sent["roles"]["PLAN"]["attachments_uploaded_generation"] == 0
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.get(str(hop["request_id"]))
    assert record is not None
    assert record.status is RequestStatus.SENT
    assert record.receipt is not None
    assert record.upload_receipt is not None
    receipt = UploadReceipt.from_dict(record.upload_receipt)
    assert tuple(item.name for item in receipt.files) == ("context.txt", "logs.txt")


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


def test_worker_shared_error_boundary_covers_wait_cleanup_and_route_fields(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path,
        task_id="task-worker-error-surfaces",
    )
    secret = "shared-worker-secret-token"
    error = RuntimeError(
        f"POST https://api.example.invalid/webhooks/{secret}/status failed; token={secret}"
    )

    waiting_state = json.loads(json.dumps(state))
    assert worker._wait_queue_error(
        waiting_state,
        error,
        code="queue_release_failed",
    ) is True
    assert secret not in json.dumps(waiting_state, ensure_ascii=False)

    cleanup_state = json.loads(json.dumps(state))
    cleanup_state["cleanup"] = {
        "state": "CLEARING",
        "phase": "closing",
        "control_id": None,
    }
    worker._record_cleanup_failure(cleanup_state, error)
    assert secret not in json.dumps(cleanup_state["cleanup"], ensure_ascii=False)

    route_state = json.loads(json.dumps(state))
    hop = _active_hop(route_state)
    worker._repair_route(route_state, hop, error)
    assert secret not in json.dumps(
        {
            "validation_error": hop.get("validation_error"),
            "route_timeline": route_state.get("route_timeline"),
            "new_hop": _active_hop(route_state),
        },
        ensure_ascii=False,
    )


def test_refresh_failure_error_is_sanitized_before_transport_persistence(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-refresh-credential-boundary",
    )
    old = sent_at - timedelta(minutes=30)
    secret = "refresh-path-secret-token"
    sensitive_url = f"https://api.example.invalid/webhooks/{secret}/status"
    snapshot = SimpleNamespace(
        state=ChatGPTState.ERROR,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=("Message delivery timed out. Please try again.",),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise TimeoutError("continue polling")

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            raise RuntimeError(
                f"refresh {sensitive_url} failed; Authorization: Bearer {secret}"
            )

    with pytest.raises(RuntimeError, match="refresh-path-secret-token"):
        asyncio.run(worker._waiting(state, hop, Actions(), path))

    persisted = store.load(path)
    error = _active_hop(persisted)["wait"]["last_refresh_result"]["error"]
    assert secret not in error
    assert "[REDACTED]" in error
    assert secret not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path_template",
    (
        "/webhooks/incoming/{secret}/status",
        "/oauth/callback/{secret}/complete",
        "/password-reset/confirm/{secret}",
        "/capability/v1/{secret}/run",
        "/magic-link/callback/{secret}",
        "/password-reset-link/{secret}",
        "/signed-url/{secret}/result",
    ),
)
def test_worker_multi_segment_high_risk_paths_never_persist(
    tmp_path: Path,
    path_template: str,
):
    from playwright_auto.cdpa_projection import build_task_projection

    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-worker-multi-segment-path",
    )
    manifest_path = Path(state["manifest_path"])
    secret = "K7p4Q9Lm3Vx8"
    sensitive_url = "https://api.example.invalid" + path_template.format(secret=secret)
    error = RuntimeError(
        f"GET {sensitive_url}?access_token=query-secret#fragment-secret failed; "
        "Authorization: Bearer bearer-secret; password=password-secret"
    )
    requested = store.request_control(
        manifest_path,
        "pause",
        reason="multi-segment credential fixture",
    )
    control_id = requested["controls"][-1]["control_id"]

    saved = store.update(
        manifest_path,
        lambda current: worker._block(
            current,
            error,
            code="role_offline",
            retryable=False,
        ),
    )
    saved = store.reject_control(
        manifest_path,
        control_id,
        f"{type(error).__name__}: {error}",
        action="pause",
    )
    payload = build_task_projection(saved, tasks=[saved]).detail

    assert secret not in manifest_path.read_text(encoding="utf-8")
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert secret not in str(saved["block_reason"])
    assert all(secret not in str(item) for item in saved["errors"])
    assert all(secret not in str(item) for item in _active_hop(saved)["errors"])
    assert secret not in str(saved["controls"][-1]["result"])
    assert all(secret not in item["message"] for item in payload["timeline"])


@pytest.mark.parametrize(
    "path_template",
    (
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
    ),
)
def test_worker_direct_marker_value_segment_never_persists(
    tmp_path: Path,
    path_template: str,
):
    from playwright_auto.cdpa_projection import build_task_projection

    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-worker-direct-marker-value",
    )
    manifest_path = Path(state["manifest_path"])
    secret = "K7p4Q9Lm3Vx8"
    sensitive_url = "https://api.example.invalid" + path_template.format(secret=secret)
    error = RuntimeError(
        f"GET {sensitive_url}?access_token=query-secret#fragment-secret failed; "
        "Authorization: Bearer bearer-secret; password=password-secret"
    )
    requested = store.request_control(
        manifest_path,
        "pause",
        reason="direct marker value fixture",
    )
    control_id = requested["controls"][-1]["control_id"]

    saved = store.update(
        manifest_path,
        lambda current: worker._block(
            current,
            error,
            code="role_offline",
            retryable=False,
        ),
    )
    saved = store.reject_control(
        manifest_path,
        control_id,
        f"{type(error).__name__}: {error}",
        action="pause",
    )
    payload = build_task_projection(saved, tasks=[saved]).detail

    assert secret not in manifest_path.read_text(encoding="utf-8")
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert secret not in str(saved["block_reason"])
    assert secret not in str(saved["roles"][saved["active_role"]]["last_error"])
    assert all(secret not in str(item) for item in saved["errors"])
    assert all(secret not in str(item) for item in _active_hop(saved)["errors"])
    assert secret not in str(saved["controls"][-1]["result"])
    assert all(secret not in item["message"] for item in payload["timeline"])


@pytest.mark.parametrize(
    "path_template",
    (
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
    ),
)
def test_worker_canonicalized_high_risk_route_never_persists(
    tmp_path: Path,
    path_template: str,
):
    from playwright_auto.cdpa_projection import build_task_projection

    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-worker-canonicalized-route",
    )
    manifest_path = Path(state["manifest_path"])
    secret = "K7p4Q9Lm3Vx8"
    sensitive_url = "https://api.example.invalid" + path_template.format(secret=secret)
    error = RuntimeError(
        f"GET {sensitive_url}?access_token=query-secret#fragment-secret failed; "
        "Authorization: Bearer bearer-secret; password=password-secret"
    )
    requested = store.request_control(
        manifest_path,
        "pause",
        reason="canonicalized route fixture",
    )
    control_id = requested["controls"][-1]["control_id"]

    saved = store.update(
        manifest_path,
        lambda current: worker._block(
            current,
            error,
            code="role_offline",
            retryable=False,
        ),
    )
    saved = store.reject_control(
        manifest_path,
        control_id,
        f"{type(error).__name__}: {error}",
        action="pause",
    )
    payload = build_task_projection(saved, tasks=[saved]).detail

    assert secret not in manifest_path.read_text(encoding="utf-8")
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert secret not in str(saved["block_reason"])
    assert secret not in str(saved["roles"][saved["active_role"]]["last_error"])
    assert all(secret not in str(item) for item in saved["errors"])
    assert all(secret not in str(item) for item in _active_hop(saved)["errors"])
    assert secret not in str(saved["controls"][-1]["result"])
    assert all(secret not in item["message"] for item in payload["timeline"])


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


def test_runtime_worker_does_not_advance_idle_terminal_or_blocked_tasks(tmp_path: Path, monkeypatch):
    _config, store, running, worker = setup_task(tmp_path, task_id="task-runtime-running")
    done = store.create_task("done", requested_team="done", task_id="task-runtime-done")
    blocked = store.create_task("blocked", requested_team="blocked", task_id="task-runtime-blocked")
    store.update(
        done["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )
    store.update(
        blocked["manifest_path"],
        lambda state: {
            **state,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "manual",
            "block_reason": "blocked",
        },
    )
    advanced = []

    async def record_advance(path, _browser_context, *, scheduling_tasks=None):
        advanced.append(store.load(path)["task_id"])
        return store.load(path)

    class InertMaintainers:
        async def advance(self, tasks, _browser_context):
            assert {state["task_id"] for _path, state in tasks} == {
                "task-runtime-running",
                "task-runtime-done",
                "task-runtime-blocked",
            }
            return False

    monkeypatch.setattr(worker, "advance", record_advance)
    worker.maintainers = InertMaintainers()

    worker.hydrate_runtime()
    worker.registry._due_at["task-runtime-running"] = 0.0
    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert advanced == ["task-runtime-running"]


def test_worker_browser_inventory_respects_cadence_and_page_count_change(tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(tmp_path, task_id="task-browser-cadence")
    evaluations = []

    class Page:
        def __init__(self, page_id):
            self.page_id = page_id
            self.url = "https://chatgpt.com/c/test"

        def is_closed(self):
            return False

        async def evaluate(self, script):
            evaluations.append((self.page_id, script))
            return {
                "url": self.url,
                "title": "ChatGPT",
                "role": "PLAN",
                "page_id": self.page_id,
                "task_id": "task-browser-cadence",
                "team": "alpha",
            }

    class InertMaintainers:
        async def advance(self, _tasks, _browser_context):
            return False

    async def inert_advance(path, _browser_context, *, scheduling_tasks=None):
        return worker.store.load(path)

    worker.maintainers = InertMaintainers()
    monkeypatch.setattr(worker, "advance", inert_advance)
    context = SimpleNamespace(pages=[Page("one")])

    asyncio.run(worker.run_once(context))
    asyncio.run(worker.run_once(context))
    assert [page_id for page_id, _script in evaluations] == ["one"]

    context.pages.append(Page("two"))
    asyncio.run(worker.run_once(context))
    assert [page_id for page_id, _script in evaluations] == ["one", "one", "two"]
    assert all("data-message-author-role" not in script for _page_id, script in evaluations)


def test_browser_disconnect_publishes_unknown_projection_and_reconnects(tmp_path: Path):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-browser-disconnect-projection"
    )
    state["roles"]["PLAN"].update(page_id="page-one", online=True)
    state = store.save(state["manifest_path"], state)
    worker.hydrate_runtime()

    class Page:
        url = "https://chatgpt.com/c/test"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return {
                "url": self.url,
                "title": "ChatGPT",
                "role": "PLAN",
                "page_id": "page-one",
                "task_id": state["task_id"],
                "team": state["team"],
            }

    context = SimpleNamespace(pages=[Page()])
    asyncio.run(worker._publish_browser_inventory(context))
    connected = worker.runtime_db.get_snapshot("browser")["payload"]
    connected_detail = worker.runtime_db.get_task_detail(state["task_id"])
    assert connected["connected"] is True
    connected_plan = next(
        role for role in connected_detail["roles"] if role["logical_role"] == "PLAN"
    )
    assert connected_detail["availability"] == "online"
    assert connected_plan["online"] is True

    worker._publish_browser_disconnected()
    disconnected = worker.runtime_db.get_snapshot("browser")["payload"]
    disconnected_detail = worker.runtime_db.get_task_detail(state["task_id"])
    assert disconnected["connected"] is False
    assert disconnected["page_count"] is None
    assert disconnected["pages"] == []
    assert disconnected["observed_at"]
    disconnected_plan = next(
        role for role in disconnected_detail["roles"] if role["logical_role"] == "PLAN"
    )
    assert disconnected_detail["availability"] == "unknown"
    assert disconnected_plan["online"] is None

    asyncio.run(worker._publish_browser_inventory(context))
    reconnected = worker.runtime_db.get_snapshot("browser")["payload"]
    reconnected_detail = worker.runtime_db.get_task_detail(state["task_id"])
    assert reconnected["connected"] is True
    assert reconnected["page_count"] == 1
    reconnected_plan = next(
        role for role in reconnected_detail["roles"] if role["logical_role"] == "PLAN"
    )
    assert reconnected_detail["availability"] == "online"
    assert reconnected_plan["online"] is True


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
        },
    )

    result = worker._apply_next_command()

    assert result["status"] == "applied"
    created = store.load_task_id("task-created-command")
    assert created is not None
    assert created["applied_command_ids"] == ["cmd-create-once"]
    assert worker.runtime_db.get_task_detail("task-created-command")["task_id"] == "task-created-command"


def test_runtime_create_command_replay_uses_manifest_provenance(tmp_path: Path):
    _config, store, _state, worker = setup_task(tmp_path, task_id="task-existing-replay")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-replay",
        idempotency_key="create-replay",
        kind="create_task",
        task_id="task-created-replay",
        expected_task_version=None,
        payload={
            "task": "Replay-safe create",
            "requested_team": "replay",
            "repository": str(tmp_path),
        },
    )
    assert worker.runtime_db.claim_next_command()["status"] == "running"
    first = store.create_task(
        "Replay-safe create",
        requested_team="replay",
        repository=tmp_path,
        task_id="task-created-replay",
        external_command_id="cmd-create-replay",
    )
    worker.runtime_db.requeue_running_commands()

    result = worker._apply_next_command()

    assert result["status"] == "applied"
    assert store.load_task_id("task-created-replay")["manifest_path"] == first["manifest_path"]
    assert len([item for item in worker.registry.tasks_by_id if item == "task-created-replay"]) == 1


def test_degraded_dispatch_runs_reload_and_rejects_other_commands(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-degraded-dispatch")
    worker.hydrate_runtime()
    worker.runtime_degraded = True
    worker.registry = None
    worker.runtime_db.enqueue_command(
        command_id="cmd-degraded-create",
        idempotency_key="degraded-create",
        kind="create_task",
        task_id="task-should-not-create",
        expected_task_version=None,
        payload={
            "task": "must be rejected while degraded",
            "requested_team": "degraded",
            "repository": str(tmp_path),
            "report_mode": "file",
            "depends_on_task_ids": [],
            "upload_paths": [],
            "reuse_team": False,
        },
    )

    rejected = worker.dispatch_command_once()

    assert rejected["status"] == "failed"
    assert "catalog is degraded" in rejected["error"]
    assert store.load_task_id("task-should-not-create") is None

    worker.runtime_db.enqueue_command(
        command_id="cmd-degraded-reload",
        idempotency_key="degraded-reload",
        kind="reload_catalog",
        task_id=None,
        expected_task_version=None,
        payload={},
    )

    recovered = worker.dispatch_command_once()

    assert recovered["status"] == "applied"
    assert worker.runtime_degraded is False
    assert worker.registry is not None
    assert "task-degraded-dispatch" in worker.registry.tasks_by_id


def test_browser_cycle_does_not_head_of_line_block_state_commands(tmp_path: Path):
    _config, store, _state, worker = setup_task(tmp_path, task_id="task-browser-cycle")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-reload-behind-browser",
        idempotency_key="reload-behind-browser",
        kind="reload_catalog",
        task_id=None,
        expected_task_version=None,
        payload={},
    )
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-during-browser",
        idempotency_key="create-during-browser",
        kind="create_task",
        task_id="task-created-during-browser",
        expected_task_version=None,
        payload={
            "task": "create while browser cycle is active",
            "requested_team": "parallel",
            "repository": str(tmp_path),
            "report_mode": "file",
            "depends_on_task_ids": [],
            "upload_paths": [],
            "reuse_team": False,
        },
    )
    worker._browser_cycle_active = True

    applied = worker.dispatch_command_once()

    assert applied["command_id"] == "cmd-create-during-browser"
    assert applied["status"] == "applied", applied
    assert store.load_task_id("task-created-during-browser") is not None
    assert worker.runtime_db.get_command("cmd-reload-behind-browser")["status"] == "queued"

    worker._browser_cycle_active = False
    reloaded = worker.dispatch_command_once()
    assert reloaded["command_id"] == "cmd-reload-behind-browser"
    assert reloaded["status"] == "applied"


def test_independent_recovery_claim_is_idempotent_without_coordinator_cooldown(
    tmp_path: Path,
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-maintainers-cooldown"
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
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    worker.hydrate_runtime()

    first = worker._activate_independent_agents()
    claimed = store.load(standby["manifest_path"])
    second = worker._activate_independent_agents()
    replay = store.load(standby["manifest_path"])

    assert first == {standby["task_id"]}
    assert second == set()
    assert claimed["independent"]["active_event"] == replay["independent"]["active_event"]
    assert not hasattr(worker, "_maintainers_next_due_at")

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
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    worker.hydrate_runtime()

    worker._activate_independent_agents()

    assert worker.registry.tasks_by_id[standby["task_id"]]["status"] == "RUNNING"
    projected = worker.runtime_db.get_task_detail(standby["task_id"])
    assert projected["status"] == "RUNNING"
    assert projected["column"] == "INDEPENDENT_AGENTS"
    assert projected["agent"]["target_task_id"] == state["task_id"]

def test_runtime_reload_command_requeues_and_applies_after_interruption(tmp_path: Path):
    _config, _store, _state, worker = setup_task(tmp_path, task_id="task-reload-replay")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-reload-replay",
        idempotency_key="reload-replay",
        kind="reload_catalog",
        task_id=None,
        expected_task_version=None,
        payload={},
    )
    assert worker.runtime_db.claim_next_command()["status"] == "running"
    worker.runtime_db.requeue_running_commands()

    result = worker._apply_next_command()

    assert result["status"] == "applied"
    assert result["result"]["catalog"]["complete"] is True


def test_coalesced_resume_commands_keep_exact_replay_provenance(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-resume-coalesced")
    blocked = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "manual",
            "block_reason": "blocked",
        },
    )
    first = store.resume_team(
        blocked["team"],
        reason="first resume",
        external_command_id="cmd-resume-first",
    )
    second = store.resume_team(
        blocked["team"],
        reason="second resume",
        external_command_id="cmd-resume-second",
    )
    assert len(second["controls"]) == len(first["controls"]) == 1
    provenance = second["controls"][0]["external_commands"]
    assert provenance == [
        {"command_id": "cmd-resume-first", "reason": "first resume"},
        {"command_id": "cmd-resume-second", "reason": "second resume"},
    ]

    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-resume-second",
        idempotency_key="resume-second",
        kind="resume_team",
        task_id=None,
        expected_task_version=None,
        payload={"team": blocked["team"], "reason": "second resume"},
    )

    result = worker._apply_next_command()

    assert result["status"] == "running"
    assert result["result"] is None
    saved = store.load(state["manifest_path"])
    assert len(saved["controls"]) == 1
    assert saved["controls"][0]["status"] == "requested"


def test_runtime_control_command_replay_reconciles_before_stale_version(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-control-replay")
    worker.hydrate_runtime()
    version = worker.runtime_db.get_task_version("task-control-replay")
    worker.runtime_db.enqueue_command(
        command_id="cmd-control-replay",
        idempotency_key="control-replay",
        kind="task_control",
        task_id="task-control-replay",
        expected_task_version=version,
        payload={"action": "pause", "reason": "operator pause", "confirmed": False},
    )
    assert worker.runtime_db.claim_next_command()["status"] == "running"
    mutated = store.request_control(
        state["manifest_path"],
        "pause",
        reason="operator pause",
        external_command_id="cmd-control-replay",
    )
    assert mutated["applied_command_ids"][-1] == "cmd-control-replay"
    worker.runtime_db.requeue_running_commands()

    result = worker._apply_next_command()

    assert result["status"] == "applied"
    assert result["result"]["reconciled"] is True
    controls = store.load(state["manifest_path"])["controls"]
    assert [item["external_command_id"] for item in controls] == ["cmd-control-replay"]


def test_runtime_resume_command_replay_reconciles_exact_control(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-resume-replay")
    blocked = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "manual",
            "block_reason": "blocked",
        },
    )
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-resume-replay",
        idempotency_key="resume-replay",
        kind="resume_team",
        task_id=None,
        expected_task_version=None,
        payload={"team": blocked["team"], "reason": "resume exact team"},
    )
    assert worker.runtime_db.claim_next_command()["status"] == "running"
    store.resume_team(
        blocked["team"],
        reason="resume exact team",
        external_command_id="cmd-resume-replay",
    )
    worker.runtime_db.requeue_running_commands()

    result = worker._apply_next_command()

    assert result["status"] == "running"
    assert result["result"] is None
    saved = store.load(state["manifest_path"])
    assert saved["controls"][-1]["external_command_id"] == "cmd-resume-replay"
    assert saved["controls"][-1]["status"] == "requested"


def test_runtime_ambiguous_command_provenance_requires_recovery(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-recovery-required")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-recovery-required",
        idempotency_key="recovery-required",
        kind="task_control",
        task_id="task-recovery-required",
        expected_task_version=worker.runtime_db.get_task_version("task-recovery-required"),
        payload={"action": "pause", "confirmed": False},
    )
    assert worker.runtime_db.claim_next_command()["status"] == "running"
    store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "applied_command_ids": [
                *current.get("applied_command_ids", []),
                "cmd-recovery-required",
            ],
        },
    )
    worker.runtime_db.requeue_running_commands()

    result = worker._apply_next_command()

    assert result["status"] == "recovery_required"
    assert "exact control record" in result["error"]


def test_runtime_command_stale_version_fails_without_mutation(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-stale-command")
    worker.hydrate_runtime()
    before = store.load(state["manifest_path"])
    worker.runtime_db.enqueue_command(
        command_id="cmd-stale",
        idempotency_key="stale",
        kind="task_control",
        task_id="task-stale-command",
        expected_task_version=99,
        payload={"action": "pause"},
    )

    result = worker._apply_next_command()

    assert result["status"] == "failed"
    assert "stale task version" in result["error"]
    after = store.load(state["manifest_path"])
    assert after["controls"] == before["controls"]
    assert "cmd-stale" not in after.get("applied_command_ids", [])


def test_active_presend_offline_reopens_exact_conversation_before_block(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-auto-recover-presend"
    )
    hop = _active_hop(state)
    original_request_id = hop["request_id"]
    exact_url = "https://chatgpt.com/c/presend-exact"
    hop["conversation_url"] = exact_url
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
            self.reopen_clean = []

        async def acquire(self, *_args, **_kwargs):
            raise RoleOwnershipError("recorded role tab is offline", code="role_offline")

        async def reopen(self, *_args, require_clean_ready=True, **_kwargs):
            self.reopen_clean.append(require_clean_ready)
            return acquired

    actions = Actions()
    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.reopen_clean == [True]
    assert hop["state"] == "sending"
    assert hop["request_id"] == original_request_id
    assert state["status"] == "RUNNING"
    assert state.get("maintenance") in (None, {})


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
            self.reopen_clean = []

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, require_clean_ready=True, **_kwargs):
            self.reopen_clean.append(require_clean_ready)
            return acquired

    actions = Actions()
    result = asyncio.run(worker._owned_or_block(state, "PLAN", actions))

    assert result == acquired
    assert actions.reopen_clean == [False]
    assert hop["state"] == "waiting"
    assert hop["request_id"] == original_request_id
    assert hop["receipt"] == receipt
    assert state["status"] != "BLOCKED"
    assert state.get("maintenance") in (None, {})


def test_unresolved_active_role_recovery_emits_one_canonical_event(
    tmp_path: Path,
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-atomic-role-incident"
    )
    hop = _active_hop(state)
    exact_url = "https://chatgpt.com/c/unresolved-exact"
    hop["conversation_url"] = exact_url
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url=exact_url,
        online=False,
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, **_kwargs):
            raise RoleOwnershipError(
                "recorded exact conversation is unavailable",
                code="role_offline",
            )

    actions = Actions()
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] == "BLOCKED"
    assert state.get("maintenance") in (None, {})
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    first = canonical_independent_events(standby, [state, standby])

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    second = canonical_independent_events(standby, [state, standby])
    assert len(first) == 1
    assert [item["event_key"] for item in first] == [item["event_key"] for item in second]
    assert first[0]["trigger_type"] == "recovery"
    assert first[0]["target_task_id"] == state["task_id"]

def test_waiting_promotes_live_exact_url_before_later_offline_recovery(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-promote-live-conversation"
    )
    hop = _active_hop(state)
    exact_url = "https://chatgpt.com/c/promoted-exact"
    hop.update(
        state="waiting",
        conversation_url="https://chatgpt.com/c/WEB:temporary-id",
        receipt={
            "binding": {"page_id": "accepted-page", "role": "alpha-plan"},
            "accepted_via": "user_message_identity",
            "user_message_id": "accepted-user",
            "user_turn_id": "accepted-turn",
        },
    )
    state["roles"]["PLAN"].update(
        page_id="accepted-page",
        page_url="https://chatgpt.com/c/WEB:temporary-id",
        online=True,
    )
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="accepted-page",
        url=exact_url,
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.online = True
            self.reopen_clean = []

        async def locate_owned(self, *_args, **_kwargs):
            return acquired if self.online else None

        async def reopen(self, *_args, require_clean_ready=True, **_kwargs):
            self.reopen_clean.append(require_clean_ready)
            return acquired

    actions = Actions()
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) == acquired
    assert hop["conversation_url"] == exact_url
    assert state["roles"]["PLAN"]["page_url"] == exact_url

    actions.online = False
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) == acquired
    assert actions.reopen_clean == [False]
    assert state["status"] != "BLOCKED"
    assert state.get("maintenance") in (None, {})


def test_accepted_waiting_restores_drifted_page_id_without_resend_or_incident(
    tmp_path: Path,
    monkeypatch,
):
    import playwright_auto.cdpa_actions as actions_module
    from test_cdpa_actions import FakeClient, FakeContext, FakePage

    _, _, state, worker = setup_task(
        tmp_path, task_id="task-recover-drifted-accepted-page"
    )
    role_record = state["roles"]["PLAN"]
    physical_role = role_record["physical_role"]
    hop = _active_hop(state)
    exact_url = "https://chatgpt.com/c/exact-drifted-page"
    original_request_id = hop["request_id"]
    receipt = {
        "binding": {
            "page_id": "recorded-accepted-page",
            "role": physical_role,
        },
        "accepted_via": "user_message_identity",
        "user_message_id": "accepted-user",
        "user_turn_id": "accepted-turn",
    }
    hop.update(state="waiting", conversation_url=exact_url, receipt=receipt)
    role_record.update(
        page_id="recorded-accepted-page",
        page_url=exact_url,
        online=True,
    )
    live = FakePage(
        page_id="live-drifted-page",
        role=physical_role,
        team=state["team"],
        task_id=state["task_id"],
    )
    live.url = exact_url
    live.snapshot_value.url = exact_url
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = actions_module.CDPATabActions(FakeContext([live]), worker.config)

    recovered = asyncio.run(worker._owned_or_block(state, "PLAN", actions))

    assert recovered is not None
    assert recovered.page_id == "recorded-accepted-page"
    assert live.snapshot_value.page_id == "recorded-accepted-page"
    assert hop["state"] == "waiting"
    assert hop["request_id"] == original_request_id
    assert hop["receipt"] == receipt
    assert state["status"] != "BLOCKED"
    assert state.get("maintenance") in (None, {})
