"""Fresh provider challenges are not request throttling or Send failures."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.chatgpt_errors import RateLimitBlockedError
from playwright_auto.workspace import ChatGPTWorkspace
from test_cdpa_worker import FakeActions, setup_task
from test_cdpa_worker_endpoints import _noop, _pool, _setup
from test_workspace import ChallengeContext, ChallengePage


class RootChallenge(ChallengePage):
    async def goto(self, url, **_options):
        self.url = url

    async def evaluate(self, _script):
        return True


class ChallengeActions(FakeActions):
    def __init__(self):
        super().__init__()
        self.context = ChallengeContext()
        self.context.page = RootChallenge()

    async def locate_owned(self, *_args, **_kwargs):
        return None

    async def fresh_chat(self, *_args, **_kwargs):
        self.fresh_calls.append("allocate")
        return await ChatGPTWorkspace().open_role(self.context, "PLAN")


@pytest.mark.parametrize("resume", [False, True])
def test_fresh_challenge_reports_provider_block_without_send_or_cooldown(tmp_path, monkeypatch, resume):
    _config, store, state, worker = setup_task(tmp_path)
    worker.runtime_db.ensure_schema()
    state["context_policy"] = "fresh"
    if resume:
        state.update(status="PAUSED", kanban_column="PAUSED", resume_status="INBOX")
    path = Path(state["manifest_path"])
    store.save(path, state)
    if resume:
        store.request_control(path, "resume", role="PLAN", reason="provider ready check")
    request_id = _active_hop(state)["request_id"]
    actions = ChallengeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args: actions)

    result = asyncio.run(worker.advance(path, actions.context))
    hop = _active_hop(result)
    assert result["status"] == "BLOCKED"
    assert result["block_code"] == "provider_challenge"
    assert "Cloudflare challenge" in result["block_reason"]
    assert not result["block_retryable"]
    assert hop["state"] == "pre_send"
    assert hop["request_id"] == request_id
    assert hop.get("receipt") is None
    assert not hop.get("preboundary_recovery")
    assert not Path(hop["ledger_path"]).exists()
    assert actions.context.page.closed
    assert actions.closed_teams == 0
    assert worker._active_rate_limit_scopes() == set()
    if resume:
        control = result["controls"][-1]
        assert control["status"] == "recovery_required"
        assert control["result"]["reason_code"] == "provider_challenge"
        assert control["result"]["postcondition"] is None

    again = asyncio.run(worker.advance(path, actions.context))
    assert actions.fresh_calls == ["allocate"]
    assert again == result
    assert not Path(hop["ledger_path"]).exists()


def test_request_throttle_expires_and_pool_admits_same_hop(tmp_path, monkeypatch):
    worker, store, endpoint, _, _ = _setup(tmp_path, task_count=0)
    state = store.create_task(
        "Admission verification", task_id="admission-check", requested_team="admission-check",
        workflow_mode="solo", roles=("PLAN",), browser_endpoint_id=endpoint["endpoint_id"],
        required_browser_endpoint_id=endpoint["endpoint_id"], conversation_mode="persistent",
    )
    request_id = _active_hop(state)["request_id"]
    worker.registry.update_task(state, now=0)

    class Actions(ChallengeActions):
        ready = False

        async def fresh_chat(self, state, role, **kwargs):
            assert worker._running_executions()[0]["request_id"] == request_id
            assert worker._running_executions()[0]["endpoint_id"] == endpoint["endpoint_id"]
            if not self.ready:
                self.fresh_calls.append("throttled")
                raise RateLimitBlockedError("Too many requests")
            return await FakeActions.fresh_chat(self, state, role, **kwargs)

    actions = Actions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args: actions)
    monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
    monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

    async def prepare(_state, _hop, _acquired, _actions, *, phase, wait_ms):
        assert phase == "pre_send"
        return SimpleNamespace(reason="ready")

    monkeypatch.setattr(worker, "_dispatch_browser_provider", prepare)

    async def run():
        pool = await _pool(store)
        try:
            await worker.run_once_pool(pool)
            waiting = store.load(state["manifest_path"])
            assert waiting["status"] in {"INBOX", "WAITING"}
            assert _active_hop(waiting)["request_id"] == request_id
            assert _active_hop(waiting)["state"] == "pre_send"
            assert worker._active_rate_limit_scopes() == {endpoint["rate_limit_scope"]}
            worker.registry.update_task(waiting, now=0)
            await worker.run_once_pool(pool)
            assert actions.fresh_calls == ["throttled"]

            waiting = store.load(state["manifest_path"])
            assert waiting["status"] == "WAITING"
            worker.registry.update_task(waiting, now=0)
            # Move both current and compatibility timestamps to the same expired time.
            for cooldown in (worker._rate_limit_cooldowns[endpoint["rate_limit_scope"]], worker._rate_limit_cooldown):
                cooldown["release_not_before"] = "2000-01-01T00:00:00+00:00"
            actions.ready = True
            await worker.run_once_pool(pool)
            admission = worker.runtime_db.get_task_detail(state["task_id"])
            assert admission["status"] == "RUNNING"
            assert admission["active_hop"]["state"] == "sending"  # Prepared; no Send in this tick.
            assert len(actions.fresh_calls) == 2
            hop = _active_hop(store.load(state["manifest_path"]))
            assert hop["request_id"] == request_id
            assert not hop.get("receipt") and not Path(hop["ledger_path"]).exists()
            assert worker._running_executions()[0]["page_id"]
        finally:
            await pool.close()

    asyncio.run(run())
