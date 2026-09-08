from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_independent import (
    RECOVERY_WARMUP_SECONDS,
    canonical_independent_events,
    claim_oldest_event,
)
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions, ready_client


def setup_agent(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    blocked = store.create_task(
        "Blocked target",
        requested_team="alpha",
        task_id="target-a",
    )
    blocked = store.update(
        blocked["manifest_path"],
        lambda state: {
            **state,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "role_offline",
            "block_reason": "alpha-dev is offline",
            "updated_at": "2026-07-26T00:00:00+00:00",
        },
    )
    blocked = store.update(
        blocked["manifest_path"],
        lambda state: {
            **state,
            "blocked_at": "2026-07-26T00:00:00+00:00",
        },
    )
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Use @mcp-g8 and recover the affected task directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
        temporary_chat=False,
    )
    standby = store.update(
        standby["manifest_path"],
        lambda state: {
            **state,
            "independent": {
                **state["independent"],
                "watermarks": {
                    **state["independent"]["watermarks"],
                    "recovery_enabled_at": "2026-07-26T00:00:00+00:00",
                },
            },
        },
    )
    claimed = claim_oldest_event(
        standby,
        canonical_independent_events(standby, [blocked, standby]),
        all_tasks=[blocked, standby],
    )
    claimed = store.save(claimed["manifest_path"], claimed)
    return config, store, blocked, claimed, CDPAWorker(config, store=store)


def test_temporary_independent_pre_send_opens_exact_fresh_url_and_ignores_saved_chat(tmp_path: Path):
    _config, _store, _blocked, state, worker = setup_agent(tmp_path)
    state["independent"]["temporary_chat"] = True
    role = state["roles"]["AGENT"]
    role.update(
        page_id="old-page",
        page_url="https://chatgpt.com/c/old-conversation",
        online=False,
    )
    hop = _active_hop(state)

    class TemporaryActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.fresh_urls = []

        async def acquire(self, *_args, **_kwargs):
            raise AssertionError("temporary chat must not use persistent acquisition")

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("temporary chat must not reopen the saved conversation")

        async def new_chat(self, *_args, **_kwargs):
            raise AssertionError("temporary chat must use the exact temporary URL")

        async def fresh_chat(self, current, logical_role, *, url):
            self.fresh_urls.append(url)
            record = current["roles"][logical_role]
            acquired = await FakeActions.acquire(self, current, logical_role)
            return type(acquired)(
                client=acquired.client,
                page_id=f"temporary-{record['physical_role']}",
                url=url,
                created=True,
                new_chat=True,
            )

    actions = TemporaryActions()
    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.fresh_urls == ["https://chatgpt.com/?temporary-chat=true"]
    assert role["page_id"].startswith("temporary-")
    assert role["page_url"] == "https://chatgpt.com/?temporary-chat=true"
    assert hop["state"] == "sending"


def test_temporary_independent_continuation_reuses_current_owned_chat(tmp_path: Path):
    _config, store, _blocked, state, worker = setup_agent(tmp_path)
    state["independent"]["temporary_chat"] = True
    role = state["roles"]["AGENT"]
    role.update(
        page_id="temporary-page",
        page_url="https://chatgpt.com/c/current-temporary",
        online=True,
        conversation_generation=1,
    )
    first_hop = _active_hop(state)
    first_hop.update(
        state="responded",
        response="Continue this job.",
        response_sha256="a" * 64,
        conversation_url=role["page_url"],
    )
    state = store.save(state["manifest_path"], state)
    continued = store.continue_independent_task(
        state["manifest_path"], reason="Continue in the same conversation."
    )
    hop = _active_hop(continued)

    class ContinuationActions(FakeActions):
        async def fresh_chat(self, *_args, **_kwargs):
            raise AssertionError("same-job continuation must not open a fresh temporary chat")

        async def acquire(self, *_args, **_kwargs):
            raise AssertionError("same-job continuation must not use persistent acquisition")

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("same-job continuation must not reopen a saved conversation")

        async def locate_owned(self, current, logical_role):
            record = current["roles"][logical_role]
            return AcquiredRole(
                client=ready_client(current, logical_role),
                page_id=record["page_id"],
                url=record["page_url"],
                created=False,
                new_chat=False,
            )

    asyncio.run(worker._pre_send(continued, hop, ContinuationActions()))

    current_role = continued["roles"]["AGENT"]
    assert continued["independent"]["cycle"] == 2
    assert hop["kind"] == "independent_cycle"
    assert hop["state"] == "sending"
    assert current_role["page_id"] == "temporary-page"
    assert current_role["page_url"] == "https://chatgpt.com/c/current-temporary"
    assert current_role["conversation_generation"] == 1


