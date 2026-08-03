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
    )
    worker = CDPAWorker(config, store=store)
    worker.runtime_db.ensure_schema()
    return config, store, state, worker


def test_reload_catalog_command_fails_when_hydration_is_still_incomplete(
    tmp_path: Path, monkeypatch
):
    _config, _store, _state, worker = _setup(tmp_path)
    worker.runtime_db.enqueue_command(
        command_id="cmd-reload-incomplete",
        idempotency_key="idem-reload-incomplete",
        kind="reload_catalog",
        task_id=None,
        expected_task_version=None,
        payload={},
    )
    worker.runtime_degraded = True
    worker.registry = None
    monkeypatch.setattr(
        worker,
        "hydrate_runtime",
        lambda **_kwargs: {
            "complete": False,
            "discovered_at": "now",
            "errors": [{"error": "broken manifest"}],
        },
    )

    result = worker.dispatch_command_once()

    assert result["status"] == "failed"
    assert "reload catalog is incomplete" in result["error"]


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
        "options": {"report_mode": "file"},
        "depends_on_task_ids": [],
    }
