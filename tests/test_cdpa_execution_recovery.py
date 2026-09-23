from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_worker import _active_hop
import playwright_auto.chatgpt as chatgpt_module
from playwright_auto.chatgpt import MessageSnapshot
from playwright_auto.durable import DurableRequestError, RequestLedger, RequestStatus
from test_cdpa_worker import (
    FakeActions,
    RecordingCDPASendActions,
    RecordingCDPASendClient,
    _prepare_sent_waiting_task,
)


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "notes.txt").write_text("unfinished work\n", encoding="utf-8")
    return path


def test_recovery_snapshot_uses_durable_workspace_and_send_evidence(tmp_path: Path):
    execution = _git_repo(tmp_path / "execution")
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-recovery-snapshot", repository=execution
    )
    state["reports"] = [{"path": ".plan/alpha/plan.md", "role": "PLAN"}]

    snapshot = worker._build_execution_recovery_snapshot(
        state, hop, reason="endpoint unavailable"
    )

    assert snapshot["task"]["task_id"] == state["task_id"]
    assert snapshot["task"]["team"] == state["team"]
    assert snapshot["execution"]["role"] == "PLAN"
    assert snapshot["execution"]["request_id"] == hop["request_id"]
    assert snapshot["send_provenance"]["ledger_status"] == "sent"
    assert snapshot["send_provenance"]["classification"] == "accepted_or_ambiguous"
    assert snapshot["workspace"]["repository"] == str(execution.resolve())
    assert "notes.txt" in snapshot["workspace"]["git_status"]
    assert snapshot["reports"][-1]["path"] == ".plan/alpha/plan.md"
    assert snapshot["reason"] == "endpoint unavailable"


