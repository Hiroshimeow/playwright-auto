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
        self.acquire_calls = []

    async def acquire(self, state, role, *, clean_for_send=False):
        assert self.owned, "explicit fresh first allocation must use fresh_chat"
        self.acquire_calls.append((role, clean_for_send))
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=str(record["page_id"]),
            url=str(record["page_url"]),
            created=False,
            new_chat=False,
        )

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

    async def fresh_chat(self, state, role, *, url, clean_for_send=False):
        assert clean_for_send is True
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
        governing_objective="explicit fresh workflow",
        governing_directives=[],
    )
    return state, CDPAWorker(config, store=store)


def test_explicit_fresh_first_send_opens_plain_new_chat_without_bootstrap(tmp_path: Path):
    state, worker = _fresh_task(tmp_path)
    hop = _active_hop(state)
    actions = FreshActions()

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.fresh_urls == ["https://chatgpt.com/"]
    assert actions.acquire_calls == []
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
    assert actions.acquire_calls == [("PLAN", True)]
    assert role["page_id"] == "fresh-owned"
    assert role["context_source"] == "fresh"


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

        async def fresh_chat(self, current, role, *, url, clean_for_send=False):
            assert clean_for_send is True
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


def test_fresh_chat_goto_timeout_is_pre_send_acquisition_failure_not_blocked(tmp_path: Path):
    """Fix D: fresh_chat TimeoutError stays pre-Send; ledger attempts remain 0; safe recovery classification."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright_auto.durable import RequestLedger

    state, worker = _fresh_task(tmp_path)
    hop = _active_hop(state)
    request_id = hop["request_id"]

    class TimeoutOnFreshActions(FreshActions):
        async def fresh_chat(self, state, role, *, url, clean_for_send=False):
            raise PlaywrightTimeoutError("Page.goto timed out after 30000ms")

    actions = TimeoutOnFreshActions()
    asyncio.run(worker._pre_send(state, hop, actions))

    # Must remain pre-Send: hop state must not advance to sending
    assert hop.get("state") != "sending", "hop must not advance to sending after fresh_chat timeout"
    # Ledger must show 0 attempts (never clicked)
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.get(request_id)
    assert record is None or int(record.attempts or 0) == 0, (
        f"No send was attempted; ledger attempts must be 0, got {getattr(record, 'attempts', None)}"
    )
    # Must produce a safe retryable or recovery classification, not generic unexpected_error BLOCKED
    assert state.get("status") in {"BLOCKED", "RUNNING"}, f"Unexpected status: {state.get('status')}"
    if state.get("status") == "BLOCKED":
        assert state.get("block_code") not in {None, "unexpected_error"}, (
            f"Fresh chat timeout must not classify as unexpected_error, got {state.get('block_code')}"
        )
