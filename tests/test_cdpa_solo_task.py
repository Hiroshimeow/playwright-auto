from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_cli as cli_module
import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.chatgpt import MessageBaseline, MessageSnapshot, PageBinding, SendReceipt, prompt_digest
from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto.cdpa_store import TaskStore, task_goal_for_hop
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.durable import RequestLedger, RequestStatus
from playwright_auto.chatgpt_errors import UnsafePageStateError
from playwright_auto.dashboard_api import DashboardAPI

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions


def make_store(tmp_path: Path) -> tuple[object, TaskStore]:
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return config, TaskStore(config)


def active_hop(state: dict) -> dict:
    return next(
        hop for hop in state["hops"]
        if hop["hop_id"] == state["active_hop_id"]
    )


def test_team_task_bootstrap_remains_plan_first(tmp_path: Path):
    _config, store = make_store(tmp_path)

    state = store.create_task(
        "Team behavior stays unchanged",
        requested_team="team-mode",
        task_id="team-mode",
        governing_objective="Team behavior stays unchanged",
        governing_directives=[],
    )

    assert state["workflow_mode"] == "team"
    assert state["active_role"] == "PLAN"
    assert active_hop(state)["target_role"] == "PLAN"
    assert state["roles"]["PLAN"]["status"] == "pending"


@pytest.mark.parametrize("role", ["DEV", "AUDIT", "REVIEW"])
def test_solo_task_bootstraps_exact_selected_role_and_endpoint(
    tmp_path: Path, role: str
):
    _config, store = make_store(tmp_path)
    conversation_url = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"

    state = store.create_task(
        "Solo work",
        requested_team=f"solo-{role.lower()}",
        task_id=f"solo-{role.lower()}",
        workflow_mode="solo",
        roles=(role,),
        goal="Inspect the current state",
        browser_endpoint_id="9222",
        conversation_url=conversation_url,
    )

    hop = active_hop(state)
    assert state["workflow_mode"] == "solo"
    assert list(state["roles"]) == [role]
    assert state["active_role"] == role
    assert state["roles"][role]["status"] == "pending"
    assert hop["target_role"] == role
    assert hop["physical_role"] == state["roles"][role]["physical_role"]
    assert hop["conversation_url"] == conversation_url
    assert state["required_browser_endpoint_id"] == state["browser_endpoint_id"]
    assert task_goal_for_hop(state, 1) == "Inspect the current state"


def test_solo_goal_may_be_blank_and_does_not_fall_back_to_name(tmp_path: Path):
    _config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo name",
        task_id="solo-blank-goal",
        workflow_mode="solo",
        roles=("DEV",),
        goal="",
        browser_endpoint_id="9222",
    )

    assert task_goal_for_hop(state, 1) == ""


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ((), "exactly one"),
        (("DEV", "AUDIT"), "exactly one"),
    ],
)
def test_solo_requires_exactly_one_role(
    tmp_path: Path, roles: tuple[str, ...], message: str
):
    _config, store = make_store(tmp_path)

    with pytest.raises(ValueError, match=message):
        store.create_task(
            "Bad solo",
            task_id="bad-solo-" + str(len(roles)),
            workflow_mode="solo",
            roles=roles,
            browser_endpoint_id="9222",
        )


def test_solo_rejects_unknown_role(tmp_path: Path):
    _config, store = make_store(tmp_path)

    with pytest.raises(ValueError, match="unknown workflow roles"):
        store.create_task(
            "Bad role",
            task_id="solo-bad-role",
            workflow_mode="solo",
            roles=("UNKNOWN",),
            browser_endpoint_id="9222",
        )


def test_solo_requires_explicit_browser_endpoint(tmp_path: Path):
    _config, store = make_store(tmp_path)

    with pytest.raises(ValueError, match="browser endpoint"):
        store.create_task(
            "Missing endpoint",
            task_id="solo-no-endpoint",
            workflow_mode="solo",
            roles=("DEV",),
        )