def test_completed_durable_response_is_consumed_before_cross_endpoint_recovery(tmp_path: Path):
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-completed-before-relocate"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "# completed PLAN work\n\n"
        "## Evidence\n"
        f"- Inspected durable response for `report_path={handoff}`.\n"
        "- `send_attempts=1` -> completed response consumed without replay.\n",
        encoding="utf-8",
    )
    response = MessageSnapshot(
        "assistant",
        "assistant-completed",
        "turn-completed",
        json.dumps({"route": "TEST", "handoff": handoff}),
        (),
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(
        hop["request_id"],
        status=RequestStatus.COMPLETED,
        response=response.to_dict(),
    )

    assert worker._consume_completed_durable_response(state, hop) is True

    completed = state["hops"][0]
    child = _active_hop(state)
    assert completed["state"] == "routed"
    assert completed["request_id"] == hop["request_id"]
    assert child["kind"] == "handoff"
    assert child["target_role"] == "TEST"
    record = ledger.get(completed["request_id"])
    assert record is not None and record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


@pytest.mark.skip(reason="legacy workflow cross-endpoint relocation is disabled by durable team endpoint pinning")
def test_recovery_final_send_payload_and_persisted_receipt_stay_bounded(tmp_path: Path, monkeypatch):
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-bounded-recovery-send"
    )
    state["task_text"] = "ORIGINAL_TASK_DETAIL " * 5000
    state["test_evidence"] = "VALIDATION_SENTINEL " * 1000
    state["validation_evidence"] = "VALIDATION_BLOB_SENTINEL " * 1000
    hop["response"] = "ASSISTANT_PROGRESS_SENTINEL " * 1000
    hop["wait"]["last_assistant_progress"] = {"text": "PROGRESS_DETAIL_SENTINEL " * 1000}
    report = tmp_path / hop["expected_report_path"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("REPORT_BODY_SENTINEL " * 1000, encoding="utf-8")
    (tmp_path / "checkpoint.md").write_text("CHECKPOINT_BODY_SENTINEL " * 1000, encoding="utf-8")
    log = tmp_path / ".runtime" / "live-events.jsonl"
    log.parent.mkdir(exist_ok=True)
    log.write_text(
        json.dumps({"task_id": state["task_id"], "kind": "recovery-test", "detail": "RECENT_LOG_SENTINEL " * 1000}) + "\n",
        encoding="utf-8",
    )
    original_request_id = hop["request_id"]
    original_prompt = str(hop["prompt"])
    original_record = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert original_record is not None
    original_attempts = original_record.attempts
    old_endpoint = store.endpoints.resolve()["endpoint_id"]
    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    role = state["roles"]["PLAN"]
    role.update(
        browser_endpoint_id=old_endpoint,
        page_id="old-page",
        page_url="https://chatgpt.com/c/old",
        online=False,
    )

    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="old endpoint offline"
    ) is True
    recovery = _active_hop(state)
    snapshot = hop["execution_recovery"]["snapshot"]

    assert snapshot["execution"]["request_id"] == hop["request_id"]
    assert snapshot["durable_paths"]["manifest"] == state["manifest_path"]
    assert snapshot["durable_paths"]["ledger"] == hop["ledger_path"]
    assert snapshot["reason"] == "old endpoint offline"
    assert snapshot["send_provenance"]["classification"] == "accepted_or_ambiguous"
    assert snapshot["send_provenance"]["attempts"] == 1
    assert len(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= 2400

    class RecoveryActions(FakeActions):
        async def locate_owned(self, current_state, current_role):
            return await self.acquire(current_state, current_role)

    asyncio.run(worker._pre_send(state, recovery, RecoveryActions()))
    prompt = str(recovery["prompt"])

    assert len(prompt) <= 8000
    for sentinel in (
        "ORIGINAL_TASK_DETAIL",
        "REPORT_BODY_SENTINEL",
        "CHECKPOINT_BODY_SENTINEL",
        "RECENT_LOG_SENTINEL",
        "VALIDATION_SENTINEL",
        "VALIDATION_BLOB_SENTINEL",
        "ASSISTANT_PROGRESS_SENTINEL",
        "PROGRESS_DETAIL_SENTINEL",
    ):
        assert sentinel not in recovery["handoff"]
        assert sentinel not in json.dumps(snapshot, ensure_ascii=False)
    assert "# PLAN" in prompt
    assert "REPORT" in prompt
    assert "ROUTE" in prompt

    class RejectOversizedComposer:
        def __init__(self):
            self.first = self
            self.text = ""

        async def wait_for(self, **_kwargs):
            return None

        async def fill(self, text):
            if len(text) > 8000:
                raise RuntimeError("oversized composer payload")
            self.text = text

    class FakePage:
        def __init__(self):
            self.composer = RejectOversizedComposer()

        def locator(self, _selector):
            return self.composer

        async def wait_for_function(self, *_args, **_kwargs):
            return True

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt_module, "action_delay", noop)
    monkeypatch.setattr(chatgpt_module, "record_page_action", noop)
    page = FakePage()
    asyncio.run(chatgpt_module.set_composer_text(page, prompt, timeout_ms=30_000))
    assert page.composer.text == prompt

    role["page_id"] = "page-PLAN"
    role["page_url"] = "https://chatgpt.com/c/cdpa-send"
    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
    asyncio.run(worker._sending(state, recovery, RecordingCDPASendActions(client)))

    record = RequestLedger(recovery["ledger_path"]).get(recovery["request_id"])
    assert client.send_calls == [prompt]
    assert original_prompt not in client.send_calls
    assert recovery["receipt"]["prompt"] == prompt
    assert record is not None
    assert record.rendered_prompt == prompt
    assert record.attempts == 1
    unchanged_original = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert unchanged_original is not None
    assert unchanged_original.attempts == original_attempts == 1
    assert unchanged_original.status is RequestStatus.SENT


