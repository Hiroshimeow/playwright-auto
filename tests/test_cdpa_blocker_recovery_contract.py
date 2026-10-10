from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto import cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_cli import main
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.cdpa_workflow_agents import task_workflow_definitions
from playwright_auto.chatgpt import MessageSnapshot
from playwright_auto.cdpa_store import (
    TaskStore,
    canonicalize_task_text,
    normalize_governing_contract,
)
from playwright_auto.dashboard_api import DashboardAPI
from playwright_auto.durable import RequestLedger
from test_cdpa_core import _stop_task, write_config
from test_cdpa_worker import _prepare_sent_waiting_task


def _store(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return TaskStore(config)


def test_task_admission_is_lossless_except_newlines_and_persists_governing_provenance(tmp_path):
    store = _store(tmp_path)
    raw = "  alpha\r\n\rbravo\n--config fake\nΩ  "
    task = store.create_task(
        raw,
        task_id="lossless-admission",
        governing_objective="  Keep Ω\r\nexact  ",
        governing_directives=["First\rline", "  second  "],
        browser_endpoint_id=None,
    )

    expected_text = "  alpha\n\nbravo\n--config fake\nΩ  "
    assert canonicalize_task_text(raw) == expected_text
    assert task["task_text"] == expected_text
    assert task["task_text_sha256"] == hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    assert task["governing_semantic_payload"] == (
        '{"objective":"  Keep Ω\\nexact  ","directives":["First\\nline","  second  "]}'
    )
    assert task["governing_semantic_sha256"] == hashlib.sha256(
        task["governing_semantic_payload"].encode("utf-8")
    ).hexdigest()
    assert task["admission"]["browser_endpoint_explicit"] is False
    assert task["admission"]["conversation_mode_explicit"] is False
    assert task["admission"]["context_policy_explicit"] is False
    assert task["endpoint_pinned"] is False


@pytest.mark.parametrize("value", ["", " \t\r\n "])
def test_task_admission_rejects_whitespace_only(value):
    with pytest.raises(ValueError, match="task must not be empty"):
        canonicalize_task_text(value)


def test_governing_contract_requires_explicit_valid_fields_and_exact_bound():
    with pytest.raises(ValueError, match="governing_contract_required"):
        normalize_governing_contract(None, [])
    with pytest.raises(ValueError, match="governing_contract_required"):
        normalize_governing_contract("objective", None)
    with pytest.raises(ValueError, match="governing_contract_required"):
        normalize_governing_contract("objective", [" "])

    payload, digest = normalize_governing_contract("x", [])
    assert payload == '{"objective":"x","directives":[]}'
    assert digest == hashlib.sha256(payload.encode("utf-8")).hexdigest()

    payload_4095, _ = normalize_governing_contract("x" * 4063, [])
    payload_4096, _ = normalize_governing_contract("x" * 4064, [])
    assert len(payload_4095) == 4095
    assert len(payload_4096) == 4096
    with pytest.raises(ValueError, match="governing_contract_required"):
        normalize_governing_contract("x" * 4065, [])

    reordered = {"directives": ["A\\B", "--config fake", "Header: value"], "objective": 'quote " stays data'}
    payload_a, digest_a = normalize_governing_contract(
        reordered["objective"], reordered["directives"]
    )
    payload_b, digest_b = normalize_governing_contract(
        **{"directives": reordered["directives"], "objective": reordered["objective"]}
    )
    assert payload_a == payload_b
    assert digest_a == digest_b
    assert json.loads(payload_a) == {
        "objective": 'quote " stays data',
        "directives": ["A\\B", "--config fake", "Header: value"],
    }


def test_api_create_preserves_semantic_text_and_explicit_provenance(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    api = DashboardAPI(config)
    normalized = api.normalize_create(
        {
            "task": "  line1\r\nline2  ",
            "governing_objective": "  objective\rmore  ",
            "governing_directives": ["first", " second "],
            "requested_team": "alpha",
            "repository": str(tmp_path),
            "temporary": True,
            "browser_endpoint_id": "9222",
        }
    )
    assert normalized["task"] == "  line1\nline2  "
    assert normalized["governing_objective"] == "  objective\nmore  "
    assert normalized["governing_directives"] == ["first", " second "]
    provenance = normalized["admission_provenance"]
    assert provenance["requested_team_explicit"] is True
    assert provenance["repository_explicit"] is True
    assert provenance["browser_endpoint_explicit"] is True
    assert provenance["conversation_mode_explicit"] is True


def test_cli_structured_admission_file_is_lossless_and_does_not_mix_options(
    tmp_path, monkeypatch
):
    envelope = tmp_path / "admission.json"
    envelope.write_text(
        json.dumps(
            {
                "task": "  first\r\n--config fake\nΩ  ",
                "governing_objective": "  keep\rthis  ",
                "governing_directives": ["A\r\nB", " C "],
                "requested_team": "alpha",
                "repository": str(tmp_path),
                "temporary": True,
                "browser_endpoint_id": "9222",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_submit(_config, **kwargs):
        captured.update(kwargs)
        return {"task_id": "x", "status": "INBOX"}

    monkeypatch.setattr("playwright_auto.cdpa_cli.submit_task", fake_submit)
    assert main(["--admission-file", str(envelope), "--config", str(write_config(tmp_path))]) == 0
    assert captured["task"] == "  first\n--config fake\nΩ  "
    assert captured["governing_objective"] == "  keep\nthis  "
    assert captured["governing_directives"] == ["A\nB", " C "]
    assert captured["browser_endpoint_id"] == "9222"
    assert captured["temporary"] is True


def test_solo_api_preserves_goal_whitespace_and_newlines(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    api = DashboardAPI(config)
    normalized = api.normalize_create(
        {
            "workflow_mode": "solo",
            "task": "solo task",
            "role": "DEV",
            "goal": "  solo\r\ngoal  ",
            "browser_endpoint_id": "9222",
        }
    )
    assert normalized["goal"] == "  solo\ngoal  "


def test_goal_change_carries_matching_canonical_governing_contract(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    api = DashboardAPI(config)
    payload, expected = api.normalize_goal_change(
        {
            "goal": "  revised\r\nGoal  ",
            "governing_objective": "  revised objective\rline  ",
            "governing_directives": [],
            "expected_task_version": 3,
        }
    )
    assert expected == 3
    assert payload == {
        "goal": "  revised\nGoal  ",
        "governing_objective": "  revised objective\nline  ",
        "governing_directives": [],
    }

    store = TaskStore(config)
    task = store.create_task(
        "initial",
        task_id="goal-contract",
        governing_objective="initial objective",
        governing_directives=[],
    )
    running = store.update(
        task["manifest_path"],
        lambda state: state.update(status="RUNNING"),
    )
    changed = store.change_goal(
        running["manifest_path"],
        payload["goal"],
        governing_objective=payload["governing_objective"],
        governing_directives=payload["governing_directives"],
        external_command_id="goal-command",
    )
    revision = changed["goal_revisions"][-1]
    assert revision["goal"] == "  revised\nGoal  "
    assert json.loads(revision["governing_semantic_payload"]) == {
        "objective": "  revised objective\nline  ",
        "directives": [],
    }
    assert revision["governing_semantic_sha256"] == hashlib.sha256(
        revision["governing_semantic_payload"].encode("utf-8")
    ).hexdigest()


def test_accepted_identity_settle_starts_at_first_provisional_decision_and_survives_reload(
    tmp_path,
    monkeypatch,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="identity-settle"
    )
    accepted_at = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
    first_decision_at = accepted_at + timedelta(seconds=34)
    response_deadline = first_decision_at + timedelta(seconds=30)

    class FrozenDateTime(datetime):
        current = first_decision_at

        @classmethod
        def now(cls, tz=None):
            value = cls.current
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(worker_module, "datetime", FrozenDateTime)

    provisional = "https://chatgpt.com/c/local-chatgpt:identity-settle"
    hop["conversation_url"] = provisional
    hop["timestamps"]["sent_at"] = accepted_at.isoformat()
    hop["wait"]["deadline_at"] = response_deadline.isoformat()
    state["roles"]["PLAN"]["page_url"] = provisional
    state = store.save(path, state)
    hop = state["hops"][0]
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts
    original_response_deadline = hop["wait"]["deadline_at"]

    snapshot = SimpleNamespace(
        page_id=receipt.binding.page_id,
        session_id="local-chatgpt:identity-settle",
        url=provisional,
        messages=(
            MessageSnapshot(
                "user",
                receipt.user_message_id,
                receipt.user_turn_id,
                receipt.prompt,
                (),
            ),
        ),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

    class Actions:
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                Client(),
                receipt.binding.page_id,
                snapshot.url,
                False,
                False,
            )

    assert asyncio.run(worker._owned_or_block(state, "PLAN", Actions())) is None
    wait = hop["wait"]
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_accepted_conversation_identity"
    assert datetime.fromisoformat(wait["accepted_identity_started_at"]) == first_decision_at
    assert datetime.fromisoformat(wait["accepted_identity_deadline_at"]) == (
        first_decision_at + timedelta(seconds=10)
    )
    assert wait["accepted_identity_page_id"] == receipt.binding.page_id
    assert wait["deadline_at"] == original_response_deadline
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == original_attempts

    store.save(path, state)
    reloaded = store.load(path)
    restarted = CDPAWorker(worker.config, store=store)
    reloaded_wait = reloaded["hops"][0]["wait"]
    assert reloaded_wait["accepted_identity_started_at"] == wait["accepted_identity_started_at"]
    assert reloaded_wait["accepted_identity_deadline_at"] == wait["accepted_identity_deadline_at"]
    assert reloaded_wait["deadline_at"] == original_response_deadline

    FrozenDateTime.current = first_decision_at + timedelta(milliseconds=92)
    canonical_id = "11111111-1111-1111-1111-111111111111"
    snapshot.session_id = canonical_id
    snapshot.url = f"https://chatgpt.com/c/{canonical_id}"
    acquired = asyncio.run(restarted._owned_or_block(reloaded, "PLAN", Actions()))
    assert acquired is not None
    final_hop = reloaded["hops"][0]
    final_wait = final_hop["wait"]
    assert final_wait["accepted_identity_started_at"] == wait["accepted_identity_started_at"]
    assert final_wait["accepted_identity_deadline_at"] == wait["accepted_identity_deadline_at"]
    assert final_wait["accepted_identity_outcome"] == "canonicalized"
    assert final_wait["deadline_at"] == original_response_deadline
    assert final_hop["request_id"] == hop["request_id"]
    durable = RequestLedger(final_hop["ledger_path"]).get(final_hop["request_id"])
    assert durable.attempts == original_attempts
    assert durable.receipt["conversation_id"] == canonical_id


def test_accepted_identity_url_settle_is_independent_of_response_deadline(
    tmp_path,
    monkeypatch,
):
    _store_value, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="identity-settle-expired-response"
    )
    now = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(worker_module, "datetime", FrozenDateTime)
    provisional = "https://chatgpt.com/c/local-chatgpt:identity-expired"
    hop["conversation_url"] = provisional
    hop["wait"]["deadline_at"] = (now - timedelta(milliseconds=1)).isoformat()
    state["roles"]["PLAN"]["page_url"] = provisional
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts
    original_hop_count = len(state["hops"])

    snapshot = SimpleNamespace(
        page_id=receipt.binding.page_id,
        session_id="local-chatgpt:identity-expired",
        url=provisional,
        messages=(
            MessageSnapshot(
                "user",
                receipt.user_message_id,
                receipt.user_turn_id,
                receipt.prompt,
                (),
            ),
        ),
    )
    acquired = AcquiredRole(
        SimpleNamespace(assert_ownership=lambda: None),
        receipt.binding.page_id,
        provisional,
        False,
        False,
    )

    async def assert_ownership():
        return snapshot

    acquired.client.assert_ownership = assert_ownership

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    assert asyncio.run(worker._owned_or_block(state, "PLAN", Actions())) is None
    wait = hop["wait"]
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_accepted_conversation_identity"
    assert state["block_code"] is None
    assert datetime.fromisoformat(wait["accepted_identity_deadline_at"]) == now + timedelta(seconds=10)
    assert len(state["hops"]) == original_hop_count
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == original_attempts


def test_accepted_identity_canonical_conflict_blocks_immediately_without_starting_settle(
    tmp_path,
):
    _store_value, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="identity-canonical-conflict"
    )
    provisional = "https://chatgpt.com/c/local-chatgpt:identity-conflict"
    hop["conversation_url"] = provisional
    state["roles"]["PLAN"]["page_url"] = provisional
    session_id = "11111111-1111-4111-8111-111111111111"
    conflicting_url = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
    snapshot = SimpleNamespace(
        page_id=receipt.binding.page_id,
        session_id=session_id,
        url=conflicting_url,
        messages=(
            MessageSnapshot(
                "user",
                receipt.user_message_id,
                receipt.user_turn_id,
                receipt.prompt,
                (),
            ),
        ),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

    class Actions:
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                Client(), receipt.binding.page_id, conflicting_url, False, False
            )

    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts
    original_hop_count = len(state["hops"])

    assert asyncio.run(worker._owned_or_block(state, "PLAN", Actions())) is None

    wait = hop["wait"]
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "accepted_conversation_identity_unresolved"
    assert state["block_retryable"] is False
    assert "accepted_identity_started_at" not in wait
    assert "accepted_identity_deadline_at" not in wait
    assert wait["accepted_identity_outcome"] == "blocked_unresolved"
    assert len(state["hops"]) == original_hop_count
    assert hop["receipt"] == receipt.to_dict()
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == original_attempts


def test_accepted_identity_settle_expiry_admits_active_response_without_replay(
    tmp_path,
    monkeypatch,
):
    _store_value, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="identity-settle-window-expiry"
    )
    now = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        current = None

        @classmethod
        def now(cls, tz=None):
            value = cls.current
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    FrozenDateTime.current = now
    monkeypatch.setattr(worker_module, "datetime", FrozenDateTime)
    provisional = "https://chatgpt.com/c/local-chatgpt:identity-window"
    hop["conversation_url"] = provisional
    hop["wait"]["deadline_at"] = (now + timedelta(seconds=30)).isoformat()
    state["roles"]["PLAN"]["page_url"] = provisional
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts
    original_hop_count = len(state["hops"])
    snapshot = SimpleNamespace(
        page_id=receipt.binding.page_id,
        session_id="local-chatgpt:identity-window",
        url=provisional,
        messages=(
            MessageSnapshot(
                "user",
                receipt.user_message_id,
                receipt.user_turn_id,
                receipt.prompt,
                (),
            ),
        ),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

    class Actions:
        async def locate_owned(self, _state, _role):
            return AcquiredRole(Client(), receipt.binding.page_id, snapshot.url, False, False)

    assert asyncio.run(worker._owned_or_block(state, "PLAN", Actions())) is None
    started = hop["wait"]["accepted_identity_started_at"]
    deadline = hop["wait"]["accepted_identity_deadline_at"]
    FrozenDateTime.current = datetime.fromisoformat(deadline)

    assert asyncio.run(worker._owned_or_block(state, "PLAN", Actions())) is not None
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert hop["wait"]["accepted_identity_started_at"] == started
    assert hop["wait"]["accepted_identity_deadline_at"] == deadline
    assert hop["wait"]["accepted_identity_outcome"] == "observe_response_without_url"
    assert len(state["hops"]) == original_hop_count
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == original_attempts


def test_receipt_divergence_allows_only_explicit_stop(tmp_path):
    store, state, worker, path, hop, receipt, _sent_at = (
        _prepare_sent_waiting_task(tmp_path, task_id="receipt-divergence-controls")
    )
    ledger_receipt = {**receipt.to_dict(), "conversation_id": "ledger-canonical"}
    hop_receipt = {**receipt.to_dict(), "conversation_id": "hop-canonical"}
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=ledger_receipt,
    )
    hop["receipt"] = hop_receipt
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["active_action"] = "blocked"
    state["block_code"] = "accepted_receipt_provenance_divergence"
    state["block_retryable"] = False
    hop["state"] = "waiting"
    state = store.save(path, state)
    hop = state["hops"][0]

    for action in ("resume", "retry", "restart_role", "new_chat", "route_plan"):
        assert workflow_control_eligibility(state, action)["eligible"] is False
        with pytest.raises(ValueError):
            store.request_control(
                path,
                action,
                role="PLAN" if action in {"restart_role", "new_chat"} else None,
                reason="forbidden divergence control",
                external_command_id=f"receipt-divergence-{action}",
            )
        unchanged = store.load(path)
        record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
        assert unchanged["status"] == "BLOCKED"
        assert unchanged["block_code"] == "accepted_receipt_provenance_divergence"
        assert unchanged["hops"][0]["receipt"] == hop_receipt
        assert record is not None
        assert record.receipt == ledger_receipt
        assert record.attempts == 1

    assert workflow_control_eligibility(state, "stop")["eligible"] is True
    stopped = store.request_control(
        path,
        "stop",
        reason="explicit operator stop after forensic divergence",
        external_command_id="receipt-divergence-stop",
    )

    class StopActions:
        async def locate_owned(self, *_args, **_kwargs):
            return None

    assert asyncio.run(worker._apply_control(stopped, StopActions(), path)) is True
    stopped_state = store.load(path)
    stopped_hop = stopped_state["hops"][0]
    stopped_record = RequestLedger(stopped_hop["ledger_path"]).get(stopped_hop["request_id"])
    assert stopped_state["status"] == "STOPPED"
    assert len(stopped_state["hops"]) == 1
    assert stopped_hop["receipt"] == hop_receipt
    assert stopped_record is not None
    assert stopped_record.receipt == ledger_receipt
    assert stopped_record.attempts == 1


def test_receipt_divergence_allows_only_one_way_conversation_id_enrichment(tmp_path):
    _store_value, state, worker, _path, hop, receipt, _sent_at = (
        _prepare_sent_waiting_task(tmp_path, task_id="receipt-divergence-one-way-enrichment")
    )
    canonical_id = "11111111-1111-4111-8111-111111111111"
    ledger_receipt = {**receipt.to_dict(), "conversation_id": canonical_id}
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=ledger_receipt,
    )
    assert hop["receipt"]["conversation_id"] is None

    worker._reconcile_hop_conversation_identity(state, hop)

    assert state["status"] != "BLOCKED"
    assert hop["receipt"] == ledger_receipt
    assert hop["conversation_url"] == f"https://chatgpt.com/c/{canonical_id}"
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert record is not None
    assert record.receipt == ledger_receipt
    assert record.attempts == 1


def _endpoint_contract_bootstrap():
    return {
        "bootstrap_id": "endpoint-contract-bootstrap",
        "name": "Endpoint contract bootstrap",
        "description": "",
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_assistant_message_id": "22222222-2222-4222-8222-222222222222",
        "enabled": True,
        "tags": [],
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
        "expires_at": None,
        "last_verified_at": None,
        "source_fingerprints": {},
    }


def test_endpoint_admission_provenance_distinguishes_equal_values_and_required_pin(tmp_path):
    store = _store(tmp_path)
    default = store.endpoints.resolve()
    temporary_endpoint = store.endpoints.add("9202", temporary_default=True)

    omitted = store.create_task(
        "omitted endpoint",
        task_id="endpoint-prov-omitted",
        governing_objective="preserve omitted endpoint provenance",
        governing_directives=[],
    )
    assert omitted["admission"]["browser_endpoint_explicit"] is False
    assert omitted["admission"]["conversation_mode_explicit"] is False
    assert omitted["endpoint_pinned"] is False

    explicit_default = store.create_task(
        "explicit default endpoint",
        task_id="endpoint-prov-explicit-default",
        browser_endpoint_id=default["endpoint_id"],
        governing_objective="preserve explicit default endpoint provenance",
        governing_directives=[],
    )
    assert explicit_default["admission"]["browser_endpoint_explicit"] is True
    assert explicit_default["endpoint_pinned"] is True
    assert explicit_default["browser_endpoint_id"] == default["endpoint_id"]

    explicit_temporary = store.create_task(
        "explicit temporary matching endpoint default",
        task_id="endpoint-prov-explicit-temporary",
        browser_endpoint_id=temporary_endpoint["endpoint_id"],
        temporary=True,
        governing_objective="preserve explicit temporary provenance",
        governing_directives=[],
    )
    assert explicit_temporary["admission"]["conversation_mode_explicit"] is True
    assert explicit_temporary["conversation_mode"] == "temporary"
    assert explicit_temporary["endpoint_pinned"] is True

    explicit_persistent = store.create_task(
        "explicit persistent on temporary-default endpoint",
        task_id="endpoint-prov-explicit-persistent",
        browser_endpoint_id=temporary_endpoint["endpoint_id"],
        conversation_mode="persistent",
        governing_objective="preserve explicit persistent provenance",
        governing_directives=[],
    )
    assert explicit_persistent["admission"]["conversation_mode_explicit"] is True
    assert explicit_persistent["conversation_mode"] == "persistent"

    required = store.create_task(
        "required endpoint",
        task_id="endpoint-prov-required",
        required_browser_endpoint_id=temporary_endpoint["endpoint_id"],
        governing_objective="preserve required endpoint authority",
        governing_directives=[],
    )
    assert required["admission"]["browser_endpoint_explicit"] is False
    assert required["admission"]["required_endpoint_explicit"] is True
    assert required["browser_endpoint_id"] == temporary_endpoint["endpoint_id"]
    assert required["required_browser_endpoint_id"] == temporary_endpoint["endpoint_id"]
    assert required["endpoint_pinned"] is True

    explicit_fresh = store.create_task(
        "explicit fresh context",
        task_id="endpoint-prov-explicit-fresh",
        context_policy="fresh",
        governing_objective="preserve explicit context authority",
        governing_directives=[],
    )
    assert explicit_fresh["admission"]["context_policy_explicit"] is True
    assert explicit_fresh["context_policy"] == "fresh"

    bootstrap = store.create_task(
        "bootstrap context",
        task_id="endpoint-prov-bootstrap",
        bootstrap=_endpoint_contract_bootstrap(),
        governing_objective="preserve bootstrap context authority",
        governing_directives=[],
    )
    assert bootstrap["admission"]["bootstrap_present"] is True
    assert bootstrap["admission"]["context_policy_explicit"] is False
    assert bootstrap["context_policy"] == "bootstrap"


def test_initial_placement_requires_omitted_endpoint_provenance_not_only_unpinned_bit(tmp_path):
    store = _store(tmp_path)
    default = store.endpoints.resolve()
    second = store.endpoints.add("9202", temporary_default=True)
    explicit = store.create_task(
        "explicit endpoint cannot be made placeable",
        task_id="placement-explicit-corruption",
        browser_endpoint_id=default["endpoint_id"],
        governing_objective="keep explicit home endpoint immutable",
        governing_directives=[],
    )
    path = Path(explicit["manifest_path"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["endpoint_pinned"] = False
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="omitted endpoint"):
        store.update_initial_placement(
            path,
            lambda state: state.update(
                browser_endpoint_id=second["endpoint_id"],
                endpoint_pinned=True,
                conversation_mode="temporary",
                context_policy="fresh",
                roles={
                    **state["roles"],
                    state["active_role"]: {
                        **state["roles"][state["active_role"]],
                        "browser_endpoint_id": second["endpoint_id"],
                    },
                },
                hops=[
                    {
                        **item,
                        **(
                            {"browser_endpoint_id": second["endpoint_id"]}
                            if item.get("hop_id") == state["active_hop_id"]
                            else {}
                        ),
                    }
                    for item in state["hops"]
                ],
            ),
        )


def test_endpoint_pinned_authority_cannot_be_toggled_by_generic_update(tmp_path):
    store = _store(tmp_path)
    default = store.endpoints.resolve()
    pinned = store.create_task(
        "pinned endpoint",
        task_id="endpoint-pin-immutable",
        browser_endpoint_id=default["endpoint_id"],
        governing_objective="keep pin immutable",
        governing_directives=[],
    )
    with pytest.raises(ValueError, match="endpoint_pinned.*immutable"):
        store.update(
            pinned["manifest_path"],
            lambda state: state.update(endpoint_pinned=False),
        )

    unpinned = store.create_task(
        "unpinned endpoint",
        task_id="endpoint-pin-unpinned",
        governing_objective="only placement may pin",
        governing_directives=[],
    )
    with pytest.raises(ValueError, match="endpoint_pinned.*immutable"):
        store.update(
            unpinned["manifest_path"],
            lambda state: state.update(endpoint_pinned=True),
        )


@pytest.mark.parametrize(
    ("operation", "field"),
    (
        ("save", "browser_endpoint_id"),
        ("save", "endpoint_pinned"),
        ("update", "browser_endpoint_id"),
        ("update", "endpoint_pinned"),
    ),
)
def test_generic_persistence_rejects_deleting_existing_endpoint_authority(
    tmp_path, operation, field
):
    store = _store(tmp_path)
    store.endpoints.resolve()
    home = store.endpoints.add("9202")
    pinned = store.create_task(
        "pinned endpoint deletion guard",
        task_id=f"endpoint-delete-{operation}-{field.replace('_', '-')}",
        browser_endpoint_id=home["endpoint_id"],
        governing_objective="preserve existing endpoint authority keys",
        governing_directives=[],
    )
    path = Path(pinned["manifest_path"])
    worker = CDPAWorker(store.config, store=store)

    if operation == "save":
        candidate = json.loads(json.dumps(store.load(path)))
        candidate.pop(field)
        with pytest.raises(ValueError, match=rf"{field}.*immutable"):
            store.save(path, candidate)
    else:
        def delete_field(state):
            state.pop(field)
            return state

        with pytest.raises(ValueError, match=rf"{field}.*immutable"):
            store.update(path, delete_field)

    unchanged = store.load(path)
    assert unchanged["endpoint_pinned"] is True
    assert unchanged["browser_endpoint_id"] == home["endpoint_id"]
    assert worker._execution_endpoint_id(unchanged) == home["endpoint_id"]


def test_initial_placement_has_exact_pristine_mutation_whitelist(tmp_path):
    store = _store(tmp_path)
    second = store.endpoints.add("9202", temporary_default=True)
    task = store.create_task(
        "place me",
        task_id="initial-placement",
        governing_objective="place exactly once",
        governing_directives=[],
    )
    assert task["endpoint_pinned"] is False

    def legal(state):
        state["browser_endpoint_id"] = second["endpoint_id"]
        state["endpoint_pinned"] = True
        state["conversation_mode"] = "temporary"
        state["context_policy"] = "fresh"
        return state

    placed = store.update_initial_placement(task["manifest_path"], legal)
    assert placed["browser_endpoint_id"] == second["endpoint_id"]
    assert placed["endpoint_pinned"] is True
    assert placed["conversation_mode"] == "temporary"

    other = store.create_task(
        "reject extra mutation",
        task_id="initial-placement-reject",
        governing_objective="place exactly once",
        governing_directives=[],
    )

    def illegal(state):
        state["browser_endpoint_id"] = second["endpoint_id"]
        state["endpoint_pinned"] = True
        state["task_text"] = "corrupted"
        return state

    with pytest.raises(ValueError, match="initial endpoint placement"):
        store.update_initial_placement(other["manifest_path"], illegal)


def test_initial_placement_cannot_rewrite_explicit_mode(tmp_path):
    store = _store(tmp_path)
    second = store.endpoints.add("9202", temporary_default=False)
    task = store.create_task(
        "explicit temporary",
        task_id="initial-placement-explicit-mode",
        temporary=True,
        governing_objective="stay temporary",
        governing_directives=[],
    )
    assert task["endpoint_pinned"] is False
    assert task["conversation_mode"] == "temporary"

    with pytest.raises(ValueError, match="explicit conversation mode"):
        store.update_initial_placement(
            task["manifest_path"],
            lambda state: state.update(
                browser_endpoint_id=second["endpoint_id"],
                endpoint_pinned=True,
                conversation_mode="persistent",
            ),
        )


def test_admission_corruption_after_accepted_send_blocks_then_allows_only_explicit_stop(
    tmp_path,
    monkeypatch,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="corrupt-admission"
    )
    original_task_text = state["task_text"]
    original_task_hash = state["task_text_sha256"]
    original_request_id = hop["request_id"]
    original_receipt = json.loads(json.dumps(hop["receipt"]))
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts
    original_hop_count = len(state["hops"])
    original_manifests = {
        str(item["task_id"]) for item in store.discover()
    }

    raw = json.loads(path.read_text(encoding="utf-8"))
    corrupted_task_text = original_task_text + " !!!"
    raw["task_text"] = corrupted_task_text
    assert raw["task_text_sha256"] == original_task_hash
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    class ForbiddenActions:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("accepted admission corruption must block before browser access")

    monkeypatch.setattr(worker_module, "CDPATabActions", ForbiddenActions)

    blocked = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert blocked is not None
    blocked_hop = next(
        item for item in blocked["hops"] if item["request_id"] == original_request_id
    )
    blocked_record = RequestLedger(blocked_hop["ledger_path"]).get(original_request_id)
    assert blocked["status"] == "BLOCKED"
    assert blocked["block_code"] == "accepted_admission_integrity_corruption"
    assert blocked["block_retryable"] is False
    assert blocked["task_text"] == corrupted_task_text
    assert blocked["task_text_sha256"] == original_task_hash
    assert blocked_hop["receipt"] == original_receipt
    assert blocked_hop["request_id"] == original_request_id
    assert blocked_record.attempts == original_attempts
    assert len(blocked["hops"]) == original_hop_count
    assert {str(item["task_id"]) for item in store.discover()} == original_manifests

    with pytest.raises(ValueError, match="STOPPED"):
        store.replace_task_and_rewire(
            blocked["task_id"],
            "incident context only",
            corrected_task_text="Corrected canonical task",
            governing_objective="correct objective",
            governing_directives=[],
            reuse_team=True,
            rewire_children=False,
            incident_id="admission-corruption-1",
        )
    still_blocked = store.load(path)
    still_hop = next(
        item for item in still_blocked["hops"] if item["request_id"] == original_request_id
    )
    still_record = RequestLedger(still_hop["ledger_path"]).get(original_request_id)
    assert still_blocked["status"] == "BLOCKED"
    assert still_blocked["task_text"] == corrupted_task_text
    assert still_hop["receipt"] == original_receipt
    assert still_record.attempts == original_attempts
    assert len(still_blocked["hops"]) == original_hop_count
    assert {str(item["task_id"]) for item in store.discover()} == original_manifests

    controls_before = json.loads(json.dumps(still_blocked.get("controls") or []))
    for action in (
        "pause",
        "resume",
        "retry",
        "restart_role",
        "new_chat",
        "open_tab",
        "route_plan",
        "clear_team",
    ):
        eligibility = workflow_control_eligibility(
            still_blocked,
            action,
            role="PLAN" if action in {"restart_role", "new_chat", "open_tab"} else None,
        )
        assert eligibility["eligible"] is False
        with pytest.raises(ValueError, match="explicit Stop"):
            store.request_control(
                path,
                action,
                role="PLAN" if action in {"restart_role", "new_chat", "open_tab"} else None,
                reason=f"forbidden {action} after accepted admission corruption",
                external_command_id=f"corrupt-admission-{action}",
            )
        after_rejection = store.load(path)
        assert after_rejection.get("controls") == controls_before
        assert after_rejection["status"] == "BLOCKED"
        assert after_rejection["block_code"] == "accepted_admission_integrity_corruption"

    assert workflow_control_eligibility(still_blocked, "stop")["eligible"] is True
    queued = store.request_control(
        path,
        "stop",
        reason="explicit operator stop after accepted admission corruption",
        external_command_id="stop-corrupt-admission",
    )
    assert [item["action"] for item in queued.get("controls") or []] == ["stop"]

    class StopActions:
        def __init__(self, *_args, **_kwargs):
            pass

        async def locate_owned(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(worker_module, "CDPATabActions", StopActions)
    stopped = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    assert stopped is not None
    stopped_hop = next(
        item for item in stopped["hops"] if item["request_id"] == original_request_id
    )
    stopped_record = RequestLedger(stopped_hop["ledger_path"]).get(original_request_id)
    assert stopped["status"] == "STOPPED"
    assert stopped["task_text"] == corrupted_task_text
    assert stopped["task_text_sha256"] == original_task_hash
    assert original_task_text != stopped["task_text"]
    assert stopped_hop["receipt"] == original_receipt
    assert stopped_hop["request_id"] == original_request_id
    assert stopped_record.attempts == original_attempts
    assert len(stopped["hops"]) == original_hop_count
    assert {str(item["task_id"]) for item in store.discover()} == original_manifests


def test_preclassification_stop_first_persists_corruption_then_terminalizes_on_second_advance(
    tmp_path,
    monkeypatch,
):
    store, state, worker, path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="corrupt-admission-stop-ordering"
    )
    original_task_text = state["task_text"]
    original_task_hash = state["task_text_sha256"]
    original_request_id = hop["request_id"]
    original_receipt = json.loads(json.dumps(hop["receipt"]))
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts
    original_hop_count = len(state["hops"])
    original_manifests = {str(item["task_id"]) for item in store.discover()}

    raw = json.loads(path.read_text(encoding="utf-8"))
    corrupted_task_text = original_task_text + " !!!"
    raw["task_text"] = corrupted_task_text
    assert raw["task_text_sha256"] == original_task_hash
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    queued = store.request_control(
        path,
        "stop",
        reason="operator stop queued before corruption classification",
        external_command_id="stop-before-corruption-classification",
    )
    assert queued["status"] == "RUNNING"
    assert queued.get("block_code") is None
    assert [
        (item["action"], item["status"]) for item in queued.get("controls") or []
    ] == [("stop", "requested")]

    class ForbiddenActions:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError(
                "first corruption-classification advance must not reach browser actions"
            )

    monkeypatch.setattr(worker_module, "CDPATabActions", ForbiddenActions)

    blocked = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert blocked is not None
    blocked_hop = next(
        item for item in blocked["hops"] if item["request_id"] == original_request_id
    )
    blocked_record = RequestLedger(blocked_hop["ledger_path"]).get(original_request_id)
    assert blocked["status"] == "BLOCKED"
    assert blocked["block_code"] == "accepted_admission_integrity_corruption"
    assert blocked["block_retryable"] is False
    assert blocked["task_text"] == corrupted_task_text
    assert blocked["task_text_sha256"] == original_task_hash
    assert blocked_hop["receipt"] == original_receipt
    assert blocked_hop["request_id"] == original_request_id
    assert blocked_record.attempts == original_attempts
    assert len(blocked["hops"]) == original_hop_count
    assert {str(item["task_id"]) for item in store.discover()} == original_manifests
    assert [
        (item["action"], item["status"]) for item in blocked.get("controls") or []
    ] == [("stop", "requested")]

    class StopActions:
        def __init__(self, *_args, **_kwargs):
            pass

        async def locate_owned(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(worker_module, "CDPATabActions", StopActions)

    stopped = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert stopped is not None
    stopped_hop = next(
        item for item in stopped["hops"] if item["request_id"] == original_request_id
    )
    stopped_record = RequestLedger(stopped_hop["ledger_path"]).get(original_request_id)
    assert stopped["status"] == "STOPPED"
    assert stopped["task_text"] == corrupted_task_text
    assert stopped["task_text_sha256"] == original_task_hash
    assert stopped_hop["receipt"] == original_receipt
    assert stopped_hop["request_id"] == original_request_id
    assert stopped_record.attempts == original_attempts
    assert len(stopped["hops"]) == original_hop_count
    assert {str(item["task_id"]) for item in store.discover()} == original_manifests
    assert [
        (item["action"], item["status"]) for item in stopped.get("controls") or []
    ] == [("stop", "applied")]
    assert any(
        "accepted admission integrity corruption" in str(item.get("error") or "").lower()
        for item in stopped.get("errors") or []
        if isinstance(item, dict)
    )

    stopped_path = Path(stopped["manifest_path"])
    stopped_bytes = stopped_path.read_bytes()
    manifests_before_replacement = {
        str(item["task_id"]) for item in store.discover()
    }

    with pytest.raises(ValueError, match="corrected_task_text"):
        store.replace_task_and_rewire(
            stopped["task_id"],
            "Unsafe continuation prose must not become executable authority.",
            reuse_team=True,
            rewire_children=False,
            incident_id="real-admission-corruption-replacement",
        )

    assert stopped_path.read_bytes() == stopped_bytes
    assert {
        str(item["task_id"]) for item in store.discover()
    } == manifests_before_replacement

    corrected_text = "Corrected real lifecycle Team task"
    corrected_objective = "Execute the trusted corrected Team task only."
    corrected_directives = ["Preserve the original stopped task as forensic history."]
    replacement = store.replace_task_and_rewire(
        stopped["task_id"],
        "Incident context remains metadata only.",
        corrected_task_text=corrected_text,
        governing_objective=corrected_objective,
        governing_directives=corrected_directives,
        reuse_team=True,
        rewire_children=False,
        incident_id="real-admission-corruption-replacement",
    )["replacement"]

    expected_payload, expected_digest = normalize_governing_contract(
        corrected_objective,
        corrected_directives,
    )
    assert replacement["task_text"] == corrected_text
    assert replacement["task_text_sha256"] == hashlib.sha256(
        corrected_text.encode("utf-8")
    ).hexdigest()
    assert replacement["governing_semantic_payload"] == expected_payload
    assert replacement["governing_semantic_sha256"] == expected_digest
    assert corrupted_task_text not in replacement["task_text"]
    assert stopped_path.read_bytes() == stopped_bytes


def test_team_incident_replacement_uses_only_corrected_executable_authority(tmp_path):
    store, state, _worker, path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="corrupt-team-replacement-authority"
    )
    original_request_id = hop["request_id"]
    original_receipt = json.loads(json.dumps(hop["receipt"]))
    original_attempts = RequestLedger(hop["ledger_path"]).get(original_request_id).attempts

    raw = json.loads(path.read_text(encoding="utf-8"))
    corrupt_text = state["task_text"] + " !!!"
    raw["task_text"] = corrupt_text
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stopped = _stop_task(store, store.load(path), reason="admission-integrity incident")
    stopped_path = Path(stopped["manifest_path"])
    stopped_bytes = stopped_path.read_bytes()

    corrected_text = "Corrected canonical Team task"
    corrected_objective = "Execute only the corrected Team authority."
    corrected_directives = [
        "Preserve accepted forensic history.",
        "Do not reuse corrupt executable text.",
    ]
    expected_payload, expected_digest = normalize_governing_contract(
        corrected_objective,
        corrected_directives,
    )

    result = store.replace_task_and_rewire(
        stopped["task_id"],
        "Incident context is metadata only.",
        corrected_task_text=corrected_text,
        governing_objective=corrected_objective,
        governing_directives=corrected_directives,
        reuse_team=True,
        rewire_children=False,
        incident_id="team-admission-correction-1",
    )
    replacement = result["replacement"]

    assert replacement["task_id"] != stopped["task_id"]
    assert replacement["replaces_task_id"] == stopped["task_id"]
    assert replacement["replacement_incident_id"] == "team-admission-correction-1"
    assert replacement["task_text"] == corrected_text
    assert replacement["task_text_sha256"] == hashlib.sha256(
        corrected_text.encode("utf-8")
    ).hexdigest()
    assert replacement["governing_semantic_payload"] == expected_payload
    assert replacement["governing_semantic_sha256"] == expected_digest
    assert corrupt_text not in replacement["task_text"]
    assert corrupt_text not in replacement["governing_semantic_payload"]
    assert "Incident context is metadata only." not in replacement["task_text"]
    assert replacement["workflow_mode"] == stopped["workflow_mode"]
    assert list(replacement["roles"]) == list(stopped["roles"])
    assert replacement["browser_endpoint_id"] == stopped["browser_endpoint_id"]
    assert replacement.get("required_browser_endpoint_id") == stopped.get("required_browser_endpoint_id")
    assert replacement["conversation_mode"] == stopped["conversation_mode"]
    assert replacement["context_policy"] == stopped["context_policy"]
    assert replacement["depends_on_task_ids"] == stopped["depends_on_task_ids"]

    assert stopped_path.read_bytes() == stopped_bytes
    old = store.load(stopped_path)
    old_hop = next(
        item for item in old["hops"] if item["request_id"] == original_request_id
    )
    old_record = RequestLedger(old_hop["ledger_path"]).get(original_request_id)
    assert old["status"] == "STOPPED"
    assert old["task_text"] == corrupt_text
    assert old_hop["receipt"] == original_receipt
    assert old_record.attempts == original_attempts

    repeated = store.replace_task_and_rewire(
        stopped["task_id"],
        "Different incident prose must not duplicate the same replacement.",
        corrected_task_text=corrected_text,
        governing_objective=corrected_objective,
        governing_directives=corrected_directives,
        reuse_team=True,
        rewire_children=False,
        incident_id="team-admission-correction-1",
    )
    assert repeated["replacement"]["task_id"] == replacement["task_id"]


def test_solo_incident_replacement_requires_and_uses_corrected_goal(tmp_path):
    store = _store(tmp_path)
    solo = store.create_task(
        "Solo corrupted executable source",
        task_id="solo-incident-corrected-goal",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
        goal="UNTRUSTED OLD SOLO GOAL",
    )
    stopped = _stop_task(store, solo, reason="solo admission-integrity incident")
    stopped_path = Path(stopped["manifest_path"])
    stopped_bytes = stopped_path.read_bytes()

    with pytest.raises(ValueError, match="corrected_goal"):
        store.replace_task_and_rewire(
            stopped["task_id"],
            "Incident context only.",
            corrected_task_text="Corrected Solo task",
            reuse_team=True,
            rewire_children=False,
            incident_id="solo-admission-correction-1",
        )

    assert stopped_path.read_bytes() == stopped_bytes

    corrected_goal = "Corrected trusted Solo Goal"
    replacement = store.replace_task_and_rewire(
        stopped["task_id"],
        "Incident context only.",
        corrected_task_text="Corrected Solo task",
        corrected_goal=corrected_goal,
        reuse_team=True,
        rewire_children=False,
        incident_id="solo-admission-correction-1",
    )["replacement"]

    assert replacement["workflow_mode"] == "solo"
    assert list(replacement["roles"]) == list(stopped["roles"])
    assert replacement["task_text"] == "Corrected Solo task"
    assert replacement["goal"] == corrected_goal
    assert replacement["goal_sha256"] == hashlib.sha256(
        corrected_goal.encode("utf-8")
    ).hexdigest()
    assert "UNTRUSTED OLD SOLO GOAL" not in replacement["goal"]
    assert replacement["browser_endpoint_id"] == stopped["browser_endpoint_id"]
    assert replacement.get("required_browser_endpoint_id") == stopped.get("required_browser_endpoint_id")
    assert replacement["conversation_mode"] == stopped["conversation_mode"]
    assert replacement["context_policy"] == stopped["context_policy"]
    assert stopped_path.read_bytes() == stopped_bytes


def test_replacement_preserves_solo_mode_role_snapshot_and_goal(tmp_path):
    store = _store(tmp_path)
    solo = store.create_task(
        "solo task",
        task_id="solo-replacement",
        workflow_mode="solo",
        roles=("DEV",),
        browser_endpoint_id="9222",
        goal="  solo goal  ",
    )
    stopped = _stop_task(store, solo)
    replacement = store.replace_task_and_rewire(
        stopped["task_id"],
        "continue safely",
        reuse_team=True,
        rewire_children=False,
        incident_id="solo-replacement-incident",
    )["replacement"]
    assert replacement["workflow_mode"] == "solo"
    assert tuple(replacement["roles"]) == tuple(solo["roles"])
    assert replacement["goal"] == "  solo goal  "
    assert replacement["browser_endpoint_id"] == solo["browser_endpoint_id"]


def test_governing_contract_for_later_hop_binds_exact_sources_and_facts_order(tmp_path):
    store = _store(tmp_path)
    state = store.create_task(
        "first turn detail that must not be replayed later",
        task_id="governing-later-hop",
        governing_objective="Preserve the reviewed contract",
        governing_directives=["Stay on the selected device", "Do not replay accepted Send"],
    )
    hop = dict(state["hops"][0])
    hop.update(
        hop_id=2,
        turn=2,
        target_role="DEV",
        physical_role=state["roles"]["DEV"]["physical_role"],
        kind="task",
    )
    worker = CDPAWorker(store.config, store=store)
    workflow = task_workflow_definitions(state, store.config)
    block = worker._governing_contract_for_hop(state, hop, workflow["DEV"])

    header1, rest = block.split("\n", 1)
    assert header1 == f"GOVERNING_SEMANTIC_SOURCE {len(state['governing_semantic_payload']):05d}"
    semantic, rest = rest.split("\nROLE_CONTINUATION_CONTRACT ", 1)
    assert semantic == state["governing_semantic_payload"]
    length2, rest = rest.split("\n", 1)
    continuation, rest = rest.split("\nGOVERNING_EXECUTION_FACTS_REF ", 1)
    assert int(length2) == len(continuation)
    length3, facts = rest.split("\n", 1)
    assert int(length3) == len(facts)
    parsed = json.loads(facts)
    assert list(parsed) == [
        "task_id",
        "hop_id",
        "manifest_path",
        "repository",
        "workflow_mode",
        "home_endpoint",
        "required_endpoint",
        "conversation_mode",
        "context_policy",
        "goal_revision",
        "effective_goal_sha256",
        "semantic_sha256",
        "target_role",
        "continuation_sha256",
    ]
    assert parsed["manifest_path"] == state["manifest_path"]
    assert parsed["semantic_sha256"] == state["governing_semantic_sha256"]
    assert parsed["continuation_sha256"] == hashlib.sha256(
        continuation.encode("utf-8")
    ).hexdigest()


def test_prefeature_team_task_missing_governing_contract_blocks_before_browser(tmp_path):
    store = _store(tmp_path)
    state = store.create_task(
        "legacy task",
        task_id="governing-missing",
        governing_objective="temporary creation contract",
        governing_directives=[],
    )
    state.pop("governing_semantic_payload")
    state.pop("governing_semantic_sha256")
    hop = state["hops"][0]
    hop.update(
        hop_id=2,
        turn=2,
        target_role="DEV",
        physical_role=state["roles"]["DEV"]["physical_role"],
        request_id="governing-missing-hop2",
    )
    browser_touched = False

    class NoBrowser:
        def __getattr__(self, _name):
            async def touched(*_args, **_kwargs):
                nonlocal browser_touched
                browser_touched = True
                raise AssertionError("browser must not be touched")
            return touched

    asyncio.run(CDPAWorker(store.config, store=store)._pre_send(state, hop, NoBrowser()))
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "governing_contract_unavailable"
    assert browser_touched is False


def test_dependency_projection_exposes_parent_status_and_block_code_without_copying_blocker(tmp_path):
    store = _store(tmp_path)
    parent = store.create_task(
        "blocked parent",
        task_id="dependency-parent",
        governing_objective="Parent objective.",
        governing_directives=[],
    )
    parent = store.update(
        parent["manifest_path"],
        lambda state: state.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="accepted_conversation_identity_unresolved",
            block_retryable=False,
            block_reason="accepted identity unresolved",
        ),
    )
    child = store.create_task(
        "waiting child",
        task_id="dependency-child",
        depends_on_task_ids=(parent["task_id"],),
        governing_objective="Child objective.",
        governing_directives=[],
    )
    projection = build_task_projection(child, tasks=(parent, child))
    assert projection.summary["block_code"] is None
    assert projection.detail["dependencies"] == [
        {
            "task_id": parent["task_id"],
            "team": parent["team"],
            "status": "BLOCKED",
            "block_code": "accepted_conversation_identity_unresolved",
        }
    ]

    stopped_parent = {
        **parent,
        "status": "STOPPED",
        "block_code": None,
    }
    stopped_projection = build_task_projection(
        child, tasks=(stopped_parent, child)
    )
    assert stopped_projection.summary["block_code"] is None
    assert stopped_projection.detail["dependencies"][0] == {
        "task_id": parent["task_id"],
        "team": parent["team"],
        "status": "STOPPED",
        "block_code": None,
    }

    missing_projection = build_task_projection(child, tasks=(child,))
    assert missing_projection.summary["block_code"] is None
    assert missing_projection.detail["dependencies"][0] == {
        "task_id": parent["task_id"],
        "team": parent["task_id"],
        "status": None,
        "block_code": None,
    }
    assert child["waiting"]["waiting_on"] == [parent["task_id"]]

    done_parent = {
        **parent,
        "status": "DONE",
        "block_code": None,
    }
    done_projection = build_task_projection(child, tasks=(done_parent, child))
    assert done_projection.summary["block_code"] is None
    assert done_projection.detail["dependencies"][0]["status"] == "DONE"
    assert done_projection.detail["dependencies"][0]["block_code"] is None


def test_create_command_replay_fails_closed_on_governing_hash_drift(tmp_path):
    store = _store(tmp_path)
    command_id = "cmd-governing-provenance"
    task = store.create_task(
        "governing provenance task",
        task_id="governing-provenance",
        repository=tmp_path,
        governing_objective="Preserve governing provenance.",
        governing_directives=["Do not infer missing policy."],
        external_command_id=command_id,
    )
    worker = CDPAWorker(store.config, store=store)
    command = {
        "command_id": command_id,
        "kind": "create_task",
        "task_id": task["task_id"],
        "payload": {
            "task": task["task_text"],
            "repository": str(tmp_path.resolve()),
            "governing_objective": "Preserve governing provenance.",
            "governing_directives": ["Do not infer missing policy."],
        },
    }
    assert worker._command_replay_state(command)["task_id"] == task["task_id"]

    store.update(
        task["manifest_path"],
        lambda state: state.update(governing_semantic_sha256="0" * 64),
    )
    with pytest.raises(RuntimeError, match="governing"):
        worker._command_replay_state(command)


def test_change_goal_command_replay_fails_closed_on_governing_hash_drift(tmp_path):
    store = _store(tmp_path)
    task = store.create_task(
        "goal replay task",
        task_id="goal-replay-provenance",
        repository=tmp_path,
        governing_objective="Initial objective.",
        governing_directives=[],
    )
    running = store.update(task["manifest_path"], lambda state: state.update(status="RUNNING"))
    command_id = "cmd-goal-replay"
    changed = store.change_goal(
        running["manifest_path"],
        "revised goal",
        governing_objective="Revised objective.",
        governing_directives=["Keep this directive."],
        external_command_id=command_id,
    )
    worker = CDPAWorker(store.config, store=store)
    command = {
        "command_id": command_id,
        "kind": "change_goal",
        "task_id": changed["task_id"],
        "payload": {
            "goal": "revised goal",
            "governing_objective": "Revised objective.",
            "governing_directives": ["Keep this directive."],
        },
    }
    assert worker._command_replay_state(command)["task_id"] == changed["task_id"]
    command["payload"]["governing_directives"] = ["Different directive."]
    with pytest.raises(RuntimeError, match="change-goal command provenance"):
        worker._command_replay_state(command)


def test_governing_contract_rejects_manifest_path_for_different_task(tmp_path):
    store = _store(tmp_path)
    first = store.create_task(
        "first manifest",
        task_id="manifest-first",
        governing_objective="First objective.",
        governing_directives=[],
    )
    second = store.create_task(
        "second manifest",
        task_id="manifest-second",
        governing_objective="Second objective.",
        governing_directives=[],
    )
    state = dict(first)
    state["manifest_path"] = second["manifest_path"]
    hop = dict(first["hops"][0])
    hop.update(
        hop_id=2,
        turn=2,
        target_role="DEV",
        physical_role=first["roles"]["DEV"]["physical_role"],
    )
    workflow = task_workflow_definitions(first, store.config)
    with pytest.raises(ValueError, match="governing_contract_unavailable"):
        CDPAWorker(store.config, store=store)._governing_contract_for_hop(
            state, hop, workflow["DEV"]
        )