def test_solo_rejects_non_conversation_url(tmp_path: Path):
    _config, store = make_store(tmp_path)

    with pytest.raises(ValueError, match="conversation URL"):
        store.create_task(
            "Bad URL",
            task_id="solo-bad-url",
            workflow_mode="solo",
            roles=("DEV",),
            browser_endpoint_id="9222",
            conversation_url="https://chatgpt.com/",
        )


def test_solo_prompt_initial_and_continuation_are_minimal(tmp_path: Path):
    config, _store = make_store(tmp_path)
    builder = PromptBuilder(config)

    initial = builder.build_solo(
        task_name="Solo review",
        logical_role="REVIEW",
        goal="Check the patch",
        continuation=False,
    )
    continuation = builder.build_solo(
        task_name="Solo review",
        logical_role="REVIEW",
        goal="",
        continuation=True,
    )

    assert initial.constructor_included is False
    assert initial.text == (
        "You are REVIEW for the solo task Solo review.\n\n"
        "Goal: Check the patch\n\n"
        "Discover, load, and use the relevant skills for this responsibility.\n\n"
        "Continue the work required by this task.\n\n"
        "ROUTE\n"
        "When complete, return exactly one JSON object using only:\n"
        '{"route":"DONE","handoff":"<concise final result>"}\n'
        "or\n"
        '{"route":"PAUSE","handoff":"<reason/current state>"}'
    )
    assert continuation.text == (
        "Continue from the current state.\n\n"
        "ROUTE\n"
        "Return exactly one valid JSON object using only:\n"
        '{"route":"DONE","handoff":"<concise final result>"}\n'
        "or\n"
        '{"route":"PAUSE","handoff":"<reason/current state>"}'
    )


@pytest.mark.parametrize("handoff", ["Implemented and verified focused tests.", "INLINE"])
def test_solo_live_candidate_accepts_done_before_responded_transition(
    tmp_path: Path,
    handoff: str,
):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo live candidate",
        task_id=f"solo-live-candidate-{handoff.lower().replace(' ', '-')}",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    candidate = MessageSnapshot(
        "assistant",
        "assistant-done",
        "turn-done",
        json.dumps({"route": "DONE", "handoff": handoff}),
        (),
    )

    worker._validate_response_candidate(state, hop, candidate)


