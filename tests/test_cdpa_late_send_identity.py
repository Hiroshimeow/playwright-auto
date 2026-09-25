from __future__ import annotations

import asyncio
from dataclasses import replace

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger, RequestStatus
from test_cdpa_worker import setup_task, FakeActions, RecordingCDPASendActions, RecordingCDPASendClient


def test_late_user_identity_reconciles_without_second_transport_send(tmp_path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    class Client(RecordingCDPASendClient):
        async def send(self, *args, **kwargs):
            accepted = await super().send(*args, **kwargs)
            return replace(accepted, accepted_via="stop_button", user_message_id=None, user_turn_id=None)
    client = Client()
    actions = RecordingCDPASendActions(client)
    asyncio.run(worker._sending(state, hop, actions))
    ledger = RequestLedger(hop["ledger_path"])
    assert ledger.get(hop["request_id"]).status is RequestStatus.SENDING
    assert state["active_action"] == "reconcile_send"
    assert state["status"] != "BLOCKED"
    asyncio.run(worker._sending(state, hop, actions))
    assert len(client.send_calls) == 1
    assert ledger.get(hop["request_id"]).attempts == 1
    assert ledger.get(hop["request_id"]).status is RequestStatus.SENT
    assert hop["receipt"]["user_message_id"] == "u1"


def test_listen_streaming_marks_markerless_send_sent_without_second_click(tmp_path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    class Client(RecordingCDPASendClient):
        async def send(self, *args, **kwargs):
            accepted = await super().send(*args, **kwargs)
            return replace(
                accepted,
                accepted_via="listen_streaming",
                user_message_id=None,
                user_turn_id=None,
            )

    client = Client()
    actions = RecordingCDPASendActions(client)
    asyncio.run(worker._sending(state, hop, actions))

    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.get(hop["request_id"])
    assert record.status is RequestStatus.SENT
    assert record.attempts == 1
    assert len(client.send_calls) == 1
    assert hop["receipt"]["accepted_via"] == "listen_streaming"
    assert hop["receipt"]["user_message_id"] is None
    assert state["active_action"] == "wait_response"
