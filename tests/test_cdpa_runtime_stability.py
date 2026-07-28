from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.chatgpt import RateLimitBlockedError, UnsafePageStateError
from playwright_auto.dashboard_api import DashboardAPI
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_core import write_config


class _RateClient:
    def __init__(self, *, visible: bool = False) -> None:
        self.dismiss_calls = 0
        self.assert_calls = 0
        self.visible = visible

    async def dismiss_known_rate_limit(self, *, timeout_ms=None):
        self.dismiss_calls += 1
        return "Got it"

    async def known_rate_limit_visible(self) -> bool:
        return self.visible

    async def assert_ownership(self):
        self.assert_calls += 1
        return SimpleNamespace(
            conversation_url="https://chatgpt.com/c/rate-limit",
            url="https://chatgpt.com/c/rate-limit",
        )


class _RateActions:
    def __init__(self, client: _RateClient) -> None:
        self.client = client
        self.acquire_calls = 0

    async def locate_owned(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=self.client,
            page_id=str(record.get("page_id") or "page-plan"),
            url=str(record.get("page_url") or "https://chatgpt.com/c/rate-limit"),
            created=False,
            new_chat=False,
        )

    async def acquire(self, state, role):
        self.acquire_calls += 1
        return await self.locate_owned(state, role)


class _RespondingActions:
    def __init__(self, *, remains_active: bool) -> None:
        self.remains_active = remains_active
        self.acquire_calls = 0
        self.client = SimpleNamespace(wait_until_clean_ready=self.wait_until_clean_ready)

    async def acquire(self, state, role):
        self.acquire_calls += 1
        if self.acquire_calls == 1:
            raise UnsafePageStateError("page is already responding")
        record = state["roles"][role]
        return AcquiredRole(
            client=self.client,
            page_id=str(record.get("page_id") or "page-plan"),
            url="https://chatgpt.com/c/exact-review",
            created=False,
            new_chat=False,
        )

    async def locate_owned(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=self.client,
            page_id=str(record.get("page_id") or "page-plan"),
            url="https://chatgpt.com/c/exact-review",
            created=False,
            new_chat=False,
        )

    async def wait_until_clean_ready(self, **_kwargs):
        if self.remains_active:
            raise TimeoutError("still responding")
        return SimpleNamespace()


def _setup(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Repair shared runtime boundaries",
        requested_team="alpha",
        task_id="task-rate-a",
        report_mode="inline",
    )
    worker = CDPAWorker(config, store=store)
    worker.runtime_db.ensure_schema()
    return config, store, state, worker


def test_shared_rate_limit_cooldown_coalesces_dismiss_and_pauses_agent_claims(
    tmp_path: Path, monkeypatch
):
    _config, _store, state, worker = _setup(tmp_path)
    state["roles"]["PLAN"].update(
        page_id="page-plan", page_url="https://chatgpt.com/c/rate-limit"
    )
    client = _RateClient()
    actions = _RateActions(client)

    async def enter_twice():
        await asyncio.gather(
            worker._enter_rate_limit_cooldown(
                state, actions, RateLimitBlockedError("Too many requests")
            ),
            worker._enter_rate_limit_cooldown(
                state, actions, RateLimitBlockedError("Too many requests")
            ),
        )

    asyncio.run(enter_twice())

    assert client.dismiss_calls == 1
    assert worker._rate_limit_gate_active() is True
    snapshot = worker.runtime_db.get_snapshot("worker")
    assert snapshot["payload"]["rate_limit_cooldown"]["state"] == "active"
    monkeypatch.setattr(
        "playwright_auto.cdpa_worker.canonical_independent_events",
        lambda *_args, **_kwargs: pytest.fail("agent triggers must not be inspected during cooldown"),
    )
    worker.registry = SimpleNamespace(tasks_by_id={}, paths_by_id={})
    assert worker._activate_independent_agents() == set()


def _prepare_sending(state: dict, *, page_id: str) -> dict:
    role = str(state["active_role"] or "PLAN")
    state["roles"][role].update(
        page_id=page_id,
        page_url=f"https://chatgpt.com/c/{page_id}",
        online=True,
    )
    hop = _active_hop(state)
    hop["prompt"] = f"prompt for {state['task_id']}"
    hop["prompt_sha256"] = hashlib.sha256(hop["prompt"].encode()).hexdigest()
    hop["state"] = "sending"
    state["active_action"] = "send"
    return hop