def test_solo_done_accepts_concise_handoff_without_report_file(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo DEV",
        task_id="solo-done",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    hop["state"] = "responded"
    hop["response"] = json.dumps(
        {"route": "DONE", "handoff": "Implemented and verified focused tests."}
    )

    worker._responded(state, hop)

    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert hop["route"] == "DONE"
    assert hop["routed_handoff"] == "Implemented and verified focused tests."
    assert hop["report_path"] is None
    assert state["reports"] == []


def test_solo_done_accepts_inline_literal_as_concise_handoff(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo DEV inline handoff",
        task_id="solo-done-inline-handoff",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    hop["state"] = "responded"
    hop["response"] = json.dumps({"route": "DONE", "handoff": "INLINE"})

    worker._responded(state, hop)

    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert hop["route"] == "DONE"
    assert hop["routed_handoff"] == "INLINE"
    assert not any(item.get("kind") == "route_repair" for item in state["hops"])


def test_solo_invalid_role_route_repairs_same_role(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo audit",
        task_id="solo-invalid-route",
        workflow_mode="solo",
        roles=("AUDIT",),
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    hop["state"] = "responded"
    hop["response"] = json.dumps(
        {"route": "AUDIT", "handoff": "keep going"}
    )

    worker._responded(state, hop)

    repair = active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "AUDIT"
    assert len(state["roles"]) == 1
    assert "PLAN" not in state["roles"]


def test_solo_route_repair_prompt_carries_exact_error_and_candidate_without_goal(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo repair proof",
        task_id="solo-repair-proof",
        workflow_mode="solo",
        roles=("DEV",),
        goal="DO_NOT_REPEAT_SUBSTANTIVE_GOAL",
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    original = active_hop(state)
    asyncio.run(worker._pre_send(state, original, FakeActions()))
    malformed = '{"route":"PAUSE","handoff":"Provider said "unavailable""}'
    original["state"] = "responded"
    original["response"] = malformed

    worker._responded(state, original)
    repair = active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "DEV"

    asyncio.run(worker._pre_send(state, repair, FakeActions()))

    assert repair["validation_error"] in repair["prompt"]
    assert json.dumps(malformed) in repair["prompt"]
    assert "DO_NOT_REPEAT_SUBSTANTIVE_GOAL" not in repair["prompt"]
    assert repair["expected_report_path"] is None
    assert "correct only" in repair["prompt"].lower()
    assert '"route":"DONE"' in repair["prompt"]
    assert '"route":"PAUSE"' in repair["prompt"]


def test_solo_route_repair_actual_send_persists_exact_repair_prompt_once(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo repair send",
        requested_team="solo-repair-send",
        task_id="solo-repair-send",
        workflow_mode="solo",
        roles=("PLAN",),
        goal="DO_NOT_REPEAT_SUBSTANTIVE_GOAL",
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    original = active_hop(state)
    asyncio.run(worker._pre_send(state, original, FakeActions()))
    malformed = '{"route":"PAUSE","handoff":"Provider said "unavailable""}'
    original["state"] = "responded"
    original["response"] = malformed
    worker._responded(state, original)
    repair = active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))

    from test_cdpa_worker import RecordingCDPASendActions, RecordingCDPASendClient
    from playwright_auto.durable import RequestLedger, RequestStatus

    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
    actions = RecordingCDPASendActions(client)
    asyncio.run(worker._sending(state, repair, actions))

    record = RequestLedger(repair["ledger_path"]).get(repair["request_id"])
    assert client.send_calls == [repair["prompt"]]
    assert record is not None
    assert record.status is RequestStatus.SENT
    assert record.attempts == 1
    assert record.rendered_prompt == repair["prompt"]
    assert "DO_NOT_REPEAT_SUBSTANTIVE_GOAL" not in record.rendered_prompt


def test_solo_repeated_invalid_repairs_are_bounded(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo bounded repair",
        task_id="solo-bounded-repair",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)

    for _ in range(config.route_repair_attempts + 1):
        hop = active_hop(state)
        hop["state"] = "responded"
        hop["response"] = "not json"
        worker._responded(state, hop)
        if state["status"] == "BLOCKED":
            break

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "route_validation_exhausted"
    assert state["block_retryable"] is True
    assert len(state["hops"]) == config.route_repair_attempts + 1
    assert active_hop(state)["repair_attempt"] == config.route_repair_attempts


def test_dashboard_api_normalizes_solo_to_existing_role_and_hard_endpoint(
    tmp_path: Path,
):
    config, _store = make_store(tmp_path)
    app = DashboardAPI(config)

    payload = app.normalize_create(
        {
            "workflow_mode": "solo",
            "task": "Solo API",
            "requested_team": "solo-api",
            "role": "REVIEW",
            "goal": "",
            "browser_endpoint_id": "9222",
            "conversation_url": "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
        }
    )

    assert payload["workflow_mode"] == "solo"
    assert payload["roles"] == ["REVIEW"]
    assert payload["goal"] == ""
    assert payload["browser_endpoint_id"] == payload["required_browser_endpoint_id"]
    assert payload["conversation_url"].startswith("https://chatgpt.com/c/")
    assert payload["bootstrap_id"] is None
    assert payload["context_policy"] == "fresh"


def test_dashboard_api_existing_solo_does_not_require_task_or_team(tmp_path: Path):
    config, _store = make_store(tmp_path)
    app = DashboardAPI(config)

    payload = app.normalize_create(
        {
            "workflow_mode": "solo",
            "role": "REVIEW",
            "goal": "",
            "browser_endpoint_id": "9222",
            "conversation_url": "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
        }
    )

    assert payload["workflow_mode"] == "solo"
    assert payload["requested_team"] is None
    assert payload["task"].startswith("Continue existing REVIEW conversation ")


def test_cli_taskless_existing_solo_is_creation_not_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: list[dict] = []
    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "load_cdpa_config",
        lambda _path, *, repository_root: config,
    )
    monkeypatch.setattr(
        cli_module,
        "submit_task",
        lambda _config, **kwargs: captured.append(kwargs) or {"status": "queued"},
    )

    assert cli_module.main(
        [
            "--solo",
            "AUDIT",
            "--browser",
            "9222",
            "--conversation-url",
            "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
        ]
    ) == 0
    assert captured[0]["task"] == ""
    assert captured[0]["team"] is None
    assert captured[0]["workflow_mode"] == "solo"
    assert captured[0]["solo_role"] == "AUDIT"


def test_cli_submit_task_adds_solo_fields_without_changing_team_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config, _store = make_store(tmp_path)
    payloads: list[dict] = []

    def fake_post(_config, _path, payload, **_kwargs):
        payloads.append(payload)
        return {"task_id": "x", "status": "INBOX"}

    monkeypatch.setattr(cli_module, "_post", fake_post)

    cli_module.submit_task(
        config,
        task="Team",
        repository=tmp_path,
        team="team",
        new_roles=(),
        new_all=False,
        governing_objective="Team",
        governing_directives=[],
    )
    cli_module.submit_task(
        config,
        task="Solo",
        repository=tmp_path,
        team="solo",
        new_roles=(),
        new_all=False,
        workflow_mode="solo",
        solo_role="AUDIT",
        goal="Check",
        browser_endpoint_id="9222",
        conversation_url="https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
    )

    assert "workflow_mode" not in payloads[0]
    assert "role" not in payloads[0]
    assert payloads[1]["workflow_mode"] == "solo"
    assert payloads[1]["role"] == "AUDIT"
    assert payloads[1]["goal"] == "Check"
    assert payloads[1]["conversation_url"].startswith("https://chatgpt.com/c/")


def test_solo_pause_resume_reuses_concise_handoff_and_same_role(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo paused audit",
        task_id="solo-pause-resume",
        workflow_mode="solo",
        roles=("AUDIT",),
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    hop["state"] = "responded"
    hop["response"] = json.dumps(
        {"route": "PAUSE", "handoff": "Waiting for an external artifact."}
    )
    worker._responded(state, hop)
    state["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "status": "requested",
            "origin": "operator",
            "role": "AUDIT",
        }
    ]

    asyncio.run(worker._apply_control(state, SimpleNamespace()))

    resumed = active_hop(state)
    assert resumed["target_role"] == "AUDIT"
    assert resumed["source_role"] == "AUDIT"
    assert resumed["handoff"] == "Waiting for an external artifact."
    assert "PLAN" not in state["roles"]


def test_solo_new_chat_uses_initial_prompt_once_and_persists_canonical_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo fresh DEV",
        task_id="solo-fresh-dev",
        workflow_mode="solo",
        roles=("DEV",),
        goal="Implement the requested change",
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    role = "DEV"
    physical = state["roles"][role]["physical_role"]
    conversation_id = "10101010-2020-3030-4040-505050505050"
    conversation_url = f"https://chatgpt.com/c/{conversation_id}"

    class Client:
        def __init__(self):
            self.binding = PageBinding("solo-fresh-page", physical)
            self.sent = False

        async def assert_ownership(self):
            url = conversation_url if self.sent else "https://chatgpt.com/"
            return SimpleNamespace(
                conversation_url=conversation_url if self.sent else None,
                url=url,
            )

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="solo-fresh-page",
        url="https://chatgpt.com/",
        created=True,
        new_chat=True,
    )

    class Actions:
        def __init__(self):
            self.fresh_calls = 0

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def fresh_chat(self, *_args, **_kwargs):
            self.fresh_calls += 1
            return acquired

    actions = Actions()

    async def no_rate_limit(_acquired):
        return None

    async def ready(*_args, **_kwargs):
        return SimpleNamespace(reason="ready")

    async def owned(*_args, **_kwargs):
        return acquired

    run_prompts: list[str] = []

    class FakeDurableSendBlock:
        def __init__(self, prompt, **_kwargs):
            self.prompt = str(prompt)

        async def run(self, _context):
            run_prompts.append(self.prompt)
            client.sent = True
            receipt = SendReceipt(
                prompt=self.prompt,
                prompt_sha256=prompt_digest(self.prompt),
                binding=client.binding,
                baseline=MessageBaseline(
                    frozenset(),
                    frozenset(),
                    frozenset(),
                    frozenset(),
                ),
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before="solo-fresh-session",
                user_message_id="solo-user-1",
                user_turn_id="solo-turn-1",
                conversation_id=conversation_id,
            )
            return {
                "receipt": receipt.to_dict(),
                "record": {"accepted_at": time.time()},
            }

    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker.role_controller, "run", ready)
    monkeypatch.setattr(worker, "_owned_or_block", owned)
    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeDurableSendBlock)

    asyncio.run(worker._pre_send(state, hop, actions))

    initial_prompt = hop["prompt"]
    assert actions.fresh_calls == 1
    assert hop["state"] == "sending"
    assert initial_prompt.startswith("You are DEV for the solo task Solo fresh DEV.")
    assert "Goal: Implement the requested change" in initial_prompt
    assert "Continue from the current state." not in initial_prompt

    asyncio.run(worker._sending(state, hop, actions))

    assert run_prompts == [initial_prompt]
    assert hop["state"] == "sent"
    assert hop["receipt"]["attempts"] == 1
    assert hop["conversation_url"] == conversation_url
    assert state["roles"]["DEV"]["page_url"] == conversation_url
    assert state["required_browser_endpoint_id"] == state["browser_endpoint_id"]


