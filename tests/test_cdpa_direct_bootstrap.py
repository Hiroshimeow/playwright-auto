import asyncio
from pathlib import Path

from playwright_auto.cdpa_actions import AcquiredRole, BranchBootstrapError
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions, bootstrap_record, ready_client


def test_bootstrap_donor_rotation_uses_direct_branch_route_only(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "direct bootstrap rotation",
        requested_team="direct-bootstrap",
        task_id="task-direct-bootstrap",
        bootstrap=bootstrap_record(),
    )
    second = {
        "conversation_id": "33333333-3333-4333-8333-333333333333",
        "assistant_message_id": "44444444-4444-4444-8444-444444444444",
    }
    state["bootstrap_task_donors"] = [second]
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def branch_from_anchor(
            self, current, role, *, source_conversation_id, assistant_message_id
        ):
            call = (role, source_conversation_id, assistant_message_id)
            self.branch_calls.append(call)
            if len(self.branch_calls) == 1:
                raise BranchBootstrapError("first donor unavailable")
            return AcquiredRole(
                client=ready_client(current, role),
                page_id="direct-second-donor",
                url="https://chatgpt.com/c/direct-second-donor",
                created=True,
                new_chat=True,
            )

    async def forbidden_ui(*_args, **_kwargs):
        raise AssertionError("bootstrap rotation must never click the DOM branch menu")

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", forbidden_ui, raising=False)
    actions = Actions()

    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", actions))

    assert acquired is not None
    assert len(actions.branch_calls) == 2
    assert actions.branch_calls[-1][1:] == (
        second["conversation_id"],
        second["assistant_message_id"],
    )
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"
    assert state["roles"]["PLAN"]["bootstrap_source_donor"] == second