@pytest.mark.skip(reason="legacy workflow cross-endpoint relocation is disabled by durable team endpoint pinning")
def test_resume_refreshes_oversized_pristine_recovery_child_on_same_request(tmp_path: Path):
    store, state, worker, path, original, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stale-recovery-refresh"
    )
    original_request_id = original["request_id"]
    ledger = RequestLedger(original["ledger_path"])
    original_before = ledger.get(original_request_id)
    assert original_before is not None
    original_source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": original["hop_id"],
        "manifest": state["manifest_path"],
    }
    ledger_data = json.loads(Path(original["ledger_path"]).read_text(encoding="utf-8"))
    original_data = ledger_data["records"][original_request_id]
    original_data["source_context"] = original_source_context
    original_data["idempotency_key"] = worker_module.build_idempotency_key(
        role=original_before.role,
        prompt=original_before.prompt,
        source_context=original_source_context,
        role_prompt_hash=original_before.role_prompt_hash,
        files=original_before.files,
    )
    Path(original["ledger_path"]).write_text(
        json.dumps(ledger_data, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    original_before = ledger.get(original_request_id)
    assert original_before is not None
    original_before_dict = original_before.to_dict()

    old_endpoint = store.endpoints.resolve()["endpoint_id"]
    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    role = state["roles"]["PLAN"]
    role.update(
        browser_endpoint_id=old_endpoint,
        page_id="old-page",
        page_url="https://chatgpt.com/c/old",
        online=False,
    )
    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="old endpoint offline"
    ) is True
    recovery = _active_hop(state)
    assert recovery["kind"] == "execution_recovery"

    child_request_id = recovery["request_id"]
    identity_before = {
        key: recovery.get(key)
        for key in (
            "request_id",
            "parent_hop_id",
            "source_role",
            "target_role",
            "physical_role",
            "turn",
            "kind",
            "recovery_of_request_id",
        )
    }
    legacy_prompt = "LEGACY_OVERSIZED_RECOVERY_PROMPT " * 400
    legacy_handoff = "LEGACY_OVERSIZED_RECOVERY_HANDOFF " * 350
    assert len(legacy_prompt) > 8000
    assert len(legacy_handoff) > 8000
    recovery["state"] = "sending"
    recovery["handoff"] = legacy_handoff
    recovery["prompt"] = legacy_prompt
    recovery["prompt_sha256"] = worker_module._sha(legacy_prompt)
    recovery["conversation_url"] = "https://chatgpt.com/c/recovery-child"
    recovery["conversation_generation"] = int(role.get("conversation_generation") or 0)
    role.update(
        page_id="page-alpha-plan",
        page_url=recovery["conversation_url"],
        online=True,
    )
    source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": recovery["hop_id"],
        "manifest": state["manifest_path"],
    }
    role_prompt_hash = worker_module._sha(
        str(worker_module.task_workflow_definitions(state, worker.config)["PLAN"]["system_prompt"])
    )
    child_record = ledger.begin(
        role=str(recovery["physical_role"]),
        prompt=legacy_prompt,
        request_id=child_request_id,
        source_context=source_context,
        role_prompt_hash=role_prompt_hash,
        render_request_marker=False,
    )
    assert child_record.status is RequestStatus.NEW
    assert child_record.attempts == 0
    assert child_record.error is None
    state = store.save(path, state)
    recovery = _active_hop(state)

    class RecoveryActions(FakeActions):
        async def locate_owned(self, current_state, current_role):
            return await self.acquire(current_state, current_role)

    actions = RecoveryActions()
    control = {
        "control_id": 1,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    asyncio.run(worker._recover_resume_sending(state, recovery, control, actions))

    assert control["status"] == "applied"
    assert recovery["state"] == "pre_send"
    assert recovery["rebuild_prompt_after_relocation"] is True
    assert state["active_action"] == "prepare_send"

    asyncio.run(worker._pre_send(state, recovery, actions))
    refreshed_prompt = str(recovery["prompt"])
    refreshed_handoff = str(recovery["handoff"])
    assert refreshed_prompt != legacy_prompt
    assert refreshed_handoff != legacy_handoff
    assert "LEGACY_OVERSIZED_RECOVERY_HANDOFF" not in refreshed_handoff
    assert "RECOVERY_SNAPSHOT" in refreshed_handoff
    assert original_request_id in refreshed_handoff
    assert len(refreshed_prompt) <= 8000
    assert recovery["rebuild_prompt_after_relocation"] is False
    assert recovery["request_id"] == child_request_id
    assert {
        key: recovery.get(key)
        for key in identity_before
    } == identity_before
    intent = recovery["prompt_rebind_intent"]
    assert intent["request_id"] == child_request_id
    assert intent["prompt"] == refreshed_prompt

    persisted_state = store.load(path)
    persisted_recovery = _active_hop(persisted_state)
    assert persisted_recovery["handoff"] == refreshed_handoff

    refreshed_record = ledger.get(child_request_id)
    assert refreshed_record is not None
    assert refreshed_record.request_id == child_request_id
    assert refreshed_record.status is RequestStatus.NEW
    assert refreshed_record.attempts == 0
    assert refreshed_record.rendered_prompt == refreshed_prompt
    assert refreshed_record.error is None
    unchanged_original = ledger.get(original_request_id)
    assert unchanged_original is not None
    assert unchanged_original.to_dict() == original_before_dict

    before_repeat = refreshed_record.to_dict()
    intent_before = json.loads(json.dumps(intent))
    repeat_control = {
        "control_id": 2,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    asyncio.run(worker._recover_resume_sending(state, recovery, repeat_control, actions))
    assert repeat_control["status"] == "applied"
    assert recovery["state"] == "sending"
    assert recovery["prompt_rebind_intent"] == intent_before
    assert ledger.get(child_request_id).to_dict() == before_repeat
    assert ledger.get(original_request_id).to_dict() == original_before_dict


def _prepare_legacy_recompact_guard_fixture(tmp_path: Path, *, task_id: str):
    store, state, worker, _path, original, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id=task_id
    )
    original_request_id = original["request_id"]
    ledger = RequestLedger(original["ledger_path"])
    original_record = ledger.get(original_request_id)
    assert original_record is not None
    original_source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": original["hop_id"],
        "manifest": state["manifest_path"],
    }
    ledger_data = json.loads(Path(original["ledger_path"]).read_text(encoding="utf-8"))
    original_data = ledger_data["records"][original_request_id]
    original_data["source_context"] = original_source_context
    original_data["idempotency_key"] = worker_module.build_idempotency_key(
        role=original_record.role,
        prompt=original_record.prompt,
        source_context=original_source_context,
        role_prompt_hash=original_record.role_prompt_hash,
        files=original_record.files,
    )
    Path(original["ledger_path"]).write_text(
        json.dumps(ledger_data, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="old endpoint offline", force_new_generation=True
    ) is True
    recovery = _active_hop(state)
    legacy_prompt = "LEGACY_OVERSIZED_RECOVERY_PROMPT " * 400
    legacy_handoff = "LEGACY_OVERSIZED_RECOVERY_HANDOFF " * 350
    recovery.update(
        state="sending",
        handoff=legacy_handoff,
        prompt=legacy_prompt,
        prompt_sha256=worker_module._sha(legacy_prompt),
        rebuild_prompt_after_relocation=True,
    )
    child_source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": recovery["hop_id"],
        "manifest": state["manifest_path"],
    }
    role_prompt_hash = worker_module._sha(
        str(worker_module.task_workflow_definitions(state, worker.config)["PLAN"]["system_prompt"])
    )
    child_record = ledger.begin(
        role=str(recovery["physical_role"]),
        prompt=legacy_prompt,
        request_id=recovery["request_id"],
        source_context=child_source_context,
        role_prompt_hash=role_prompt_hash,
        render_request_marker=False,
    )
    assert child_record.status is RequestStatus.NEW
    return state, worker, recovery, ledger, original_request_id, legacy_handoff