def test_solo_existing_responding_url_enters_wait_without_send(tmp_path: Path, monkeypatch):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
    state = store.create_task(
        "Solo responding",
        task_id="solo-adopt-responding",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
        conversation_url=url,
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="page-adopted",
        url=url,
        created=True,
        new_chat=False,
        conversation_id=url.rsplit("/", 1)[-1],
    )

    class Actions:
        async def adopt_conversation(self, *_args, **_kwargs):
            return acquired

    async def no_rate_limit(_acquired):
        return None

    async def responding(*_args, **_kwargs):
        return SimpleNamespace(reason="response_in_progress")

    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker.role_controller, "run", responding)

    asyncio.run(worker._pre_send(state, hop, Actions()))

    assert hop["state"] == "waiting"
    assert hop["receipt"] is None
    assert hop["prompt"] is None
    assert hop["observed_existing_response"] is True
    assert "adopted_existing_response" not in hop
    assert state["active_action"] == "wait_response"


def test_solo_existing_url_send_race_observes_active_response_then_retries_goal(
    tmp_path: Path, monkeypatch
):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/solo-send-race"
    state = store.create_task(
        "Solo send race",
        task_id="solo-send-race",
        workflow_mode="solo",
        roles=("DEV",),
        goal="CURRENT GOAL MUST SEND AFTER OLD RESPONSE",
        browser_endpoint_id="9222",
        conversation_url=url,
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="page-send-race",
        url=url,
        created=False,
        new_chat=False,
        conversation_id=url.rsplit("/", 1)[-1],
    )

    class Actions:
        async def adopt_conversation(self, *_args, **_kwargs):
            return acquired

        async def acquire(self, *_args, **_kwargs):
            return acquired

    async def no_rate_limit(_acquired):
        return None

    async def ready(*_args, **_kwargs):
        return SimpleNamespace(reason="ready")

    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker.role_controller, "run", ready)

    asyncio.run(worker._pre_send(state, hop, Actions()))
    assert hop["state"] == "sending"

    constructor = worker_module.task_workflow_definitions(state, config)["DEV"]["system_prompt"]
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": state["manifest_path"],
        },
        role_prompt_hash=worker_module._sha(constructor),
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0

    async def owned(*_args, **_kwargs):
        return acquired

    async def active_before_click(_block, _context):
        raise UnsafePageStateError("page is already responding")

    monkeypatch.setattr(worker, "_owned_or_block", owned)
    monkeypatch.setattr(worker_module.DurableSendBlock, "run", active_before_click)

    asyncio.run(worker._sending(state, hop, Actions()))

    durable = ledger.get(hop["request_id"])
    assert durable is not None
    assert durable.status is RequestStatus.NEW
    assert durable.attempts == 0
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_response"
    assert hop["state"] == "waiting"
    assert hop["observed_existing_response"] is True
    assert hop.get("receipt") is None

    asyncio.run(
        worker._waiting(
            state,
            hop,
            Actions(),
            Path(state["manifest_path"]),
        )
    )
    assert hop["state"] == "pre_send"
    assert "observed_existing_response" not in hop
    assert state["active_action"] == "queued"
    assert worker._pre_send_request_identity_is_safe(state, hop) is True
    assert state["status"] != "BLOCKED"