def test_independent_pre_send_uses_shared_prompt_without_route_contract(tmp_path: Path):
    _config, _store, _blocked, state, worker = setup_agent(tmp_path)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert hop["state"] == "sending"
    assert hop.get("expected_report_path") is None
    assert "Use @mcp-g8" in hop["prompt"]
    assert "INDEPENDENT_AGENT_OPERATING_RULE" in hop["prompt"]
    assert '"trigger_type": "recovery"' in hop["prompt"]
    assert '"target_task_id": "target-a"' in hop["prompt"]
    assert "allowed-routes" not in hop["prompt"]
    assert ".plan/" not in hop["prompt"]
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"


def test_post_release_inflight_lease_blocks_deferred_independent_new_chat(tmp_path: Path):
    _config, _store, _blocked, state, worker = setup_agent(tmp_path)
    worker._rate_limit_cooldown = {
        "state": "released",
        "detected_at": "2026-08-09T00:00:00+00:00",
        "release_not_before": "2026-08-09T00:01:00+00:00",
        "released_at": "2026-08-09T00:01:00+00:00",
        "post_release_acquisition": "in_progress",
    }
    state["independent"]["new_chat_next_job"] = True
    state["independent"]["new_chat_deferred_task_id"] = None
    hop = _active_hop(state)

    class NoNewChat(FakeActions):
        async def new_chat(self, *_args, **_kwargs):
            raise AssertionError("another post-release acquisition owns the profile lease")

    asyncio.run(worker._pre_send(state, hop, NoNewChat()))

    assert hop["state"] == "pre_send"
    assert state["independent"]["new_chat_next_job"] is True
    assert worker._rate_limit_cooldown["post_release_acquisition"] == "in_progress"


def test_independent_responded_waits_for_explicit_completion_without_route_parse(
    tmp_path: Path,
):
    _config, _store, _blocked, state, worker = setup_agent(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["state"] = "responded"
    hop["response"] = "Recovered target and verified the postcondition."
    hop["response_sha256"] = "a" * 64

    worker._responded(state, hop)

    assert hop["state"] == "responded"
    assert hop["route"] is None
    assert state["status"] == "RUNNING"
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"
    assert state["active_action"] == "await_completion"
    assert len(state["hops"]) == 1




def test_independent_pause_and_resume_preserve_active_event(tmp_path: Path):
    _config, store, _blocked, state, worker = setup_agent(tmp_path)
    event_key = state["independent"]["active_event"]["event_key"]
    state = store.request_control(state["manifest_path"], "pause", role="AGENT")

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert state["status"] == "PAUSED"
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"
    assert state["independent"]["enabled"] is False
    assert state["independent"]["active_event"]["event_key"] == event_key

    state = store.save(state["manifest_path"], state)
    state = store.request_control(state["manifest_path"], "resume", role="AGENT")
    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert state["status"] == "RUNNING"
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"
    assert state["independent"]["enabled"] is True
    assert state["independent"]["active_event"]["event_key"] == event_key




def test_operator_reset_releases_independent_job_without_respawn(tmp_path: Path):
    _config, store, _blocked, state, worker = setup_agent(tmp_path)
    state = store.request_control(state["manifest_path"], "reset", role="AGENT")

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    state = store.save(state["manifest_path"], state)
    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()

    assert state["status"] == "WAITING"
    assert state["terminal_state"] is None
    assert state["independent"]["enabled"] is True
    assert state["independent"]["active_event"] is None
    assert state["independent"]["job_history"][-1]["disposition"] == "RESET"
    assert state["independent"].get("successor_task_id") is None
    assert not [
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item["independent"].get("previous_task_id") == state["task_id"]
    ]

def test_interval_agent_stays_waiting_until_first_interval_is_due(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    monitor = store.create_independent_agent(
        "Monitor",
        system_prompt="Inspect progress.",
        trigger_settings={"interval_minutes": 30, "check_all": True},
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()

    changed = worker._activate_independent_agents()
    persisted = store.load(monitor["manifest_path"])

    assert monitor["task_id"] not in changed
    assert persisted["status"] == "WAITING"
    assert persisted["independent"]["active_event"] is None


def test_worker_activates_oldest_independent_event_and_persists_claim(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    older = store.create_task("Older", requested_team="alpha", task_id="older")
    newer = store.create_task("Newer", requested_team="beta", task_id="newer")
    for state, at in (
        (older, "2026-07-26T00:00:00+00:00"),
        (newer, "2026-07-26T00:01:00+00:00"),
    ):
        store.update(
            state["manifest_path"],
            lambda current, at=at: {
                **current,
                "status": "BLOCKED",
                "kanban_column": "BLOCKED",
                "block_code": "role_offline",
                "block_reason": "role is offline",
                "updated_at": at,
            },
        )
        store.update(
            state["manifest_path"],
            lambda current, at=at: {**current, "blocked_at": at},
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
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()

    changed = worker._activate_independent_agents()
    persisted = store.load(standby["manifest_path"])

    assert persisted["status"] == "RUNNING"
    assert persisted["independent"]["active_event"]["target_task_id"] == "older"
    assert persisted["hops"][0]["state"] == "pre_send"
    assert persisted["task_id"] in changed



def _blocked_target(store: TaskStore, task_id: str, *, blocked_at: datetime) -> dict:
    state = store.create_task(
        f"Blocked {task_id}",
        requested_team=task_id,
        task_id=task_id,
    )
    state = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "role_offline",
            "block_reason": "DEV tab is offline",
            "updated_at": blocked_at.isoformat(),
        },
    )
    return store.update(
        state["manifest_path"],
        lambda current: {**current, "blocked_at": blocked_at.isoformat()},
    )


def test_idle_recovery_pause_resume_restarts_warmup(tmp_path: Path, monkeypatch):
    enabled_at = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "playwright_auto.cdpa_worker.utc_now", lambda: enabled_at.isoformat()
    )
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    target = _blocked_target(
        store,
        "resume-warmup-target",
        blocked_at=enabled_at - timedelta(minutes=10),
    )
    state = store.create_independent_agent(
        "Resume warmup",
        system_prompt="Recover.",
        task_id="agent-resume-warmup-g1",
        trigger_settings={"recovery": True},
    )
    state = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "independent": {
                **current["independent"],
                "watermarks": {
                    **current["independent"]["watermarks"],
                    "recovery_enabled_at": "2026-07-31T07:00:00+00:00",
                },
            },
        },
    )
    state = store.update_independent_agent(state["manifest_path"], enabled=False)
    worker = CDPAWorker(config, store=store)
    state = store.request_control(state["manifest_path"], "resume", role="AGENT")

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert state["independent"]["watermarks"]["recovery_enabled_at"] == enabled_at.isoformat()
    assert canonical_independent_events(
        state,
        [state, target],
        now=enabled_at + timedelta(seconds=RECOVERY_WARMUP_SECONDS - 1),
    ) == []
    assert canonical_independent_events(
        state,
        [state, target],
        now=enabled_at + timedelta(seconds=RECOVERY_WARMUP_SECONDS),
    )


