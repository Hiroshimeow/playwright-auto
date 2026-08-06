from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole, BranchBootstrapError
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.durable import RequestLedger

from test_cdpa_core import write_config
from test_cdpa_worker import (
    FakeActions,
    RecordingCDPASendActions,
    RecordingCDPASendClient,
    bootstrap_record,
)


class BootstrapActions(FakeActions):
    def __init__(self) -> None:
        super().__init__()
        self.branch_calls: list[str] = []

    async def locate_owned(self, _state, _role):
        return None

    async def branch_from_anchor(
        self,
        _state,
        role,
        *,
        source_conversation_id,
        assistant_message_id,
    ):
        self.branch_calls.append(role)
        return AcquiredRole(
            client=object(),
            page_id=f"branch-{role.lower()}",
            url=f"https://chatgpt.com/c/{role.lower()}-branch",
            created=True,
            new_chat=True,
        )


def _setup(tmp_path: Path, *, bootstrap=None, roles=None):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Prompt compression task",
        requested_team="alpha",
        task_id="task-prompt-compression",
        roles=roles,
        bootstrap=bootstrap,
    )
    return store, state, CDPAWorker(config, store=store)


def test_fresh_builtin_first_turn_includes_base_role_and_response_guide(tmp_path: Path):
    _store, state, worker = _setup(tmp_path)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert "TEST_BASE_CONTEXT" in hop["prompt"]
    assert "Constructor for PLAN" in hop["prompt"]
    assert "Return only the strict route JSON." in hop["prompt"]


def test_inherited_builtin_omits_base_and_exact_send_payload_stays_identical(tmp_path: Path):
    _store, state, worker = _setup(tmp_path, bootstrap=bootstrap_record())
    hop = _active_hop(state)
    actions = BootstrapActions()

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.branch_calls == ["PLAN"]
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_native"
    assert state["roles"]["PLAN"]["conversation_generation"] == 1
    assert "TEST_BASE_CONTEXT" not in hop["prompt"]
    assert "Constructor for PLAN" in hop["prompt"]
    assert "Return only the strict route JSON." in hop["prompt"]

    state["roles"]["PLAN"]["page_id"] = "page-PLAN"
    state["roles"]["PLAN"]["page_url"] = "https://chatgpt.com/c/cdpa-send"
    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert client.send_calls == [hop["prompt"]]
    assert hop["receipt"]["prompt"] == hop["prompt"]
    assert record is not None
    assert record.rendered_prompt == hop["prompt"]


def test_selected_bootstrap_exhaustion_waits_without_fresh_base_context(tmp_path: Path, monkeypatch):
    _store, state, worker = _setup(tmp_path, bootstrap=bootstrap_record())

    class FailedBootstrapActions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise BranchBootstrapError("native unavailable")

        async def acquire(self, *_args, **_kwargs):
            raise AssertionError("selected bootstrap must not open Fresh context")

    async def ui_failure(*_args, **_kwargs):
        raise worker_module.BootstrapUIBranchError("ui unavailable")

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", ui_failure)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FailedBootstrapActions()))

    assert state["status"] == "WAITING"
    assert state["waiting_code"] == "bootstrap_repair"
    assert state["waiting"]["bootstrap_repair_error"] == "Bootstrap Keeper is unavailable"
    assert state["roles"]["PLAN"].get("context_source") is None
    assert hop["state"] == "pre_send"
    assert hop.get("prompt") is None


def test_generation_two_after_inherited_new_chat_restores_base_context(tmp_path: Path):
    _store, state, worker = _setup(tmp_path, bootstrap=bootstrap_record())
    first = _active_hop(state)
    actions = BootstrapActions()
    asyncio.run(worker._pre_send(state, first, actions))
    assert state["roles"]["PLAN"]["conversation_generation"] == 1
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_native"

    worker._record_acquired(
        state,
        "PLAN",
        AcquiredRole(
            client=object(),
            page_id="fresh-plan-generation-2",
            url="https://chatgpt.com/c/fresh-plan-generation-2",
            created=True,
            new_chat=True,
        ),
    )
    assert state["roles"]["PLAN"]["conversation_generation"] == 2

    second = worker._append_hop(
        state,
        source_role="REVIEW",
        target_role="PLAN",
        handoff="generation two",
    )
    asyncio.run(worker._pre_send(state, second, FakeActions()))

    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_native"
    assert "TEST_BASE_CONTEXT" in second["prompt"]
    assert "Constructor for PLAN" in second["prompt"]


def test_custom_workflow_prompt_is_preserved_even_when_bootstrap_inherited(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    custom = store.create_workflow_agent(
        display_name="Researcher",
        system_prompt="CUSTOM_BOOTSTRAP_PROMPT_EXACT",
        external_command_id="cmd-custom-bootstrap-prompt",
    )
    route = custom["route_key"]
    state = store.create_task(
        "Custom bootstrap prompt",
        requested_team="custom-bootstrap",
        task_id="task-custom-bootstrap-prompt",
        roles=("PLAN", route, "REVIEW"),
        bootstrap=bootstrap_record(),
    )
    hop = _active_hop(state)
    hop["target_role"] = route
    hop["physical_role"] = state["roles"][route]["physical_role"]
    state["active_role"] = route
    worker = CDPAWorker(config, store=store)

    asyncio.run(worker._pre_send(state, hop, BootstrapActions()))

    assert state["roles"][route]["context_source"] == "bootstrap_native"
    assert state["roles"][route]["conversation_generation"] == 1
    assert "CUSTOM_BOOTSTRAP_PROMPT_EXACT" in hop["prompt"]
    assert "TEST_BASE_CONTEXT" not in hop["prompt"]
    assert "Return only the strict route JSON." in hop["prompt"]


def test_production_prompt_split_keeps_common_context_out_of_role_files():
    root = Path(__file__).resolve().parents[1]
    base = (root / "prompts" / "cdpa" / "BASE_CONTEXT.md").read_text(encoding="utf-8")
    guide = (root / "prompts" / "cdpa" / "RESPONSE_GUIDE.md").read_text(encoding="utf-8")

    assert "Superpower" in base
    assert "AGENTS.md" in base
    assert "LEARNING.md" in base
    assert "cdpa.yaml" in base and "only when" in base
    assert "preserve" in base.lower() and "dirty" in base.lower()

    for role in ("PLAN", "DEV", "TEST", "REVIEW", "AUDIT"):
        text = (root / "prompts" / "cdpa" / f"{role}.md").read_text(encoding="utf-8")
        assert "AGENTS.md" not in text
        assert "Superpower" not in text
        assert "cdpa.yaml" not in text
        if role == "PLAN":
            assert "cdpa_learning" in text
        else:
            assert "LEARNING.md" not in text

    assert '"route":"PLAN|DEV|TEST|REVIEW|AUDIT|DONE"' in guide
    assert "Only PLAN may use `DONE`" in guide
    assert "REVIEW and AUDIT must route clean work back to PLAN" in guide