def test_solo_existing_idle_url_sends_full_first_goal_prompt(tmp_path: Path, monkeypatch):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    state = store.create_task(
        "Solo idle",
        task_id="solo-adopt-idle",
        workflow_mode="solo",
        roles=("REVIEW",),
        goal="Review only the current state",
        browser_endpoint_id="9222",
        conversation_url=url,
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="page-idle",
        url=url,
        created=True,
        new_chat=False,
        conversation_id=url.rsplit("/", 1)[-1],
    )

    clean_calls = []

    class Actions:
        async def adopt_conversation(self, *_args, **_kwargs):
            return acquired

        async def acquire(self, *_args, **kwargs):
            clean_calls.append(kwargs.get("clean_for_send"))
            return acquired

    async def no_rate_limit(_acquired):
        return None

    async def ready(*_args, **_kwargs):
        return SimpleNamespace(reason="ready")

    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker.role_controller, "run", ready)

    asyncio.run(worker._pre_send(state, hop, Actions()))

    assert hop["state"] == "sending"
    assert hop["receipt"] is None
    assert clean_calls == [True]
    assert "Review only the current state" in hop["prompt"]
    assert "You are REVIEW for the solo task" in hop["prompt"]
    assert not hop["prompt"].startswith("Continue from the current state.")


