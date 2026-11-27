from pathlib import Path
import copy
import json

import pytest

from playwright_auto.chatgpt_history_tail import validate_native_history_tail

FIXTURES=json.loads((Path(__file__).parent / "fixtures/native_history_tail.json").read_text(encoding="utf-8"))

@pytest.mark.parametrize("case",FIXTURES["cases"],ids=lambda case:case["name"])
def test_native_history_tail_contract(case):
    current=copy.deepcopy(FIXTURES["bases"][case["base"]])
    for change in case["changes"]:
        cursor=current
        for part in change["path"][:-1]:
            cursor=cursor[part]
        cursor[change["path"][-1]]=copy.deepcopy(change["value"])
    before=copy.deepcopy(current)
    result=validate_native_history_tail(
        current["evidence"],current["snapshot"],
        latest_accepted_user_id=current["expected"],
    )
    assert result["verified"] is case["verified"],result
    assert current==before
    if not case["verified"]:
        assert case["reason"] in result["reason_codes"],result
        assert result["evidence_sha256"] is None
    else:
        assert result["reason_codes"]==[]
        assert len(result["evidence_sha256"])==64


def _collector_fixture(monkeypatch, *, challenges=(False, False), changed=False):
    from playwright_auto import chatgpt
    from playwright_auto.workspace import CLOUDFLARE_CHALLENGE_DOM
    from types import SimpleNamespace

    base=copy.deepcopy(FIXTURES["bases"]["dev-0"])
    if changed:
        base["snapshot"]["page_task_id"]="foreign-task"

    class ReadOnlyPage:
        url=base["snapshot"]["url"]

        def __init__(self):
            self.calls=[]
            self.challenges=list(challenges)

        async def evaluate(self, script):
            self.calls.append(script)
            assert ".click(" not in script and ".setItem(" not in script
            if script==CLOUDFLARE_CHALLENGE_DOM:
                value=self.challenges.pop(0)
                if isinstance(value,Exception):
                    raise value
                return value
            assert "chatgpt_virtual_tail_v1" in script
            assert "__ACTIVE_CHAT_ROOT__" not in script
            return copy.deepcopy(base["evidence"])

    page=ReadOnlyPage()
    async def inspect(actual):
        assert actual is page
        return SimpleNamespace(to_dict=lambda:copy.deepcopy(base["snapshot"]))
    monkeypatch.setattr(chatgpt,"inspect_chatgpt_page",inspect)
    return page,base


def _collect(page, **overrides):
    import asyncio
    from playwright_auto.chatgpt_history_tail import collect_stopped_writer_observation

    kwargs={"endpoint_id":"fixture-endpoint","endpoint_cdp_url":"http://127.0.0.1:9999",
            "physical_browser_id":"devtools:fixture-browser","target_id":"fixture-target"}
    kwargs.update(overrides)
    return asyncio.run(collect_stopped_writer_observation(page,**kwargs))


def test_collector_reads_exact_page_and_returns_untrusted_raw_tail(monkeypatch):
    page,base=_collector_fixture(monkeypatch)
    result=_collect(page)
    assert result["endpoint_id"]=="fixture-endpoint"
    assert result["target_id"]=="fixture-target"
    assert result["physical_browser_id"]=="devtools:fixture-browser"
    assert result["document_ready_state"]=="complete"
    assert result["storage_readable"] is True
    assert result["provider_challenge"] is False
    assert result["observed_at"]>0
    assert "history_tail_verified" not in result
    assert result["history_tail"]==base["evidence"]
    assert result["snapshot"]==base["snapshot"]
    assert len(page.calls)==3
    assert validate_native_history_tail(
        result["history_tail"],result["snapshot"],latest_accepted_user_id=base["expected"],
    )["verified"] is True


@pytest.mark.parametrize("challenges", [(True,False),(False,True)])
def test_collector_keeps_challenge_before_or_after_native_read(monkeypatch,challenges):
    page,_=_collector_fixture(monkeypatch,challenges=challenges)
    assert _collect(page)["provider_challenge"] is True


@pytest.mark.parametrize("challenges", [("unknown",False),(False,None)])
def test_collector_rejects_unknown_challenge_state(monkeypatch,challenges):
    page,_=_collector_fixture(monkeypatch,challenges=challenges)
    with pytest.raises(ValueError,match="challenge state is unknown"):
        _collect(page)


def test_collector_propagates_dom_read_failure(monkeypatch):
    page,_=_collector_fixture(monkeypatch,challenges=(RuntimeError("detached"),False))
    with pytest.raises(RuntimeError,match="detached"):
        _collect(page)


def test_collector_requires_complete_outer_identity_without_acquiring_page(monkeypatch):
    page,_=_collector_fixture(monkeypatch)
    with pytest.raises(ValueError,match="pinned page context"):
        _collect(page,target_id="")
    assert page.calls==[]


def test_collector_identity_changed_between_tail_and_native_read_stays_unverified(monkeypatch):
    page,base=_collector_fixture(monkeypatch,changed=True)
    result=_collect(page)
    assert validate_native_history_tail(
        result["history_tail"],result["snapshot"],latest_accepted_user_id=base["expected"],
    )["verified"] is False