def _begin_sent_record(state: dict, hop: dict) -> dict:
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=str(hop["physical_role"]),
        prompt=str(hop["prompt"]),
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": state["manifest_path"],
        },
        role_prompt_hash="constructor",
        request_id=str(hop["request_id"]),
        render_request_marker=False,
    )
    record = ledger.update(record.request_id, status=RequestStatus.SENDING)
    receipt = {"prompt_sha256": hop["prompt_sha256"]}
    record = ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1_700_000_000.0,
        receipt=receipt,
    )
    return {"receipt": receipt, "record": record.to_dict()}


def test_competing_sends_activate_cooldown_before_second_send_boundary(
    tmp_path: Path, monkeypatch
):
    _config, store, first, worker = _setup(tmp_path)
    second = store.create_task(
        "Second competing send",
        requested_team="beta",
        task_id="task-rate-b",
        report_mode="inline",
    )
    first_hop = _prepare_sending(first, page_id="page-rate-a")
    second_hop = _prepare_sending(second, page_id="page-rate-b")
    client = _RateClient()
    first_actions = _RateActions(client)
    second_actions = _RateActions(client)
    send_calls: list[str] = []

    class CompetingSendBlock:
        def __init__(self, prompt, **kwargs):
            self.prompt = prompt
            self.ledger_path = kwargs["ledger_path"]
            self.source_context = kwargs["source_context"]
            self.role_prompt_hash = kwargs["role_prompt_hash"]
            self.request_id = kwargs["request_id"]

        async def run(self, _context):
            task_id = str(self.source_context["task_id"])
            send_calls.append(task_id)
            state = first if task_id == first["task_id"] else second
            hop = first_hop if task_id == first["task_id"] else second_hop
            ledger = RequestLedger(self.ledger_path)
            record = ledger.begin(
                role=str(hop["physical_role"]),
                prompt=self.prompt,
                source_context=self.source_context,
                role_prompt_hash=self.role_prompt_hash,
                request_id=self.request_id,
                render_request_marker=False,
            )
            record = ledger.update(record.request_id, status=RequestStatus.SENDING)
            if task_id == first["task_id"]:
                raise RateLimitBlockedError("Too many requests")
            receipt = {"prompt_sha256": hop["prompt_sha256"]}
            record = ledger.update(
                record.request_id,
                status=RequestStatus.SENT,
                accepted_at=1_700_000_000.0,
                receipt=receipt,
            )
            return {"receipt": receipt, "record": record.to_dict()}

    monkeypatch.setattr(worker_module, "DurableSendBlock", CompetingSendBlock)

    async def compete():
        return await asyncio.gather(
            worker._sending(first, first_hop, first_actions),
            worker._sending(second, second_hop, second_actions),
            return_exceptions=True,
        )

    results = asyncio.run(compete())

    assert results == [None, None]
    assert send_calls == ["task-rate-a"]
    assert worker._rate_limit_gate_active() is True
    assert first["active_action"] == "rate_limit_cooldown_reconcile"
    assert second["status"] == "BLOCKED"
    assert second["block_code"] == "rate_limit_cooldown"
    assert second["block_retryable"] is True
    assert client.dismiss_calls == 1


def test_active_cooldown_reconciles_existing_sent_ledger_once(
    tmp_path: Path, monkeypatch
):
    _config, _store, state, worker = _setup(tmp_path)
    hop = _prepare_sending(state, page_id="page-accepted")
    cached = _begin_sent_record(state, hop)
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-07-27T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }
    calls = 0

    class CachedSendBlock:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _context):
            nonlocal calls
            calls += 1
            return cached

    monkeypatch.setattr(worker_module, "DurableSendBlock", CachedSendBlock)

    asyncio.run(worker._sending(state, hop, _RateActions(_RateClient())))

    assert calls == 1
    assert hop["state"] == "sent"
    assert state["active_action"] == "wait_response"
    assert state.get("block_code") is None