def test_solo_foreign_conversation_owner_blocks_before_adoption(tmp_path: Path):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444"
    owner = store.create_task(
        "Owner",
        requested_team="owner",
        task_id="solo-owner",
        governing_objective="Owner",
        governing_directives=[],
    )
    owner["roles"]["PLAN"]["page_id"] = "owner-page"
    owner["roles"]["PLAN"]["page_url"] = url
    store.save(owner["manifest_path"], owner)
    state = store.create_task(
        "Solo duplicate",
        task_id="solo-duplicate",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
        conversation_url=url,
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)

    class Actions:
        async def adopt_conversation(self, *_args, **_kwargs):
            raise AssertionError("foreign conversation must not be adopted")

    asyncio.run(worker._pre_send(state, hop, Actions()))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "conversation_owned_by_other_task"
    assert "solo-owner" in state["block_reason"]




def test_solo_terminal_historical_conversation_owner_does_not_block_reuse(tmp_path: Path, monkeypatch):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/terminal-history-reuse"
    owner = store.create_task("Historical owner", requested_team="owner", task_id="solo-terminal-owner", governing_objective="Historical owner", governing_directives=[])
    owner["roles"]["PLAN"]["page_id"] = "owner-page"
    owner["roles"]["PLAN"]["page_url"] = url
    owner["status"] = "DONE"
    owner["terminal_state"] = "DONE"
    owner["active_role"] = None
    owner["active_hop_id"] = None
    store.save(owner["manifest_path"], owner)
    state = store.create_task(
        "Solo reuse historical", task_id="solo-reuse-terminal", workflow_mode="solo",
        roles=("DEV",), browser_endpoint_id="9222", conversation_url=url,
        goal="Reuse the historical conversation",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    acquired = AcquiredRole(client=SimpleNamespace(), page_id="new-page", url=url, created=True, new_chat=False)

    class Actions:
        async def adopt_conversation(self, *_args, **_kwargs): return acquired
        async def acquire(self, *_args, **_kwargs): return acquired

    async def no_rate_limit(_acquired): return None
    async def ready(*_args, **_kwargs): return SimpleNamespace(reason="ready")
    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker.role_controller, "run", ready)

    asyncio.run(worker._pre_send(state, hop, Actions()))
    assert state["status"] == "RUNNING"
    assert hop["state"] == "sending"
    assert "Reuse the historical conversation" in hop["prompt"]


