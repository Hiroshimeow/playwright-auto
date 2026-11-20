"""Temporary Chat must never reload, and every new context must be self-contained."""
import asyncio

import pytest

from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto.chatgpt import ChatGPTPage, UnsafePageStateError


def test_temporary_solo_initial_has_goal_workspace():
    prompt = PromptBuilder(None).build_solo(
        task_name="content-co", logical_role="DEV",
        goal="Ship a real content engine", workspace=r"E:\git-project\cdpa-plugin-content-company",
        temporary_context=True,
    ).text
    assert "Ship a real content engine" in prompt
    assert r"E:\git-project\cdpa-plugin-content-company" in prompt
    assert "TEMPORARY CHAT" in prompt


def test_temporary_solo_continuation_is_self_contained():
    prompt = PromptBuilder(None).build_solo(
        task_name="content-co", logical_role="DEV",
        goal="Ship a real content engine", workspace=r"E:\git-project\cdpa-plugin-content-company",
        temporary_context=True, continuation=True, recovery_handoff="resume from durable ledger",
    ).text
    assert "Ship a real content engine" in prompt
    assert "resume from durable ledger" in prompt
    assert r"E:\git-project\cdpa-plugin-content-company" in prompt
    assert "never replay accepted" in prompt
    assert '"route":"DONE"' in prompt


def test_persistent_solo_continuation_keeps_compact_contract():
    prompt = PromptBuilder(None).build_solo(
        task_name="persistent", logical_role="DEV", goal="Keep goal private",
        workspace="repo", continuation=True
    ).text
    assert "Continue from the current state" in prompt
    assert "Keep goal private" not in prompt


def test_temporary_client_refresh_is_denied_before_browser_access():
    class FakePage:
        pass
    client = ChatGPTPage(FakePage())
    client.temporary_chat = True
    with pytest.raises(UnsafePageStateError, match="Temporary Chat cannot be refreshed"):
        asyncio.run(client.refresh())
