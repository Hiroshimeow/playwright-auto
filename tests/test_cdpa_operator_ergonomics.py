from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from playwright_auto import cdpa_cli
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_worker import FakeActions, _prepare_sent_waiting_task, setup_task
from test_dashboard_api import request, start_api


@pytest.mark.parametrize(("content", "message"), [(b"\xff", "UTF-8"), (b" \r\n", "non-empty")])
def test_task_file_rejects_invalid_input_before_submission(tmp_path, capsys, content, message):
    path = tmp_path / "brief with spaces.txt"
    path.write_bytes(content)
    assert cdpa_cli.main(["--task-file", str(path)]) == 2
    assert message in capsys.readouterr().err


def test_task_file_cannot_be_mixed_with_other_prompt_sources(tmp_path, capsys):
    path = tmp_path / "brief.txt"
    path.write_text("hello", encoding="utf-8")
    for other in (["positional"], ["-p", "quick"]):
        assert cdpa_cli.main(["--task-file", str(path), *other]) == 2
        assert "cannot be combined" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cdpa_cli.build_parser().parse_args(["--task-file", str(path), "--task", "explicit"])


def test_cli_exact_status_reads_projection_including_terminal_and_block_details(tmp_path, monkeypatch, capsys):
    config, _, raw, _ = setup_task(tmp_path)
    _, db, server, thread = start_api(tmp_path)
    config = replace(config, dashboard_api_port=server.server_address[1])
    monkeypatch.setattr(cdpa_cli, "load_cdpa_config", lambda *_args, **_kwargs: config)
    try:
        for status in ("INBOX", "BLOCKED", "DONE", "STOPPED"):
            state = {**raw, "status": status}
            if status in {"DONE", "STOPPED"}:
                state.update(active_role=None, active_hop_id=None)
            elif status == "BLOCKED":
                state.update(block_code="durable_send_provenance_missing", block_reason="Missing accepted receipt")
            db.upsert_task_projections([build_task_projection(state, tasks=[state])])
            assert cdpa_cli.main(["status", "--task", raw["task_id"]]) == 0
            out = capsys.readouterr().out
            assert f"status={status}\n" in out
            if status in {"DONE", "STOPPED"}:
                assert "active_role=-\nactive_hop_id=-\n" in out
            else:
                assert "active_role=PLAN\nactive_hop_id=1\nhop_state=pre_send\n" in out
            if status == "BLOCKED":
                assert "block_code=durable_send_provenance_missing\nblock_reason=Missing accepted receipt\n" in out
                assert cdpa_cli.main(["stop", "--task", raw["task_id"]]) == 2
                assert "durable_send_provenance_missing" in capsys.readouterr().err
                assert db.claim_next_command() is None
            if status == "INBOX":
                assert "queue_reason=Queued for browser worker admission\n" in out
        assert cdpa_cli.main(["status", "--task", "absent-task"]) == 2
        assert "404" in capsys.readouterr().err
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def queue_stop(worker, state):
    db = worker.runtime_db
    db.ensure_schema()
    db.enqueue_command(command_id="cmd-stop", idempotency_key="stop-test", kind="task_control",
                       task_id=state["task_id"], expected_task_version=None,
                       payload={"action": "stop_at_boundary"})
    return worker.dispatch_command_once()


@pytest.mark.parametrize("status", ["INBOX", "WAITING", "PAUSED"])
def test_safe_stop_uses_terminal_transition_for_queued_and_paused_work(tmp_path, status):
    _, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    if status == "WAITING":
        state = worker._set_pool_wait(state, waiting=True)
    elif status == "PAUSED":
        state = store.request_control(path, "pause")
        state = asyncio.run(worker.advance(path, None, scheduling_tasks=[state]))
    assert state["status"] == status
    assert queue_stop(worker, state)["status"] == "running"
    state = store.load(path)
    stop = state["controls"][-1]
    stopped = asyncio.run(worker.advance(
        path, None, scheduling_tasks=[state],
        expected_endpointless_control=(stop["control_id"], "stop_at_boundary"),
    ))
    assert stopped["status"] == stopped["terminal_state"] == "STOPPED"
    assert stopped["active_role"] is stopped["active_hop_id"] is None
    assert stopped["cleanup"]["state"] == "STOP_RELEASED"
    assert stopped["hops"] == state["hops"]
    assert "resume_status" not in stopped
    assert worker.runtime_db.get_command("cmd-stop")["status"] == "applied"