def test_solo_never_exposes_route_plan_control(tmp_path: Path):
    _config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo control",
        task_id="solo-control",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
    )

    eligibility = workflow_control_eligibility(state, "route_plan")

    assert eligibility["eligible"] is False
    assert "Solo Task" in eligibility["reason"]


def test_solo_adopted_waiting_reuses_existing_wait_budget_without_sent_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/12121212-3434-5656-7878-909090909090"
    state = store.create_task(
        "Solo adopted wait",
        task_id="solo-adopt-wait",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
        conversation_url=url,
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="page-adopt-wait",
        url=url,
        created=True,
        new_chat=False,
        conversation_id=url.rsplit("/", 1)[-1],
    )

    class Actions:
        async def adopt_conversation(self, *_args, **_kwargs):
            return acquired

    async def no_rate_limit(_acquired):
        return None

    async def responding(*_args, **_kwargs):
        return SimpleNamespace(reason="response_in_progress")

    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker.role_controller, "run", responding)
    asyncio.run(worker._pre_send(state, hop, Actions()))

    original_deadline = hop["wait"]["deadline_at"]

    async def owned(*_args, **_kwargs):
        return acquired

    async def wait_only(*_args, **_kwargs):
        return SimpleNamespace(reason="wait_response")

    monkeypatch.setattr(worker, "_owned_or_block", owned)
    monkeypatch.setattr(worker.role_controller, "run", wait_only)
    asyncio.run(
        worker._waiting(
            state,
            hop,
            SimpleNamespace(),
            Path(state["manifest_path"]),
        )
    )

    assert hop["receipt"] is None
    assert hop["state"] == "waiting"
    assert hop["wait"]["deadline_at"] == original_deadline



def test_solo_preexisting_response_completion_is_not_consumed_and_goal_resumes(tmp_path: Path, monkeypatch):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/old-response-isolation"
    state = store.create_task(
        "Solo old response", task_id="solo-old-response", workflow_mode="solo",
        roles=("DEV",), goal="CURRENT GOAL MUST SEND", browser_endpoint_id="9222",
        conversation_url=url,
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)
    acquired = AcquiredRole(client=SimpleNamespace(), page_id="page-old", url=url, created=False, new_chat=False)

    class Actions:
        async def adopt_conversation(self, *_args, **_kwargs): return acquired
        async def acquire(self, *_args, **_kwargs): return acquired
        async def locate_owned(self, *_args, **_kwargs): return acquired
    async def no_rate_limit(_acquired): return None
    reasons = iter(("response_in_progress", "ready", "ready"))
    async def controller(*_args, **_kwargs):
        return SimpleNamespace(reason=next(reasons))
    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker.role_controller, "run", controller)

    asyncio.run(worker._pre_send(state, hop, Actions()))
    assert hop["state"] == "waiting" and hop["observed_existing_response"] is True
    asyncio.run(worker._waiting(state, hop, Actions(), Path(state["manifest_path"])))
    assert hop["state"] == "pre_send"
    assert hop.get("response") is None
    assert not state.get("route_timeline")
    asyncio.run(worker._pre_send(state, hop, Actions()))
    assert hop["state"] == "sending"
    assert "CURRENT GOAL MUST SEND" in hop["prompt"]

