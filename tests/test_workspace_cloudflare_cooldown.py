"""Fresh readiness failure must use the existing worker cooldown, never Send."""
import asyncio
from pathlib import Path

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.workspace import ChatGPTWorkspace
from test_cdpa_worker import FakeActions, setup_task
from test_workspace import ChallengeContext, ChallengePage


def test_unchanged_url_challenge_enters_worker_cooldown_without_send(tmp_path, monkeypatch):
    _config, store, state, worker = setup_task(tmp_path)
    worker.runtime_db.ensure_schema()
    state["context_policy"] = "fresh"
    state["status"] = "RUNNING"
    store.save(Path(state["manifest_path"]), state)
    request_id = _active_hop(state)["request_id"]

    class RootChallenge(ChallengePage):
        async def goto(self, url, **_options):
            self.url = url

        async def evaluate(self, _script):
            return True

    context = ChallengeContext()
    context.page = RootChallenge()

    class Actions(FakeActions):
        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def fresh_chat(self, *_args, **_kwargs):
            self.fresh_calls.append("allocate")
            return await ChatGPTWorkspace().open_role(context, "PLAN")

    actions = Actions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args: actions)
    result = asyncio.run(worker.advance(state["manifest_path"], context))
    hop = _active_hop(result)
    assert result["status"] == "RUNNING", result.get("block_reason")
    assert result["active_action"] == "rate_limit_cooldown"
    assert result.get("block_code") is None
    assert hop["state"] == "pre_send"
    assert hop["request_id"] == request_id
    assert hop.get("receipt") is None
    assert not hop.get("preboundary_recovery")
    assert not Path(hop["ledger_path"]).exists()
    assert context.page.closed
    assert worker._rate_limit_gate_active(state=result)
    again = asyncio.run(worker.advance(state["manifest_path"], context))
    assert actions.fresh_calls == ["allocate"]
    assert _active_hop(again)["request_id"] == request_id
    assert not Path(hop["ledger_path"]).exists()