@pytest.mark.parametrize(
    "corruption",
    (
        "accepted_child",
        "ambiguous_sending_child",
        "missing_parent",
        "mismatched_parent_hop",
        "mismatched_recovery_request",
        "mismatched_original_provenance",
    ),
)
@pytest.mark.skip(reason="legacy workflow cross-endpoint relocation is disabled by durable team endpoint pinning")
def test_legacy_recovery_handoff_recompact_fails_closed_on_unsafe_provenance(
    tmp_path: Path, corruption: str
):
    state, worker, recovery, ledger, original_request_id, legacy_handoff = (
        _prepare_legacy_recompact_guard_fixture(
            tmp_path, task_id=f"task-recompact-guard-{corruption}"
        )
    )
    child_request_id = recovery["request_id"]

    if corruption == "accepted_child":
        ledger.update(child_request_id, status=RequestStatus.SENDING, attempts=1)
        ledger.update(
            child_request_id,
            status=RequestStatus.SENT,
            accepted_at=1.0,
            receipt={"prompt_sha256": recovery["prompt_sha256"]},
        )
    elif corruption == "ambiguous_sending_child":
        ledger.update(child_request_id, status=RequestStatus.SENDING, attempts=1)
    elif corruption == "missing_parent":
        state["hops"] = [
            item for item in state["hops"] if item.get("hop_id") != recovery["parent_hop_id"]
        ]
    elif corruption == "mismatched_parent_hop":
        recovery["parent_hop_id"] = 999999
    elif corruption == "mismatched_recovery_request":
        recovery["recovery_of_request_id"] = "wrong-original-request"
    elif corruption == "mismatched_original_provenance":
        ledger_data = json.loads(Path(recovery["ledger_path"]).read_text(encoding="utf-8"))
        ledger_data["records"][original_request_id]["source_context"]["hop_id"] = 999999
        Path(recovery["ledger_path"]).write_text(
            json.dumps(ledger_data, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    else:  # pragma: no cover
        raise AssertionError(corruption)

    child_before = ledger.get(child_request_id).to_dict()
    original_before = ledger.get(original_request_id).to_dict()

    with pytest.raises(DurableRequestError):
        worker._recompact_stale_execution_recovery_handoff(state, recovery)

    assert recovery["handoff"] == legacy_handoff
    assert ledger.get(child_request_id).to_dict() == child_before
    assert ledger.get(original_request_id).to_dict() == original_before


@pytest.mark.skip(reason="legacy workflow cross-endpoint relocation is disabled by durable team endpoint pinning")
def test_resume_does_not_refresh_oversized_recovery_child_with_message_identity(tmp_path: Path):
    store, state, worker, path, original, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-stale-recovery-identity-guard"
    )
    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="old endpoint offline", force_new_generation=True
    ) is True
    recovery = _active_hop(state)
    legacy_prompt = "LEGACY_OVERSIZED_RECOVERY_PROMPT " * 400
    recovery["state"] = "sending"
    recovery["prompt"] = legacy_prompt
    recovery["prompt_sha256"] = worker_module._sha(legacy_prompt)
    recovery["message_identity"] = {"user_message_id": "ambiguous-user"}
    role = state["roles"]["PLAN"]
    source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": recovery["hop_id"],
        "manifest": state["manifest_path"],
    }
    role_prompt_hash = worker_module._sha(
        str(worker_module.task_workflow_definitions(state, worker.config)["PLAN"]["system_prompt"])
    )
    ledger = RequestLedger(recovery["ledger_path"])
    persisted = ledger.begin(
        role=str(recovery["physical_role"]),
        prompt=legacy_prompt,
        request_id=recovery["request_id"],
        source_context=source_context,
        role_prompt_hash=role_prompt_hash,
        render_request_marker=False,
    )
    persisted_before = persisted.to_dict()
    state = store.save(path, state)
    recovery = _active_hop(state)

    class NoActions:
        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("ambiguous child identity must fail closed before browser access")

    control = {
        "control_id": 1,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    asyncio.run(worker._recover_resume_sending(state, recovery, control, NoActions()))

    assert state["status"] == "BLOCKED"
    assert control["status"] == "recovery_required"
    assert control["result"]["outcome"] == "recovery_required"
    assert recovery.get("rebuild_prompt_after_relocation") is not True
    assert ledger.get(recovery["request_id"]).to_dict() == persisted_before


def test_missing_recovery_ledger_fails_closed_without_relocation_or_resend(tmp_path: Path):
    store, state, worker, path, original, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-missing-recovery-ledger"
    )
    original["state"] = "sending"
    original["receipt"] = None
    original["message_identity"] = None
    ledger_path = Path(original["ledger_path"])
    ledger_path.unlink()
    state = store.save(path, state)
    hop = _active_hop(state)

    class NoActions:
        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("missing durable ledger must fail closed before browser access")

    control = {
        "control_id": 1,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    asyncio.run(worker._recover_resume_sending(state, hop, control, NoActions()))

    assert state["status"] == "BLOCKED"
    assert control["status"] == "recovery_required"
    assert control["result"]["outcome"] == "recovery_required"
    assert hop["state"] == "sending"


@pytest.mark.skip(reason="legacy workflow cross-endpoint relocation is disabled by durable team endpoint pinning")
def test_accepted_send_relocates_as_new_recovery_request_without_replay(tmp_path: Path):
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-cross-endpoint-recovery"
    )
    old_endpoint = store.endpoints.resolve()["endpoint_id"]
    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    role = state["roles"]["PLAN"]
    role.update(
        browser_endpoint_id=old_endpoint,
        page_id="old-page",
        page_url="https://chatgpt.com/c/old",
        online=False,
    )
    original_request_id = hop["request_id"]
    original_receipt = json.loads(json.dumps(hop["receipt"]))
    original_record = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert original_record is not None
    original_prompt_hash = original_receipt["prompt_sha256"]
    original_attempts = original_record.attempts
    original_status = original_record.status

    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="old endpoint offline"
    ) is True

    old_hop = state["hops"][0]
    recovery = _active_hop(state)
    assert old_hop["state"] == "abandoned"
    assert old_hop["request_id"] == original_request_id
    assert old_hop["receipt"] == original_receipt
    assert recovery["kind"] == "execution_recovery"
    assert recovery["target_role"] == "PLAN"
    assert recovery["request_id"] != original_request_id
    assert recovery["recovery_of_request_id"] == original_request_id
    assert "Continue this existing role/task from durable state" in recovery["handoff"]
    assert original_request_id in recovery["handoff"]
    snapshot = old_hop["execution_recovery"]["snapshot"]
    assert snapshot["execution"]["request_id"] == original_request_id
    assert snapshot["execution"]["recovery_of_request_id"] == original_request_id
    assert snapshot["send_provenance"]["ledger_status"] == original_status.value
    assert snapshot["send_provenance"]["attempts"] == original_attempts
    assert snapshot["send_provenance"]["receipt"]["prompt_sha256"] == original_prompt_hash
    assert role["browser_endpoint_id"] == new_endpoint
    assert role["page_id"] is None
    assert role["page_url"] is None
    old_record = RequestLedger(old_hop["ledger_path"]).get(original_request_id)
    assert old_record is not None
    assert old_record.status is original_status is RequestStatus.SENT
    assert old_record.attempts == original_attempts == 1
    assert old_record.receipt is not None
    assert old_record.receipt["prompt_sha256"] == original_prompt_hash
    assert old_record.receipt == original_receipt