def test_solo_route_plan_control_path_is_explicitly_unavailable(tmp_path: Path):
    config, store = make_store(tmp_path)
    state = store.create_task(
        "Solo no PLAN",
        task_id="solo-no-plan-route",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
    )
    worker = CDPAWorker(config, store=store)
    hop = active_hop(state)

    with pytest.raises(RuntimeError, match="unavailable for Solo Task"):
        worker._route_to_plan(
            state,
            hop,
            reason="operator requested PLAN",
            kind="control",
        )

    assert state["active_role"] == "DEV"
    assert active_hop(state)["target_role"] == "DEV"
    assert "PLAN" not in state["roles"]


def test_solo_restart_role_preserves_durable_url_and_role(
    tmp_path: Path,
):
    config, store = make_store(tmp_path)
    url = "https://chatgpt.com/c/abababab-cdcd-efef-1212-343434343434"
    state = store.create_task(
        "Solo restart",
        task_id="solo-restart",
        workflow_mode="solo",
        roles=("AUDIT",),
        browser_endpoint_id="9222",
        conversation_url=url,
    )
    role = state["roles"]["AUDIT"]
    role["page_id"] = "old-page"
    role["page_url"] = url
    role["online"] = True
    state["controls"] = [
        {
            "control_id": 1,
            "action": "restart_role",
            "status": "requested",
            "origin": "operator",
            "role": "AUDIT",
        }
    ]
    worker = CDPAWorker(config, store=store)

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return SimpleNamespace()

        async def restart(self, *_args, **_kwargs):
            raise AssertionError("Solo restart must not create a fresh conversation")

        async def restart_exact(self, *_args, **_kwargs):
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id="restarted-page",
                url=url,
                created=True,
                new_chat=False,
                conversation_id=url.rsplit("/", 1)[-1],
            )

    assert asyncio.run(worker._apply_control(state, Actions())) is True

    assert state["roles"]["AUDIT"]["page_url"] == url
    assert state["roles"]["AUDIT"]["page_id"] == "restarted-page"
    assert active_hop(state)["target_role"] == "AUDIT"
    assert "PLAN" not in state["roles"]


def test_cli_parser_accepts_direct_solo_creation_flags():
    args = cli_module.build_parser().parse_args(
        [
            "Solo CLI",
            "--solo",
            "REVIEW",
            "--goal",
            "Inspect",
            "--browser",
            "9222",
            "--conversation-url",
            "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
        ]
    )

    assert args.solo == "REVIEW"
    assert args.goal == "Inspect"
    assert args.browser == "9222"
    assert args.conversation_url.startswith("https://chatgpt.com/c/")


def test_create_task_ui_exposes_team_and_solo_without_is_running():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/playwright_auto/dashboard.html").read_text(encoding="utf-8")
    app = (root / "src/playwright_auto/dashboard_assets/app.js").read_text(encoding="utf-8")

    assert 'name="workflow_mode"' in html
    assert "Team Task" in html
    assert "Solo Task" in html
    assert 'name="solo_role"' in html
    assert 'name="goal"' in html
    assert 'name="conversation_url"' in html
    assert "workflow_mode: \"solo\"" in app
    assert "existingSolo" in app
    assert "roots.taskInput.required = !existingSolo" in app
    assert "roots.taskDetails.hidden = existingSolo" in app
    assert "roots.requestedTeamField.hidden = existingSolo" in app
    assert 'data-create-section="team-repository" data-team-only' in html
    assert 'data-solo-browser-slot' in html
    assert "roots.soloBrowserSlot.append(roots.browserEndpointField)" in app
    assert '/assets/app.js?v=20261011-p0012-challenge-confirm-v1' in html
    assert "is_running" not in html.casefold()
    assert "is_running" not in app.casefold()