def test_active_recovery_resume_preserves_job_and_gates_next_target(
    tmp_path: Path, monkeypatch
):
    enabled_at = datetime(2026, 7, 31, 8, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "playwright_auto.cdpa_worker.utc_now", lambda: enabled_at.isoformat()
    )
    monkeypatch.setattr(
        "playwright_auto.cdpa_store.utc_now", lambda: enabled_at.isoformat()
    )
    monkeypatch.setattr(
        "playwright_auto.cdpa_store_independent.utc_now",
        lambda: enabled_at.isoformat(),
    )
    _config, store, _blocked, state, worker = setup_agent(tmp_path)
    event_key = state["independent"]["active_event"]["event_key"]
    state = store.request_control(state["manifest_path"], "pause", role="AGENT")
    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    state = store.save(state["manifest_path"], state)
    state = store.request_control(state["manifest_path"], "resume", role="AGENT")

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert state["independent"]["active_event"]["event_key"] == event_key
    assert state["independent"]["watermarks"]["recovery_enabled_at"] == enabled_at.isoformat()
    state = store.save(state["manifest_path"], state)
    released = store.reset_independent_task(
        state["manifest_path"], reason="Release current recovery job"
    )
    next_target = _blocked_target(
        store,
        "next-recovery-target",
        blocked_at=enabled_at - timedelta(minutes=10),
    )

    assert canonical_independent_events(
        released,
        [released, next_target],
        now=enabled_at + timedelta(seconds=RECOVERY_WARMUP_SECONDS - 1),
    ) == []
    assert canonical_independent_events(
        released,
        [released, next_target],
        now=enabled_at + timedelta(seconds=RECOVERY_WARMUP_SECONDS),
    )


def test_redundant_recovery_resume_keeps_existing_warmup_boundary(
    tmp_path: Path, monkeypatch
):
    resume_at = datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc)
    existing_enabled_at = "2026-07-31T08:29:45+00:00"
    monkeypatch.setattr(
        "playwright_auto.cdpa_worker.utc_now", lambda: resume_at.isoformat()
    )
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_independent_agent(
        "Redundant resume",
        system_prompt="Recover.",
        task_id="agent-redundant-resume-g1",
        trigger_settings={"recovery": True},
    )
    state = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "independent": {
                **current["independent"],
                "watermarks": {
                    **current["independent"]["watermarks"],
                    "recovery_enabled_at": existing_enabled_at,
                },
            },
        },
    )
    worker = CDPAWorker(config, store=store)
    state = store.request_control(state["manifest_path"], "resume", role="AGENT")

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert (
        state["independent"]["watermarks"]["recovery_enabled_at"]
        == existing_enabled_at
    )