def test_cooldown_blocks_pre_send_but_preserves_accepted_waiting_identity(tmp_path: Path):
    _config, store, state, worker = _setup(tmp_path)
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-07-27T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }
    hop = _active_hop(state)
    assert worker._cooldown_allows_hop(hop) is False

    result = asyncio.run(worker.advance(state["manifest_path"], SimpleNamespace(pages=[])))
    assert result["status"] == "BLOCKED"
    assert result["block_code"] == "rate_limit_cooldown"
    assert result["block_retryable"] is True
    persisted_hop = _active_hop(store.load(state["manifest_path"]))
    persisted_hop.update(
        state="waiting",
        receipt={
            "user_message_id": "user-accepted",
            "user_turn_id": "turn-accepted",
        },
    )
    assert worker._cooldown_allows_hop(persisted_hop) is True


def test_cooldown_restores_across_restart_and_preserves_accepted_waiting(
    tmp_path: Path,
):
    config, store, state, worker = _setup(tmp_path)

    def accept(current: dict) -> dict:
        hop = _active_hop(current)
        hop["state"] = "waiting"
        hop["receipt"] = {
            "user_message_id": "user-accepted",
            "user_turn_id": "turn-accepted",
        }
        current["active_action"] = "wait_response"
        return current

    persisted = store.update(state["manifest_path"], accept)
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-07-27T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
        "profile": str(config.cdp_url),
    }
    worker._publish_heartbeat(force=True)

    restarted = CDPAWorker(config, store=store)
    restarted.runtime_db.ensure_schema()
    restarted._restore_rate_limit_cooldown()

    assert restarted._rate_limit_gate_active() is True
    assert restarted._cooldown_allows_hop(_active_hop(persisted)) is True


def test_builtin_seed_does_not_recreate_explicitly_stopped_agents(tmp_path: Path):
    config, store, _state, worker = _setup(tmp_path)
    created = [
        store.create_independent_agent(
            "Maintainers",
            system_prompt="Recover directly.",
            task_id="agent-maintainers-g1",
            trigger_settings={"recovery": True},
            max_cycles=5,
        ),
        store.create_independent_agent(
            "Monitor",
            system_prompt="Observe directly.",
            task_id="agent-monitor-g1",
            trigger_settings={"interval_minutes": 30, "check_all": True},
            max_cycles=1,
        ),
    ]

    def stop(current: dict) -> dict:
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "STOPPED"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["active_action"] = "stopped"
        current["stopped_at"] = "2026-07-27T00:00:00+00:00"
        current["independent"]["enabled"] = False
        current["independent"]["active_event"] = None
        return current

    for item in created:
        store.update(item["manifest_path"], stop)

    worker._ensure_builtin_independent_agents()
    agents = [
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item["independent"]["agent_key"] in {"maintainers", "monitor"}
    ]

    assert {(item["task_id"], item["status"]) for item in agents} == {
        ("agent-maintainers-g1", "STOPPED"),
        ("agent-monitor-g1", "STOPPED"),
    }
    assert all(item["independent"]["enabled"] is False for item in agents)
    assert all(item["independent"]["agent_generation"] == 1 for item in agents)


def test_pre_send_page_already_responding_reconciles_without_duplicate_send(tmp_path: Path):
    _config, _store, state, worker = _setup(tmp_path)
    state["roles"]["PLAN"].update(
        page_id="page-plan", page_url="https://chatgpt.com/c/exact-review"
    )
    hop = _active_hop(state)
    actions = _RespondingActions(remains_active=False)

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.acquire_calls == 2
    assert hop["state"] == "sending"
    assert state["active_action"] == "send"


def test_pre_send_page_already_responding_stays_pre_send_while_active(tmp_path: Path):
    _config, _store, state, worker = _setup(tmp_path)
    state["roles"]["PLAN"].update(
        page_id="page-plan", page_url="https://chatgpt.com/c/exact-review"
    )
    hop = _active_hop(state)
    actions = _RespondingActions(remains_active=True)

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.acquire_calls == 1
    assert hop["state"] == "pre_send"
    assert state["active_action"] == "reconcile_page_response"


