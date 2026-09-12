import asyncio
from pathlib import Path

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions, ready_client
from test_operator_resume_recovery import _prepare_legacy_donorless_pristine_new


class FreshActions(FakeActions):
    def __init__(self, *, owned=False):
        super().__init__()
        self.owned = owned
        self.fresh_urls = []

    async def acquire(self, *_args, **_kwargs):
        raise AssertionError("explicit fresh policy must not use persistent bootstrap acquisition")

    async def branch_from_anchor(self, *_args, **_kwargs):
        raise AssertionError("explicit fresh policy must not branch from a bootstrap donor")

    async def reopen(self, *_args, **_kwargs):
        raise AssertionError("explicit fresh policy must not reopen a saved conversation")

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

    async def fresh_chat(self, state, role, *, url):
        self.fresh_urls.append(url)
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=f"fresh-{record['physical_role']}",
            url=url,
            created=True,
            new_chat=True,
        )


def _fresh_task(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "explicit fresh workflow",
        requested_team="fresh-workflow",
        task_id="task-fresh-workflow",
        context_policy="fresh",
    )
    return state, CDPAWorker(config, store=store)


def test_explicit_fresh_first_send_opens_plain_new_chat_without_bootstrap(tmp_path: Path):
    state, worker = _fresh_task(tmp_path)
    hop = _active_hop(state)
    actions = FreshActions()

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.fresh_urls == ["https://chatgpt.com/"]
    assert state["roles"]["PLAN"]["context_source"] == "fresh"
    assert hop["state"] == "sending"


def test_explicit_fresh_same_generation_reuses_owned_page(tmp_path: Path):
    state, worker = _fresh_task(tmp_path)
    role = state["roles"]["PLAN"]
    role.update(
        page_id="fresh-owned",
        page_url="https://chatgpt.com/c/fresh-owned",
        online=True,
        context_source="fresh",
        conversation_generation=1,
    )
    actions = FreshActions(owned=True)

    asyncio.run(worker._pre_send(state, _active_hop(state), actions))

    assert actions.fresh_urls == []
    assert role["page_id"] == "fresh-owned"


def test_explicit_fresh_resume_never_rebases_to_default_bootstrap(tmp_path: Path):
    _store, state, worker, _path, hop, _ledger, _donor = (
        _prepare_legacy_donorless_pristine_new(
            tmp_path, task_id="task-fresh-resume-no-rebase"
        )
    )
    state["context_policy"] = "fresh"
    calls = []

    class Actions:
        async def locate_owned(self, _state, _role):
            return None

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise AssertionError("explicit fresh recovery must not branch from default bootstrap")

        async def restart(self, current, role, *, known_automated_draft=None):
            calls.append(("restart", known_automated_draft))
            record = current["roles"][role]
            return AcquiredRole(
                client=ready_client(current, role),
                page_id=f"fresh-resume-{record['physical_role']}",
                url="https://chatgpt.com/",
                created=True,
                new_chat=True,
            )

        async def fresh_chat(self, current, role, *, url):
            calls.append(("fresh_chat", url))
            record = current["roles"][role]
            return AcquiredRole(
                client=ready_client(current, role),
                page_id=f"fresh-resume-{record['physical_role']}",
                url=url,
                created=True,
                new_chat=True,
            )

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    assert control["status"] == "applied"
    assert state["active_action"] == "send"
    assert calls and calls[0][0] in {"restart", "fresh_chat"}
