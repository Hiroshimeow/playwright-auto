import asyncio
from pathlib import Path

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions, _prepare_sent_waiting_task, ready_client
from test_operator_resume_recovery import _prepare_pristine_new_sending_record


TEMPORARY_CHAT_URL = "https://chatgpt.com/?temporary-chat=true"


def _temporary_task(tmp_path: Path, *, task_id: str):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Continue workflow in temporary chat",
        requested_team="alpha",
        task_id=task_id,
        temporary=True,
    )
    return store, state, CDPAWorker(config, store=store)


class TemporaryWorkflowActions(FakeActions):
    def __init__(self, *, owned: bool = False):
        super().__init__()
        self.owned = owned
        self.fresh_urls = []

    async def acquire(self, state, role, *, clean_for_send=False):
        assert self.owned, "temporary workflow must reuse only the owned generation"
        assert clean_for_send is True
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=str(record["page_id"]),
            url=str(record["page_url"]),
            created=False,
            new_chat=False,
        )

    async def reopen(self, *_args, **_kwargs):
        raise AssertionError("temporary workflow must not reopen a saved conversation")

    async def locate_owned(self, state, role):
        if not self.owned:
            return None
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=str(record["page_id"]),
            url=str(record["page_url"]),
            created=False,
            new_chat=False,
        )

    async def fresh_chat(self, state, role, *, url, clean_for_send=False):
        assert clean_for_send is True
        self.fresh_urls.append(url)
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=f"temporary-{record['physical_role']}",
            url=url,
            created=True,
            new_chat=True,
        )


def test_workflow_temporary_first_send_opens_exact_temporary_url(tmp_path: Path):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-first")
    hop = _active_hop(state)
    actions = TemporaryWorkflowActions()

    asyncio.run(worker._pre_send(state, hop, actions))

    role = state["roles"]["PLAN"]
    assert actions.fresh_urls == [TEMPORARY_CHAT_URL]
    assert role["page_url"] == TEMPORARY_CHAT_URL
    assert hop["state"] == "sending"


def test_workflow_temporary_same_generation_reuses_owned_tab(tmp_path: Path):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-owned")
    role = state["roles"]["PLAN"]
    role.update(
        page_id="temporary-plan-page",
        page_url="https://chatgpt.com/c/current-temporary-generation",
        online=True,
        conversation_generation=1,
    )
    actions = TemporaryWorkflowActions(owned=True)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.fresh_urls == []
    assert role["page_id"] == "temporary-plan-page"
    assert role["page_url"] == "https://chatgpt.com/c/current-temporary-generation"
    assert hop["state"] == "sending"


def test_cross_endpoint_accepted_temporary_recovery_waits_for_same_team_endpoint(tmp_path: Path):
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-temp-cross-endpoint"
    )
    state["conversation_mode"] = "temporary"
    old_endpoint = store.endpoints.resolve()["endpoint_id"]
    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    role = state["roles"]["PLAN"]
    role.update(
        browser_endpoint_id=old_endpoint,
        page_id="lost-temporary-page",
        page_url="https://chatgpt.com/c/lost-temporary-generation",
        online=False,
    )
    original_request_id = hop["request_id"]

    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="temporary endpoint offline"
    ) is False

    assert _active_hop(state)["request_id"] == original_request_id
    assert role["browser_endpoint_id"] == old_endpoint
    assert state["status"] == "WAITING"
    assert state["waiting_code"] == "team_endpoint_unavailable"


def test_same_endpoint_pristine_temporary_resume_starts_new_temporary_generation(tmp_path: Path):
    _store, state, worker, _path, hop, ledger, _donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-temp-pristine-resume"
    )
    state["conversation_mode"] = "temporary"
    state["context_policy"] = "fresh"
    state.pop("bootstrap", None)
    role = state["roles"]["PLAN"]
    role["context_source"] = "fresh"
    role.pop("bootstrap_source_donor", None)
    original_request_id = hop["request_id"]

    class Actions(TemporaryWorkflowActions):
        async def branch_from_anchor(self, *_args, **_kwargs):
            raise AssertionError("temporary resume must not branch from a persistent donor")

        async def restart(self, *_args, **_kwargs):
            raise AssertionError("temporary resume must not use persistent restart")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(original_request_id)
    assert actions.fresh_urls == [TEMPORARY_CHAT_URL]
    assert hop["request_id"] == original_request_id
    assert hop["state"] == "sending"
    assert control["status"] == "applied"
    assert state["roles"]["PLAN"]["page_url"] == TEMPORARY_CHAT_URL
    assert current is not None
    assert current.attempts == 0
    assert current.receipt is None
