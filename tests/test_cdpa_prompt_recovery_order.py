from __future__ import annotations

import asyncio

from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker import FakeActions, setup_task


def test_recovery_snapshot_precedes_delivery_and_final_directive(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-recovery-order",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    state["roles"]["PLAN"]["constructor_sent_generation"] = 0
    state["roles"]["PLAN"]["conversation_generation"] = 0
    hop = worker._append_hop(
        state,
        source_role="REVIEW",
        target_role="PLAN",
        handoff=".plan/alpha/alpha-review_turn1_task-recovery-order.md",
    )
    hop["context_recovery_snapshot"] = {
        "reason": "safe continuation",
        "send_provenance": {"classification": "pre_send"},
    }

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    prompt = str(hop["prompt"])
    assert "RECOVERY_SNAPSHOT" in prompt
    assert prompt.index("RECOVERY_SNAPSHOT") < prompt.index("REPORT")
    assert prompt.index("RECOVERY_SNAPSHOT") < prompt.index("Complete substantive work before returning a route.")
    assert prompt.endswith(
        "This is the closing role; DONE is available only when acceptance is satisfied."
    )
    assert _active_hop(state)["request_id"] == hop["request_id"]