def _independent_task(
    tmp_path: Path,
    *,
    task_id: str,
    generation: int,
    status: str,
    previous_task_id: str | None,
    report_content: str | None = None,
) -> dict:
    digest = hashlib.sha256((report_content or "").encode()).hexdigest()
    reports = (
        [
            {
                "report_id": f"{task_id}-independent",
                "role": "AGENT",
                "created_at": f"2026-07-2{generation}T00:00:00+00:00",
                "content": report_content,
                "sha256": digest,
                "summary": f"generation {generation} summary",
                "outcome": "SUCCESS",
            }
        ]
        if report_content
        else []
    )
    return {
        "task_mode": "independent",
        "task_id": task_id,
        "team": "agent-monitor",
        "status": status,
        "kanban_column": "INDEPENDENT_AGENTS",
        "active_role": None,
        "active_hop_id": None,
        "active_action": "completed" if status == "DONE" else "waiting_trigger",
        "created_at": f"2026-07-2{generation}T00:00:00+00:00",
        "updated_at": f"2026-07-2{generation}T01:00:00+00:00",
        "completed_at": (
            f"2026-07-2{generation}T01:00:00+00:00" if status == "DONE" else None
        ),
        "task_text": "Independent agent: Monitor",
        "manifest_path": str(tmp_path / ".plan" / "agent-monitor" / f"{task_id}.json"),
        "repository": str(tmp_path),
        "roles": {
            "AGENT": {
                "physical_role": "agent-monitor-agent",
                "status": "idle",
                "turn": generation,
                "page_id": "page-monitor",
                "page_url": "https://chatgpt.com/c/monitor",
                "online": False,
            }
        },
        "hops": [],
        "independent": {
            "agent_name": "Monitor",
            "agent_key": "monitor",
            "agent_generation": generation,
            "previous_task_id": previous_task_id,
            "successor_task_id": None,
            "enabled": True,
            "system_prompt": "private",
            "trigger_settings": {"interval_minutes": 30},
            "active_event": None,
            "cycle": 0,
            "max_cycles": 1,
            "last_outcome": (
                {"outcome": "SUCCESS", "summary": f"generation {generation} summary"}
                if status == "DONE"
                else None
            ),
        },
        "route_timeline": [],
        "dependency_events": [],
        "queue_events": [],
        "errors": [],
        "reports": reports,
        "attachments": [],
        "cleanup": {"state": "ACTIVE"},
        "controls": [],
        "options": {"report_mode": "inline"},
        "depends_on_task_ids": [],
    }


def test_selected_agent_projection_aggregates_only_its_generations_and_inline_reports(
    tmp_path: Path,
):
    generation1 = _independent_task(
        tmp_path,
        task_id="agent-monitor-g1",
        generation=1,
        status="DONE",
        previous_task_id=None,
        report_content="# Monitor report\n\nGeneration one durable body.",
    )
    generation1["reports"][0]["hop_id"] = 1
    generation1["hops"] = [
        {
            "hop_id": 1,
            "state": "responded",
            "target_role": "AGENT",
            "physical_role": "agent-monitor-agent",
            "turn": 1,
            "response": "# Monitor report\n\nGeneration one durable body.",
            "response_sha256": hashlib.sha256(
                b"# Monitor report\n\nGeneration one durable body."
            ).hexdigest(),
            "timestamps": {"responded_at": "2026-07-21T00:00:00+00:00"},
        }
    ]
    generation2 = _independent_task(
        tmp_path,
        task_id="agent-monitor-g2",
        generation=2,
        status="WAITING",
        previous_task_id="agent-monitor-g1",
    )
    unrelated = _independent_task(
        tmp_path,
        task_id="agent-other-g1",
        generation=1,
        status="DONE",
        previous_task_id=None,
        report_content="unrelated report",
    )
    unrelated["team"] = "agent-other"
    unrelated["independent"]["agent_name"] = "Other"
    unrelated["independent"]["agent_key"] = "other"

    projection = build_task_projection(
        generation2,
        tasks=[unrelated, generation2, generation1],
    )

    assert [item["task_id"] for item in projection.detail["independent_history"]] == [
        "agent-monitor-g2",
        "agent-monitor-g1",
    ]
    assert all(item["agent_key"] == "monitor" for item in projection.detail["independent_history"])
    assert len(projection.detail["reports"]) == 1
    report = projection.detail["reports"][0]
    assert report["source_task_id"] == "agent-monitor-g1"
    assert report["url"].startswith("/api/reports/agent-monitor-g2/")
    locator = projection.private["reports"][report["report_id"]]
    assert locator["content"].startswith("# Monitor report")


