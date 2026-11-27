"""Regression: provider challenges must stop pristine Resume before another allocation."""
from __future__ import annotations

import asyncio
import copy
import pytest

from playwright_auto.chatgpt_errors import ProviderChallengeError
from test_operator_resume_recovery import _prepare_operator_new_chat_pristine_sending


@pytest.mark.parametrize("stage", ["fresh_chat", "restart", "new_chat"])
def test_pristine_resume_keeps_provider_challenge_and_never_sends(tmp_path, stage):
    _store, state, worker, _path, hop, ledger, _donor = (
        _prepare_operator_new_chat_pristine_sending(
            tmp_path, task_id="provider-challenge-" + stage,
        )
    )
    before_ledger = ledger.path.read_bytes()
    before_role = copy.deepcopy(state["roles"]["PLAN"])
    before_hop = copy.deepcopy(hop)
    calls = []

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            calls.append("locate")
            return None

        async def fresh_chat(self, *_args, **_kwargs):
            calls.append("fresh_chat")
            raise ProviderChallengeError("Cloudflare challenge blocked fresh ChatGPT page readiness")

        async def restart(self, *_args, **_kwargs):
            calls.append("restart")
            if stage == "new_chat":
                raise RuntimeError("The prior pristine context cannot be reopened")
            raise ProviderChallengeError("Cloudflare challenge blocked role restart")

        async def new_chat(self, *_args, **_kwargs):
            calls.append("new_chat")
            raise ProviderChallengeError("Cloudflare challenge blocked new chat")

        async def send(self, *_args, **_kwargs):
            raise AssertionError("A provider challenge must never cross Send")

    control = state["controls"][-1]
    actions = Actions()
    if stage == "fresh_chat":
        asyncio.run(worker._recover_resume_sending(state, hop, control, actions))
        assert calls == ["locate", "fresh_chat"]
    else:
        asyncio.run(worker._restart_pristine_resume_role(
            state, hop, control, actions, reason="Lost proven pristine page",
        ))
        assert calls == (["restart", "new_chat"] if stage == "new_chat" else ["restart"])

    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "provider_challenge"
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "provider_challenge"
    assert not state["block_retryable"]
    assert "verification" in control["result"]["next_safe_action"].lower()
    assert state["active_hop_id"] == before_hop["hop_id"]
    assert len(state["hops"]) == 1
    assert hop["request_id"] == before_hop["request_id"]
    assert hop["prompt"] == before_hop["prompt"]
    assert hop["prompt_sha256"] == before_hop["prompt_sha256"]
    assert state["roles"]["PLAN"] == before_role
    assert ledger.path.read_bytes() == before_ledger


def test_lost_sending_provider_challenge_preserves_exact_nonacceptance(tmp_path):
    from test_operator_resume_recovery import _prepare_lost_sending_record

    _store, state, worker, _path, hop, ledger, _donor, _binding, _baseline = (
        _prepare_lost_sending_record(
            tmp_path, task_id="provider-challenge-lost-sending",
            error=(
                "ComposerConflictError: composer changed or became unavailable "
                "inside the atomic send boundary"
            ),
        )
    )
    before_ledger = ledger.path.read_bytes()
    before_role = copy.deepcopy(state["roles"]["PLAN"])
    before_hop = copy.deepcopy(hop)
    calls = []

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            calls.append("locate")
            return None

        async def fresh_chat(self, *_args, **_kwargs):
            calls.append("fresh_chat")
            raise ProviderChallengeError("Cloudflare challenge blocked fresh ChatGPT page readiness")

        async def restart(self, *_args, **_kwargs):
            raise AssertionError("Provider verification must not trigger a role restart")

        async def new_chat(self, *_args, **_kwargs):
            raise AssertionError("Provider verification must not trigger another chat")

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    assert calls == ["locate", "fresh_chat"]
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "provider_challenge"
    assert control["result"]["action"] == "reacquire_sending_role"
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "provider_challenge"
    assert not state["block_retryable"]
    assert "verification" in control["result"]["next_safe_action"].lower()
    assert state["active_hop_id"] == before_hop["hop_id"]
    assert len(state["hops"]) == 1
    assert hop["request_id"] == before_hop["request_id"]
    assert hop["prompt"] == before_hop["prompt"]
    assert hop["prompt_sha256"] == before_hop["prompt_sha256"]
    assert state["roles"]["PLAN"] == before_role
    assert ledger.path.read_bytes() == before_ledger