@pytest.mark.parametrize("finish", ["DEV", "DONE"])
def test_running_stop_survives_worker_recreation_and_never_clicks_stop(tmp_path, monkeypatch, finish):
    store, state, worker, path, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="running-stop")
    assert queue_stop(worker, state)["status"] == "running"
    worker = CDPAWorker(worker.config, store=store)
    actions = FakeActions()
    monkeypatch.setattr("playwright_auto.cdpa_worker.CDPATabActions", lambda *_: actions)
    expected = hop["expected_report_path"]

    async def finish_hop(working, active, *_args):
        assert working["status"] == "RUNNING"
        assert active["state"] == "waiting"
        assert actions.stop_calls == 0
        active.update(state="routed", route=finish, report_path=expected)
        if finish == "DEV":
            worker._append_hop(working, source_role="PLAN", target_role="DEV", handoff=expected)
        else:
            working.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None)

    monkeypatch.setattr(worker, "_waiting", finish_hop)
    monkeypatch.setattr(worker, "_schedule_repository_project", lambda *_: None)
    routed = asyncio.run(worker.advance(path, None, scheduling_tasks=store.discover()))
    assert routed["hops"][0]["state"] == "routed"
    assert routed["hops"][0]["receipt"] == hop["receipt"]
    result = asyncio.run(worker.advance(path, None, scheduling_tasks=store.discover()))
    assert actions.stop_calls == 0
    assert result["active_hop_id"] is None
    command = worker.runtime_db.get_command("cmd-stop")
    if finish == "DEV":
        assert result["status"] == "STOPPED"
        assert result["hops"][1]["state"] == "pre_send"
        assert command["status"] == "applied"
    else:
        assert result["status"] == "DONE"
        assert command["status"] == "failed"
        assert "terminal" in command["error"]


@pytest.mark.parametrize("mode", ["ambiguous-block", "safe-hold", "save-gap"])
def test_stop_refuses_ambiguous_send_and_safe_hold_without_manifest_mutation(tmp_path, mode):
    _, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    if mode == "ambiguous-block":
        state.update(status="BLOCKED", block_code="durable_send_provenance_missing")
    elif mode == "safe-hold":
        state.update(status="BLOCKED", block_code="resume_progress_unverified")
        hop["preboundary_recovery"] = {"phase": "recovery_required", "stage": "acquisition"}
    else:
        ledger = RequestLedger(hop["ledger_path"])
        ledger.begin(role=hop["physical_role"], prompt="once", request_id=hop["request_id"])
        ledger.update(hop["request_id"], status=RequestStatus.SENDING, attempts=1)
    state = store.save(path, state)
    before = path.read_bytes()
    rejected = queue_stop(worker, state)
    assert rejected["status"] == "failed"
    assert {"ambiguous-block": "durable_send_provenance_missing", "safe-hold": "SAFE HOLD",
            "save-gap": "ambiguous Send provenance"}[mode] in rejected["error"]
    assert path.read_bytes() == before


def test_done_report_is_resolved_by_projected_material_id_not_generic_filename(tmp_path):
    _, _, raw, _ = setup_task(tmp_path)
    task_id, team = raw["task_id"], raw["team"]
    relpath = f".plan/{team}/{team}-plan_turn1_{task_id}.md"
    data = b"# Final role report\n"
    (tmp_path / relpath).write_bytes(data)
    raw.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None,
               reports=[{"report_id": 1, "role": "PLAN", "turn": 1, "path": relpath}])
    _, db, server, thread = start_api(tmp_path)
    try:
        db.upsert_task_projections([build_task_projection(raw, tasks=[raw])])
        _, _, detail = request(server, "GET", f"/api/tasks/{task_id}")
        report = json.loads(detail)["reports"][0]
        assert report["material_id"] == "1"
        assert request(server, "GET", report["url"])[2] == data
        _, _, material = request(server, "GET", report["material_url"])
        assert json.loads(material)["content"] == data.decode()
        _, _, missing = request(server, "GET", f"/api/report/{task_id}/REPORT.md")
        assert json.loads(missing)["error"]["code"] == "MATERIAL_NOT_FOUND"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_stop_refuses_new_ambiguity_after_queueing_before_transport(tmp_path, monkeypatch):
    store, state, worker, path, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="stop-race")
    assert queue_stop(worker, state)["status"] == "running"
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(hop["request_id"], receipt=None)
    before = ledger.path.read_bytes()

    async def forbidden(*_args):
        pytest.fail("ambiguous Stop must not touch transport")

    monkeypatch.setattr(worker, "_waiting", forbidden)
    result = asyncio.run(worker.advance(path, None, scheduling_tasks=store.discover()))
    assert result["status"] == "RUNNING"
    assert result["hops"] == state["hops"]
    assert ledger.path.read_bytes() == before
    command = worker.runtime_db.get_command("cmd-stop")
    assert command["status"] == "failed"
    assert "ambiguous Send provenance" in command["error"]


def test_cli_stop_posts_only_boundary_control_and_reports_mailbox_result(tmp_path, monkeypatch, capsys):
    config, store, state, worker = setup_task(tmp_path)
    _, db, server, thread = start_api(tmp_path)
    config = replace(config, dashboard_api_port=server.server_address[1])
    monkeypatch.setattr(cdpa_cli, "load_cdpa_config", lambda *_args, **_kwargs: config)
    worker.hydrate_runtime()

    def dispatch(_seconds):
        admitted = worker.dispatch_command_once()
        if admitted is not None:
            assert admitted["payload"]["action"] == "stop_at_boundary"
            asyncio.run(worker.advance(state["manifest_path"], None, scheduling_tasks=store.discover()))

    monkeypatch.setattr(cdpa_cli.time, "sleep", dispatch)
    try:
        assert cdpa_cli.main(["stop", "--task", state["task_id"]]) == 0
        out = capsys.readouterr().out
        command_id = next(line.split("=", 1)[1] for line in out.splitlines() if line.startswith("command_id="))
        assert db.get_command(command_id)["status"] == "applied"
        assert "status=STOPPED\n" in out
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