def test_selected_agent_projection_exposes_unmaterialized_responded_hop_body(
    tmp_path: Path,
):
    body = "# Maintainers response\n\nDurable body before independent completion."
    responded = _independent_task(
        tmp_path,
        task_id="agent-maintainers-g1",
        generation=1,
        status="STOPPED",
        previous_task_id=None,
    )
    responded["team"] = "agent-maintainers"
    responded["status"] = "STOPPED"
    responded["active_action"] = "stopped"
    responded["stopped_at"] = "2026-07-27T00:30:00+00:00"
    responded["independent"].update(
        agent_name="Maintainers",
        agent_key="maintainers",
        enabled=False,
    )
    responded["hops"] = [
        {
            "hop_id": 1,
            "state": "responded",
            "target_role": "AGENT",
            "physical_role": "agent-maintainers-agent",
            "turn": 1,
            "response": body,
            "response_sha256": "not-the-content-hash",
            "timestamps": {"responded_at": "2026-07-27T00:20:00+00:00"},
        },
        {
            "hop_id": 2,
            "state": "waiting",
            "target_role": "AGENT",
            "physical_role": "agent-maintainers-agent",
            "turn": 2,
            "response": "partial response must not become a report",
            "timestamps": {"waiting_at": "2026-07-27T00:25:00+00:00"},
        },
    ]
    unrelated = _independent_task(
        tmp_path,
        task_id="agent-other-g1",
        generation=1,
        status="DONE",
        previous_task_id=None,
        report_content="unrelated durable body",
    )
    unrelated["team"] = "agent-other"
    unrelated["independent"].update(agent_name="Other", agent_key="other")

    projection = build_task_projection(
        responded,
        tasks=[unrelated, responded],
    )

    assert [item["task_id"] for item in projection.detail["independent_history"]] == [
        "agent-maintainers-g1"
    ]
    assert len(projection.detail["reports"]) == 1
    report = projection.detail["reports"][0]
    assert report == {
        "report_id": "g1-hop1-response",
        "physical_role": "agent-maintainers-agent",
        "role": "AGENT",
        "turn": 1,
        "created_at": "2026-07-27T00:20:00+00:00",
        "summary": None,
        "outcome": None,
        "source_task_id": "agent-maintainers-g1",
        "generation": 1,
        "url": "/api/reports/agent-maintainers-g1/g1-hop1-response",
    }
    locator = projection.private["reports"][report["report_id"]]
    encoded = body.encode()
    assert locator == {
        "content": body,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size": len(encoded),
    }

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    worker = CDPAWorker(config, store=TaskStore(config))
    worker.runtime_db.ensure_schema()
    worker.runtime_db.replace_task_projections(
        [projection],
        catalog={"complete": True, "discovered_at": "now", "errors": []},
    )
    api = DashboardAPI(config, db=worker.runtime_db)
    assert api.report_bytes(
        "agent-maintainers-g1", "g1-hop1-response", maintenance=False
    ) == encoded


def test_dashboard_api_serves_inline_selected_agent_report_body(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    worker = CDPAWorker(config, store=TaskStore(config))
    worker.runtime_db.ensure_schema()
    body = b"# Durable independent report\n\nRecovered."
    from playwright_auto.cdpa_projection import TaskProjection

    projection = TaskProjection(
        task_id="agent-monitor-g2",
        team="agent-monitor",
        status="WAITING",
        surface="active",
        active_role=None,
        updated_at="2026-07-27T00:00:00+00:00",
        summary={"task_id": "agent-monitor-g2", "team": "agent-monitor", "status": "WAITING"},
        detail={"task_id": "agent-monitor-g2", "reports": []},
        private={
            "manifest_path": str(tmp_path / ".plan" / "agent-monitor" / "g2.json"),
            "reports": {
                "generation-1-report": {
                    "content": body.decode(),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                }
            },
            "maintenance_reports": {},
            "timeline": [],
        },
    )
    worker.runtime_db.replace_task_projections(
        [projection],
        catalog={"complete": True, "discovered_at": "now", "errors": []},
    )
    api = DashboardAPI(config, db=worker.runtime_db)

    assert api.report_bytes("agent-monitor-g2", "generation-1-report", maintenance=False) == body
