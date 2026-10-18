"""Change URL is a durable role reassignment, not Send/Resume or endpoint migration."""
from __future__ import annotations

import json

import pytest

from playwright_auto.cdpa_commands import conversation_identity
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.dashboard_api import APIError, DashboardAPI
from test_cdpa_worker import setup_task

CID = "6acb02f9-751c-83ee-8b7b-b46fd4ea6235"
OTHER = "11111111-1111-4111-8111-111111111111"
URL = f"https://chatgpt.com/g/g-p-project/c/{CID}"
PLAIN = f"https://chatgpt.com/c/{CID}"
OTHER_URL = f"https://chatgpt.com/c/{OTHER}"



def pinned_task(tmp_path, **kwargs):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.cdpa_worker import CDPAWorker
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id=kwargs.get("task_id", "task-a"),
        browser_endpoint_id="legacy",
        governing_objective="Implement exact production behavior",
        governing_directives=[],
    )
    return config, store, state, CDPAWorker(config, store=store)

def test_role_url_change_persists_without_send_or_endpoint_change(tmp_path):
    _config, store, state, _worker = pinned_task(tmp_path, task_id="role-url-one")
    path = state["manifest_path"]
    role = "PLAN"
    endpoint = state["roles"][role].get("browser_endpoint_id") or state["browser_endpoint_id"]
    original = _active_hop(state)
    request_id = original["request_id"]
    updated = store.change_role_url(
        path, role=role, url=URL, expected_browser_endpoint_id=endpoint,
        external_command_id="cmd-change-role-url-one",
    )
    hop = _active_hop(updated)
    assert updated["roles"][role]["page_url"] == URL
    assert (updated["roles"][role].get("browser_endpoint_id") or updated["browser_endpoint_id"]) == endpoint
    assert updated["roles"][role]["page_id"] is None
    assert hop["request_id"] == request_id
    assert hop.get("conversation_url") is None
    assert updated["roles"][role]["role_url_binding"]["status"] == "PENDING"
    assert updated["roles"][role]["role_url_generation"] == 1
    assert hop["state"] == "pre_send"
    assert updated["status"] == state["status"]
    assert len(updated["role_url_revisions"]) == 1
    assert store.load(path)["roles"][role]["page_url"] == URL
    assert updated["roles"][role]["role_url_binding"]["requested_url"] == URL

    replay = store.change_role_url(
        path, role=role, url=URL, expected_browser_endpoint_id=endpoint,
        external_command_id="cmd-change-role-url-one",
    )
    assert len(replay["role_url_revisions"]) == 1
    with pytest.raises(ValueError, match="replay provenance"):
        store.change_role_url(
            path, role=role, url=OTHER_URL, expected_browser_endpoint_id=endpoint,
            external_command_id="cmd-change-role-url-one",
        )


def test_paused_inflight_alias_change_preserves_request_and_receipt(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id="role-url-paused")
    endpoint = state["roles"]["PLAN"].get("browser_endpoint_id") or state["browser_endpoint_id"]

    def paused(current):
        role = current["roles"]["PLAN"]
        role.update(page_id="old-owned-page", page_url="https://chatgpt.com/",
                    online=True)
        parent = _active_hop(current)
        parent.update(
            state="routed", route="PAUSE", conversation_url=PLAIN,
            receipt={"conversation_id": CID},
        )
        child = worker._append_hop(
            current, source_role="PLAN", target_role="PLAN",
            handoff="Continue review", kind="handoff",
        )
        child.update(state="sending", conversation_url=None,
                     prompt_sha256="immutable-prompt")
        current.update(status="PAUSED", kanban_column="PAUSED",
                       active_action="send")
        return current

    state = store.update(state["manifest_path"], paused)
    old = _active_hop(state)
    updated = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=endpoint,
        external_command_id="cmd-paused-alias",
    )
    new = _active_hop(updated)
    assert updated["status"] == "PAUSED"
    assert updated["roles"]["PLAN"]["page_url"] == URL
    assert new["request_id"] == old["request_id"]
    assert new["state"] == "sending"
    assert new["conversation_url"] is None
    assert new["prompt_sha256"] == "immutable-prompt"
    assert updated["hops"][0]["receipt"] == state["hops"][0]["receipt"]
    newer = store.change_role_url(
        state["manifest_path"], role="PLAN", url=OTHER_URL,
        expected_browser_endpoint_id=endpoint,
        external_command_id="cmd-paused-foreign",
    )
    assert newer["roles"]["PLAN"]["page_url"] == OTHER_URL
    assert newer["roles"]["PLAN"]["role_url_generation"] == 2
    assert _active_hop(newer)["prompt_sha256"] == "immutable-prompt"


def test_change_role_url_rejects_foreign_owner_and_wrong_endpoint(tmp_path):
    _config, store, first, _worker = pinned_task(tmp_path, task_id="url-first")
    endpoint = first["roles"]["PLAN"].get("browser_endpoint_id") or first["browser_endpoint_id"]
    store.change_role_url(
        first["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=endpoint, external_command_id="cmd-first",
    )
    _config2, _store2, second, _worker2 = pinned_task(tmp_path, task_id="url-second")
    with pytest.raises(ValueError, match="already reserved"):
        store.change_role_url(
            second["manifest_path"], role="PLAN", url=PLAIN,
            expected_browser_endpoint_id=endpoint, external_command_id="cmd-collision",
        )
    with pytest.raises(ValueError, match="endpoint mismatch"):
        store.change_role_url(
            second["manifest_path"], role="PLAN", url=OTHER_URL,
            expected_browser_endpoint_id="wrong-endpoint", external_command_id="cmd-mismatch",
        )
    with pytest.raises(ValueError, match="role"):
        store.change_role_url(
            second["manifest_path"], role="MISSING", url=OTHER_URL,
            expected_browser_endpoint_id=endpoint, external_command_id="cmd-missing-role",
        )


def test_change_url_rejects_other_task_hop_only_reservation(tmp_path):
    _config, store, state, _worker = pinned_task(tmp_path, task_id="url-target")
    solo = store.create_task(
        "Solo with existing conversation",
        requested_team="solo-existing-reservation",
        task_id="solo-existing-reservation",
        workflow_mode="solo", roles=("REVIEW",),
        browser_endpoint_id="legacy",
        conversation_url=URL,
    )
    assert solo["roles"]["REVIEW"]["page_url"] is None
    assert solo["hops"][0]["conversation_url"] == URL
    with pytest.raises(ValueError, match="already reserved"):
        store.change_role_url(
            state["manifest_path"], role="PLAN", url=PLAIN,
            expected_browser_endpoint_id=state["browser_endpoint_id"],
            external_command_id="cmd-url-reservation-collision",
        )


def test_role_url_api_requires_url_role_and_endpoint(tmp_path):
    config, _store, _state, _worker = pinned_task(tmp_path)
    payload, version = DashboardAPI.normalize_role_url_change({
        "role": "review", "url": URL,
        "expected_browser_endpoint_id": "endpoint-9212",
        "expected_task_version": 10,
    })
    assert payload == {
        "role": "REVIEW", "url": URL,
        "expected_browser_endpoint_id": "endpoint-9212",
    }
    assert version == 10
    home, _ = DashboardAPI.normalize_role_url_change({
        "role": "REVIEW", "url": "https://chatgpt.com/",
        "expected_browser_endpoint_id": "endpoint-9212",
    })
    assert home["url"] == "https://chatgpt.com/"
    for bad in (
        {"role": "REVIEW", "url": "https://chatgpt.com/c/not-a-uuid"},
        {"role": "REVIEW", "url": URL},
        {"role": "REVIEW", "url": URL, "expected_browser_endpoint_id": "one", "extra": "x"},
        {"role": "REVIEW", "url": URL, "expected_browser_endpoint_id": "one", "expected_task_version": True},
    ):
        with pytest.raises(APIError):
            DashboardAPI.normalize_role_url_change(bad)


def test_role_url_api_endpoint_enqueues_idempotent_command(tmp_path):
    from test_dashboard_api import request, start_api

    _config, db, server, thread = start_api(tmp_path)
    body = {
        "role": "REVIEW", "url": URL,
        "expected_browser_endpoint_id": "endpoint-9212",
        "expected_task_version": 9,
    }
    try:
        headers = {"Idempotency-Key": "url-review-9212"}
        code, _, result = request(
            server, "POST", "/api/tasks/task-a/role-url", body=body, headers=headers
        )
        assert code == 202
        accepted = json.loads(result)
        command = db.get_command(accepted["command_id"])
        assert command["kind"] == "change_role_url"
        assert command["payload"] == {
            "role": "REVIEW", "url": URL,
            "expected_browser_endpoint_id": "endpoint-9212",
        }
        assert command["task_id"] == "task-a"
        assert command["expected_task_version"] == 9
        repeated, _, same = request(
            server, "POST", "/api/tasks/task-a/role-url",
            body=body, headers=headers,
        )
        assert repeated == 202
        assert json.loads(same) == accepted
        for index, bad in enumerate((
            {"role": "REVIEW", "url": URL},
            {**body, "expected_task_version": "not-an-int"},
        )):
            bad_status, _, _ = request(
                server, "POST", "/api/tasks/task-a/role-url",
                body=bad, headers={"Idempotency-Key": f"url-bad-{index}"},
            )
            assert bad_status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_change_url_ui_is_role_scoped_and_endpoint_readonly():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "playwright_auto"
    html = (root / "dashboard.html").read_text(encoding="utf-8")
    app = (root / "dashboard_assets" / "app.js").read_text(encoding="utf-8")
    detail = (root / "dashboard_assets" / "views" / "task_detail.js").read_text(encoding="utf-8")
    assert 'id="change-url-dialog"' in html
    assert 'name="expected_browser_endpoint_id"' in html
    assert 'id="change-url-endpoint-name"' in html
    assert 'id="change-url-endpoint-id"' in html
    assert 'id="change-url-endpoint-address"' in html
    assert 'name="current_url" type="text" readonly' in html
    assert 'name="url" type="url" required' in html
    assert 'data.change_role_url' not in html
    assert 'node.dataset.changeRoleUrl = task.task_id' in detail
    assert 'const missing = !role || !endpointId || changeUrlDetail?.endpoint_pinned === false;' in app
    assert 'state.runtime?.browser?.endpoints' in app
    assert 'expected_browser_endpoint_id: endpointId' in app
    assert '/role-url' in app
    assert '/assets/app.js?v=20261011-p0012-challenge-confirm-v1' in html
    assert './views/task_detail.js?v=20261011-p0012-challenge-confirm-v1' in app


def test_change_role_url_does_not_change_existing_ten_minute_timeout():
    from playwright_auto.role_runtime.model import Policy
    assert Policy().timeout_seconds == 600.0


def test_change_role_url_runtime_command_idempotent_without_browser(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-command")
    worker.hydrate_runtime()
    role = "PLAN"
    endpoint = state["roles"][role].get("browser_endpoint_id") or state["browser_endpoint_id"]
    worker.runtime_db.enqueue_command(
        command_id="cmd-url-change", idempotency_key="change-role-url",
        kind="change_role_url", task_id=state["task_id"],
        expected_task_version=None,
        payload={"role": role, "url": URL,
                 "expected_browser_endpoint_id": endpoint},
    )
    first = worker.dispatch_command_once()
    assert first["status"] == "accepted"
    assert first["result"]["url"] == URL
    assert first["result"]["binding_status"] == "PENDING"
    changed = store.load_task_id(state["task_id"])
    assert changed["roles"][role]["page_url"] == URL
    assert len(changed["role_url_revisions"]) == 1
    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            ("cmd-url-change",),
        )
    second = worker.dispatch_command_once()
    assert second["status"] == "accepted"
    assert second["result"]["reconciled"] is True
    assert len(store.load_task_id(state["task_id"])["role_url_revisions"]) == 1


def test_home_url_is_pending_not_durable_homepage(tmp_path):
    _config, store, state, _worker = pinned_task(tmp_path, task_id="url-new-pending")
    changed = store.change_role_url(
        state["manifest_path"], role="PLAN", url="https://chatgpt.com/",
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="new-chat-request",
    )
    role = changed["roles"]["PLAN"]
    assert role["page_url"] is None
    assert role["role_url_binding"]["status"] == "NEW_PENDING"
    assert role["role_url_binding"]["requested_url"] == "https://chatgpt.com/"
    assert role["role_url_generation"] == 1
    assert store.load(state["manifest_path"])["roles"]["PLAN"] == role


@pytest.mark.parametrize("binding_status", ["PENDING", "BOUND", "NEW_PENDING"])
def test_legacy_new_chat_supersedes_prior_url_intent_only_after_success(tmp_path, binding_status):
    import asyncio
    from copy import deepcopy
    from pathlib import Path
    from test_cdpa_worker import FakeActions

    _config, store, state, worker = pinned_task(tmp_path, task_id="legacy-new-chat")

    def paused(current):
        parent = _active_hop(current)
        parent.update(state="routed", conversation_url=PLAIN,
                      receipt={"conversation_id": CID, "attempts": 1,
                               "user_message_id": "accepted-user"})
        worker._append_hop(current, source_role="PLAN", target_role="PLAN",
                           handoff="Continue without replay", kind="handoff")
        current.update(status="PAUSED", kanban_column="PAUSED",
                       pause_reason="operator pause")
        return current

    state = store.update(state["manifest_path"], paused)
    state = store.change_role_url(
        state["manifest_path"], role="PLAN",
        url="https://chatgpt.com/" if binding_status == "NEW_PENDING" else URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="prior-url",
    )
    state = store.update(state["manifest_path"], lambda current: (
        current["roles"]["PLAN"]["role_url_binding"].update(
            status=binding_status, page_id="old-page", error="previous binding error"
        ), current
    )[-1])
    state = store.request_control(
        state["manifest_path"], "new_chat", role="PLAN",
        external_command_id="later-new-chat",
    )
    before = deepcopy(state)
    actions = FakeActions()
    asyncio.run(worker._apply_control(state, actions, Path(state["manifest_path"])))
    saved = store.load(state["manifest_path"])
    role = saved["roles"]["PLAN"]
    control = saved["controls"][-1]
    assert control["status"] == "applied"
    assert control["result"]["new_chat"] is True
    assert role["role_url_binding"] == {
        "generation": 2, "requested_url": "https://chatgpt.com/",
        "status": "NEW_PENDING", "observed_url": None, "error": None,
        "page_id": control["result"]["page_id"],
    }
    assert role["page_url"] is None
    assert role["page_id"] == control["result"]["page_id"]
    assert role["role_url_generation"] == 2
    assert role["conversation_generation"] == (
        int(before["roles"]["PLAN"].get("conversation_generation") or 0) + 1
    )
    assert role["constructor_sent_generation"] is None
    assert saved["browser_endpoint_id"] == before["browser_endpoint_id"]
    assert saved["status"] == "PAUSED"
    assert saved["pause_reason"] == before["pause_reason"]
    assert saved["hops"] == before["hops"]
    assert saved["role_url_revisions"][:-1] == before["role_url_revisions"]
    revision = saved["role_url_revisions"][-1]
    assert revision["url"] == "https://chatgpt.com/"
    assert revision["previous_url"] == before["roles"]["PLAN"]["page_url"]
    assert revision["external_command_id"] == "later-new-chat"
    located = list(actions.located_roles)
    asyncio.run(worker._reconcile_role_url_bindings(
        saved, actions, endpoint_id=saved["browser_endpoint_id"],
    ))
    assert actions.located_roles == located
    assert actions.fresh_calls == []
    assert saved["hops"] == before["hops"]


def test_legacy_new_chat_supersedes_prior_url_mailbox_command(tmp_path):
    import asyncio
    from pathlib import Path
    from test_cdpa_worker import FakeActions

    _config, store, state, worker = pinned_task(tmp_path, task_id="legacy-new-chat-mailbox")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="old-url", idempotency_key="old-url",
        kind="change_role_url", task_id=state["task_id"],
        expected_task_version=None,
        payload={"role": "PLAN", "url": URL,
                 "expected_browser_endpoint_id": state["browser_endpoint_id"]},
    )
    assert worker.dispatch_command_once()["status"] == "accepted"
    state = store.request_control(state["manifest_path"], "new_chat", role="PLAN")
    asyncio.run(worker._apply_control(state, FakeActions(), Path(state["manifest_path"])))
    saved = store.load(state["manifest_path"])
    assert saved["controls"][-1]["status"] == "applied"
    worker._sync_role_url_command(saved)
    prior = worker.runtime_db.get_command("old-url")
    assert prior["status"] == "failed"
    assert "supersed" in prior["error"].lower()
    assert saved["role_url_revisions"][-1]["external_command_id"] == (
        state["controls"][-1]["command"]["command_id"]
    )


def test_concurrent_change_url_fences_legacy_new_chat_persistence(tmp_path):
    import asyncio
    from pathlib import Path
    from test_cdpa_worker import FakeActions

    _config, store, state, worker = pinned_task(tmp_path, task_id="legacy-new-chat-race")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="prior-url",
    )
    state = store.request_control(
        state["manifest_path"], "new_chat", role="PLAN",
        external_command_id="queued-new-chat",
    )

    class ConcurrentActions(FakeActions):
        async def new_chat(self, current, role):
            store.change_role_url(
                current["manifest_path"], role=role, url=OTHER_URL,
                expected_browser_endpoint_id=current["browser_endpoint_id"],
                external_command_id="concurrent-url",
            )
            return await super().new_chat(current, role)

    with pytest.raises(ValueError, match="task changed"):
        asyncio.run(worker._apply_control(
            state, ConcurrentActions(), Path(state["manifest_path"]),
        ))
    saved = store.load(state["manifest_path"])
    assert saved["roles"]["PLAN"]["page_url"] == OTHER_URL
    assert saved["roles"]["PLAN"]["role_url_generation"] == 2
    assert saved["roles"]["PLAN"]["role_url_binding"]["requested_url"] == OTHER_URL
    assert saved["controls"][-1]["status"] == "requested"
    assert [item["external_command_id"] for item in saved["role_url_revisions"]] == [
        "prior-url", "concurrent-url",
    ]


def test_failed_legacy_new_chat_preserves_prior_url_intent(tmp_path):
    import asyncio
    from copy import deepcopy
    from pathlib import Path
    from test_cdpa_worker import FakeActions

    _config, store, state, worker = pinned_task(tmp_path, task_id="failed-legacy-new-chat")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="prior-url",
    )
    state = store.request_control(
        state["manifest_path"], "new_chat", role="PLAN",
        external_command_id="failed-new-chat",
    )
    before = deepcopy(state)

    class FailedActions(FakeActions):
        async def new_chat(self, *_args):
            raise RuntimeError("New Chat failed before acquisition")

    asyncio.run(worker._apply_control(state, FailedActions(), Path(state["manifest_path"])))
    saved = store.load(state["manifest_path"])
    assert saved["controls"][-1]["status"] == "rejected"
    assert "New Chat failed" in saved["controls"][-1]["result"]
    for key in ("roles", "role_url_revisions", "hops", "status"):
        assert saved[key] == before[key]


def test_stale_receipt_cannot_replace_new_desired_role_url(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-stale-receipt")
    state = store.update(state["manifest_path"], lambda s: (
        s["hops"][0].update(receipt={"conversation_id": CID}), s
    )[-1])
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=OTHER_URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="operator-new-target",
    )
    worker._canonicalize_receipt_conversation_url(state, state["hops"][0])
    assert state["roles"]["PLAN"]["page_url"] == OTHER_URL
    assert state["roles"]["PLAN"]["role_url_binding"]["status"] == "PENDING"
    assert state["hops"][0]["receipt"]["conversation_id"] == CID


def test_new_pending_receipt_does_not_bind_without_browser_proof(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-unverified-fresh")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url="https://chatgpt.com/",
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="fresh-command",
    )
    state["hops"][0]["role_url_generation"] = 1
    state["hops"][0]["receipt"] = {"conversation_id": CID}
    worker._canonicalize_receipt_conversation_url(state, state["hops"][0])
    assert state["roles"]["PLAN"]["page_url"] is None
    assert state["roles"]["PLAN"]["role_url_binding"]["status"] == "NEW_PENDING"


def test_rebind_existing_operator_target_requires_owned_verified_page(tmp_path):
    import asyncio
    from types import SimpleNamespace
    _config, store, state, worker = pinned_task(tmp_path, task_id="rebind-existing")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="existing",
    )
    role = state["roles"]["PLAN"]

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                page_id="verified", url=URL, session_id=CID,
                page_role=role["physical_role"], page_task_id=state["task_id"],
                page_team=state["team"], composer_text="", attachment_markers=(),
                stop_visible=False,
            )

    class Actions:
        calls = 0
        async def locate_owned(self, *_args): return None
        async def reopen(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(page_id="verified", url=URL, client=Client())

    actions = Actions()
    asyncio.run(worker._reconcile_role_url_bindings(
        state, actions, endpoint_id=state["browser_endpoint_id"]
    ))
    assert actions.calls == 1
    assert role["role_url_binding"]["status"] == "BOUND"
    assert role["page_id"] == "verified"


def test_old_pre_send_hop_is_never_sent_into_operator_new_binding(tmp_path):
    import asyncio
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-stale-presend")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="url-command",
    )
    class Actions:
        async def preflight_role(self, *_args, **_kwargs):
            raise AssertionError("stale request must not reach browser")
    asyncio.run(worker._pre_send(state, _active_hop(state), Actions()))
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "operator_url_override_requires_fresh_hop"


def test_new_homepage_acquisition_does_not_claim_verified_conversation_without_send(tmp_path):
    from types import SimpleNamespace
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-fresh-must-send")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url="https://chatgpt.com/",
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="url-fresh-command",
    )
    worker._record_acquired(state, "PLAN", SimpleNamespace(
        url=PLAIN, page_id="fake-page", new_chat=True, created=True,
        conversation_id=CID,
    ))
    assert state["roles"]["PLAN"]["page_url"] is None
    assert state["roles"]["PLAN"]["role_url_binding"]["status"] == "NEW_PENDING"


@pytest.mark.parametrize("snapshot_url", [PLAIN, URL])
def test_new_homepage_canonicalizes_only_proven_accepted_send_with_project_url(tmp_path, snapshot_url):
    from dataclasses import replace
    from types import SimpleNamespace
    from test_cdpa_worker import _prepare_sent_waiting_task

    store, state, worker, _path, hop, receipt, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="url-fresh-accepted"
    )
    enriched = replace(receipt, conversation_id=CID)
    hop["receipt"] = enriched.to_dict()
    hop["role_url_generation"] = 1
    state["roles"]["PLAN"].update(
        page_url=None, role_url_generation=1,
        role_url_binding={"generation": 1, "requested_url": "https://chatgpt.com/",
                          "status": "NEW_PENDING", "observed_url": None,
                          "error": None, "page_id": enriched.binding.page_id},
    )
    snapshot = SimpleNamespace(
        url=snapshot_url, page_id=enriched.binding.page_id, session_id=CID,
        page_role=state["roles"]["PLAN"]["physical_role"],
        page_team=state["team"], page_task_id=state["task_id"],
    )
    worker._bind_verified_new_chat(state, hop, snapshot)
    assert state["roles"]["PLAN"]["page_url"] == snapshot_url
    assert state["roles"]["PLAN"]["role_url_binding"]["status"] == "BOUND"

    changed = replace(enriched, conversation_id=OTHER)
    state["roles"]["PLAN"]["role_url_binding"]["status"] = "NEW_PENDING"
    state["roles"]["PLAN"]["page_url"] = None
    hop["receipt"] = changed.to_dict()
    worker._bind_verified_new_chat(state, hop, snapshot)
    assert state["roles"]["PLAN"]["page_url"] is None
    assert state["roles"]["PLAN"]["role_url_binding"]["status"] == "NEW_PENDING"


def test_transport_stale_acquisition_does_not_mutate_new_role_generation(tmp_path):
    from copy import deepcopy
    from pathlib import Path
    from types import SimpleNamespace
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-old-transport")
    before = deepcopy(state)
    stale = deepcopy(state)
    worker._record_acquired(stale, "PLAN", SimpleNamespace(
        url=PLAIN, page_id="stale-old-page", new_chat=False, created=True,
        conversation_id=CID,
    ))
    current = store.change_role_url(
        state["manifest_path"], role="PLAN", url=OTHER_URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="override-stale",
    )
    saved = worker._persist_transport_result(Path(state["manifest_path"]), before, stale)
    assert saved["roles"]["PLAN"]["page_url"] == OTHER_URL
    assert saved["roles"]["PLAN"].get("page_id") != "stale-old-page"
    assert saved["roles"]["PLAN"]["role_url_generation"] == 1
    assert saved["roles"]["PLAN"]["role_url_binding"]["status"] == "PENDING"


def test_change_url_pending_still_schedules_paused_blocked_waiting_roles(tmp_path):
    from playwright_auto.cdpa_runtime_registry import CDPARuntimeRegistry
    _config, store, state, _worker = pinned_task(tmp_path, task_id="url-paused-due")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=OTHER_URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="queued-override",
    )
    for status in ("PAUSED", "BLOCKED", "WAITING"):
        candidate = json.loads(json.dumps(state))
        candidate["status"] = status
        if status == "WAITING":
            candidate["waiting"] = {"reason": "dependency", "waiting_on": []}
        registry = CDPARuntimeRegistry.hydrate([candidate], now=100.0)
        assert state["task_id"] in registry.due_task_ids(now=110.0)


def test_change_url_rebind_retains_true_manual_draft_without_navigation(tmp_path):
    import asyncio
    from types import SimpleNamespace
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-manual-preserve")
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="manual-block",
    )
    role = state["roles"]["PLAN"]
    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                page_id="old-page", url=PLAIN,
                page_role=role["physical_role"], page_task_id=state["task_id"],
                page_team=state["team"], composer_text="manual draft",
                attachment_markers=(), stop_visible=False,
            )
    class Actions:
        async def locate_owned(self, *_args):
            return SimpleNamespace(page_id="old-page", url=PLAIN, client=Client())
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("manual draft must block navigation")
    asyncio.run(worker._reconcile_role_url_bindings(
        state, Actions(), endpoint_id=state["browser_endpoint_id"]
    ))
    binding = role["role_url_binding"]
    assert binding["status"] == "PENDING"
    assert "manual" in binding["error"].lower()


def test_waiting_operator_change_reaches_reconciliation_before_wait_return(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-wait-reconcile")
    state = store.update(
        state["manifest_path"],
        lambda s: (s.update(
            status="WAITING", kanban_column="WAITING",
            waiting={"reason": "dependency"},
        ) or s),
    )
    store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="wait-command",
    )
    monkeypatch.setattr(
        store, "refresh_scheduling",
        lambda *a, **k: (store.load(state["manifest_path"]), False),
    )
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_reconcile_role_url_bindings", probe)
    asyncio.run(worker.advance(state["manifest_path"], SimpleNamespace(pages=[]),
                               endpoint_id=state["browser_endpoint_id"]))
    probe.assert_awaited_once()


def test_operator_url_mailbox_promotes_only_after_durable_verified_binding(tmp_path):
    from copy import deepcopy
    from pathlib import Path
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-mailbox-applied")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="url-promotion", idempotency_key="url-promotion-key",
        kind="change_role_url", task_id=state["task_id"],
        expected_task_version=None,
        payload={"role": "PLAN", "url": URL,
                 "expected_browser_endpoint_id": state["browser_endpoint_id"]},
    )
    accepted = worker.dispatch_command_once()
    assert accepted["status"] == "accepted"
    before = store.load(state["manifest_path"])
    assert before["roles"]["PLAN"]["role_url_binding"]["status"] == "PENDING"
    assert worker.runtime_db.get_command("url-promotion")["status"] == "accepted"
    after = deepcopy(before)
    after["roles"]["PLAN"]["role_url_binding"].update(
        status="BOUND", observed_url=URL, error=None,
    )
    after["roles"]["PLAN"]["page_id"] = "verified-page"
    saved = worker._persist_transport_result(Path(state["manifest_path"]), before, after)
    assert saved["roles"]["PLAN"]["role_url_binding"]["status"] == "BOUND"
    result = worker.runtime_db.get_command("url-promotion")
    assert result["status"] == "applied"
    assert result["result"]["binding_status"] == "BOUND"
    assert result["result"]["generation"] == 1
    assert result["result"]["page_id"] == "verified-page"


def test_old_operator_command_superseded_but_newer_cannot_be_applied_without_bound(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-mailbox-supersession")
    worker.hydrate_runtime()
    for command_id, url in (("old-url", URL), ("new-url", OTHER_URL)):
        worker.runtime_db.enqueue_command(
            command_id=command_id, idempotency_key=command_id,
            kind="change_role_url", task_id=state["task_id"],
            expected_task_version=None,
            payload={"role": "PLAN", "url": url,
                     "expected_browser_endpoint_id": state["browser_endpoint_id"]},
        )
        assert worker.dispatch_command_once()["status"] == "accepted"
    assert worker.runtime_db.get_command("old-url")["status"] == "failed"
    assert "supersed" in worker.runtime_db.get_command("old-url")["error"].lower()
    assert worker.runtime_db.get_command("new-url")["status"] == "accepted"


@pytest.mark.parametrize(
    "status,reason_field,reason_value",
    [
        ("PAUSED", "pause_reason", "operator pause, do not send"),
        ("WAITING", "waiting", {"reason": "dependency", "waiting_on": []}),
        ("BLOCKED", "block_reason", "manual draft needs operator attention"),
    ],
)
def test_operator_url_browser_binding_never_resumes_nonrunning_workflow(
    tmp_path, status, reason_field, reason_value,
):
    import asyncio
    from types import SimpleNamespace
    _config, store, state, worker = pinned_task(
        tmp_path, task_id=f"url-nonrunning-{status.lower()}"
    )
    def mark(current):
        current["status"] = status
        current[reason_field] = reason_value
        if status == "BLOCKED":
            current["block_code"] = "manual_composer_conflict"
        return current
    state = store.update(state["manifest_path"], mark)
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id=f"status-override-{status}",
    )
    role = state["roles"]["PLAN"]
    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                page_id="verified-page", url=URL, session_id=CID,
                page_role=role["physical_role"], page_task_id=state["task_id"],
                page_team=state["team"], composer_text="", attachment_markers=(),
                stop_visible=False,
            )
    class Actions:
        calls = 0
        async def locate_owned(self, *_args): return None
        async def reopen(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(page_id="verified-page", url=URL, client=Client())
    actions = Actions()
    original = dict(_active_hop(state))
    asyncio.run(worker._reconcile_role_url_bindings(
        state, actions, endpoint_id=state["browser_endpoint_id"],
    ))
    assert actions.calls == 1
    assert role["role_url_binding"]["status"] == "BOUND"
    assert state["status"] == status
    assert state[reason_field] == reason_value
    if status == "BLOCKED":
        assert state["block_code"] == "manual_composer_conflict"
    assert len(state["hops"]) == 1
    assert _active_hop(state)["request_id"] == original["request_id"]
    assert _active_hop(state).get("superseded_by_hop_id") is None


def test_workflow_ui_uses_only_change_url_for_fresh_chat():
    from pathlib import Path
    source_root = Path(__file__).resolve().parents[1] / "src" / "playwright_auto"
    view = (source_root / "dashboard_assets" / "views" / "task_detail.js").read_text(encoding="utf-8")
    html = (source_root / "dashboard.html").read_text(encoding="utf-8")
    assert '["new_chat", "New chat"]' not in view
    assert "controls.append(changeUrlButton(detail))" in view
    assert "independentButton" in view
    assert "https://chatgpt.com/" in html
    assert "NEW_PENDING" in html


def test_bare_homepage_and_slash_share_canonical_change_url_intent(tmp_path):
    _config, store, state, _worker = pinned_task(tmp_path, task_id="url-bare-home")
    payload, _ = DashboardAPI.normalize_role_url_change({
        "role": "PLAN", "url": "https://chatgpt.com",
        "expected_browser_endpoint_id": state["browser_endpoint_id"],
    })
    assert payload["url"] == "https://chatgpt.com/"
    stored = store.change_role_url(
        state["manifest_path"], role="PLAN", url="https://chatgpt.com",
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="bare-home",
    )
    assert stored["roles"]["PLAN"]["role_url_binding"]["status"] == "NEW_PENDING"
    replay = store.change_role_url(
        state["manifest_path"], role="PLAN", url="https://chatgpt.com/",
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="bare-home",
    )
    assert len(replay["role_url_revisions"]) == 1


def test_waiting_accepted_a_binds_independent_b_and_retains_old_receipt(tmp_path):
    import asyncio
    from types import SimpleNamespace

    _config, store, state, worker = pinned_task(tmp_path, task_id="wait-a-to-b")
    old_receipt = {"conversation_id": CID}
    state = store.update(state["manifest_path"], lambda current: (
        current["hops"][0].update(
            state="waiting", receipt=old_receipt, conversation_url=None
        ),
        current["roles"]["PLAN"].update(page_id="old-a-page", page_url=PLAIN),
        current.update(status="WAITING", kanban_column="WAITING"),
        current,
    )[-1])
    state = store.change_role_url(
        state["manifest_path"], role="PLAN", url=OTHER_URL,
        expected_browser_endpoint_id=state["browser_endpoint_id"],
        external_command_id="wait-to-b",
    )
    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                page_id="new-b-page", url=OTHER_URL, session_id=OTHER,
                page_role=state["roles"]["PLAN"]["physical_role"],
                page_team=state["team"], page_task_id=state["task_id"],
            )
    class Actions:
        calls = 0
        async def locate_owned(self, *_args):
            raise AssertionError("old A must not be reattached to B")
        async def reopen(self, *_args, **kwargs):
            self.calls += 1
            assert kwargs["preserve_existing"] is True
            return SimpleNamespace(page_id="new-b-page", url=OTHER_URL, client=Client())
    actions = Actions()
    asyncio.run(worker._reconcile_role_url_bindings(
        state, actions, endpoint_id=state["browser_endpoint_id"],
    ))
    assert actions.calls == 1
    assert state["roles"]["PLAN"]["role_url_binding"]["status"] == "BOUND"
    assert state["roles"]["PLAN"]["page_id"] == "new-b-page"
    assert state["hops"][0]["receipt"] == old_receipt
    assert len(state["hops"]) == 1


def test_isolated_mailbox_to_real_actions_verified_b_without_closing_accepted_a(tmp_path, monkeypatch):
    import asyncio
    from copy import deepcopy
    from pathlib import Path
    from playwright_auto import cdpa_actions as actions_module
    from playwright_auto.cdpa_actions import CDPATabActions
    from test_cdpa_actions import FakeClient, FakeContext, FakePage

    config, store, state, worker = pinned_task(tmp_path, task_id="integration-accepted-a-target-b")
    physical = state["roles"]["PLAN"]["physical_role"]
    old_id = "receipt-original-page"
    original_receipt = {
        "conversation_id": CID,
        "binding": {"page_id": old_id, "role": physical},
        "attempts": 1,
    }
    state = store.update(state["manifest_path"], lambda current: (
        current["hops"][0].update(
            state="waiting", receipt=original_receipt, conversation_url=PLAIN
        ),
        current["roles"]["PLAN"].update(page_id=old_id, page_url=PLAIN),
        current.update(status="WAITING", kanban_column="WAITING"),
        current,
    )[-1])
    old = FakePage(page_id=old_id, role=physical, team=state["team"], task_id=state["task_id"])
    old.url = PLAIN
    old.snapshot_value.url = PLAIN
    old.snapshot_value.stop_visible = True
    new = FakePage(page_id="bound-b-page", role=physical, team=state["team"], task_id=state["task_id"])
    new.url = OTHER_URL
    new.snapshot_value.url = OTHER_URL
    context = FakeContext([old, new])
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="integration-b", idempotency_key="integration-b",
        kind="change_role_url", task_id=state["task_id"],
        expected_task_version=None,
        payload={"role": "PLAN", "url": OTHER_URL,
                 "expected_browser_endpoint_id": state["browser_endpoint_id"]},
    )
    assert worker.dispatch_command_once()["status"] == "accepted"
    before = store.load(state["manifest_path"])
    active = deepcopy(before)
    actions = CDPATabActions(context, config)
    asyncio.run(worker._reconcile_role_url_bindings(
        active, actions, endpoint_id=state["browser_endpoint_id"],
    ))
    result = worker._persist_transport_result(Path(state["manifest_path"]), before, active)
    assert result["roles"]["PLAN"]["role_url_binding"]["status"] == "BOUND"
    assert result["roles"]["PLAN"]["page_url"] == OTHER_URL
    assert result["roles"]["PLAN"]["page_id"] == "bound-b-page"
    assert result["hops"][0]["receipt"] == original_receipt
    assert old.closed is False and new.closed is False
    assert context.new_page_calls == 0
    assert worker.runtime_db.get_command("integration-b")["status"] == "applied"
    _, previous, previous_page, _ = worker._active_recovery_context(result, "PLAN")
    assert previous.endswith(CID)
    assert previous_page == old_id


def test_two_concurrent_url_claims_cannot_both_reserve_same_conversation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    _config, store, first, _worker = pinned_task(tmp_path, task_id="claim-race-a")
    _cfg, _store, second, _worker = pinned_task(tmp_path, task_id="claim-race-b")
    gate = Barrier(2, timeout=12)
    original_mutate = store._mutate_manifest
    def aligned_mutation(*args, **kwargs):
        gate.wait()
        return original_mutate(*args, **kwargs)
    monkeypatch.setattr(store, "_mutate_manifest", aligned_mutation)
    def claim(task):
        try:
            saved = store.change_role_url(
                task["manifest_path"], role="PLAN", url=PLAIN,
                expected_browser_endpoint_id=task["browser_endpoint_id"],
                external_command_id=f"race-{task['task_id']}",
            )
            return ("accepted", saved["roles"]["PLAN"]["page_url"])
        except ValueError as exc:
            return ("rejected", str(exc))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [first, second]))
    assert sorted(result[0] for result in results) == ["accepted", "rejected"]
    assert "already reserved" in next(value for status, value in results if status == "rejected")
    claimed = [
        store.load(task["manifest_path"])["roles"]["PLAN"].get("page_url")
        for task in (first, second)
    ]
    assert claimed.count(PLAIN) == 1


def test_superseded_change_url_command_replay_bypasses_new_owner_reservation(tmp_path):
    _config, store, first, _worker = pinned_task(tmp_path, task_id="idem-first")
    _cfg, _store, second, _worker = pinned_task(tmp_path, task_id="idem-second")
    first = store.change_role_url(
        first["manifest_path"], role="PLAN", url=PLAIN,
        expected_browser_endpoint_id=first["browser_endpoint_id"],
        external_command_id="old-a-cmd",
    )
    first = store.change_role_url(
        first["manifest_path"], role="PLAN", url=OTHER_URL,
        expected_browser_endpoint_id=first["browser_endpoint_id"],
        external_command_id="new-b-cmd",
    )
    claimed = store.change_role_url(
        second["manifest_path"], role="PLAN", url=PLAIN,
        expected_browser_endpoint_id=second["browser_endpoint_id"],
        external_command_id="second-claims-a",
    )
    assert claimed["roles"]["PLAN"]["page_url"] == PLAIN
    replay = store.change_role_url(
        first["manifest_path"], role="PLAN", url=PLAIN,
        expected_browser_endpoint_id=first["browser_endpoint_id"],
        external_command_id="old-a-cmd",
    )
    assert replay["roles"]["PLAN"]["page_url"] == OTHER_URL
    assert replay["roles"]["PLAN"]["role_url_generation"] == 2
    assert len(replay["role_url_revisions"]) == 2
    with pytest.raises(ValueError, match="replay provenance"):
        store.change_role_url(
            first["manifest_path"], role="PLAN", url=OTHER_URL,
            expected_browser_endpoint_id=first["browser_endpoint_id"],
            external_command_id="old-a-cmd",
        )


def test_live_accepted_hop_reserves_both_hop_url_and_receipt_identity(tmp_path):
    _config, store, first, _worker = pinned_task(tmp_path, task_id="divergent-first")
    _cfg, _store, second, _worker = pinned_task(tmp_path, task_id="divergent-second")
    first = store.update(first["manifest_path"], lambda s: (
        s["hops"][0].update(
            state="waiting", conversation_url=OTHER_URL,
            receipt={"conversation_id": CID, "binding": {"page_id": "accepted-a-page"}},
        ),
        s["roles"]["PLAN"].update(page_url=OTHER_URL, page_id="accepted-a-page"),
        s.update(status="WAITING", kanban_column="WAITING"), s
    )[-1])
    original = json.loads(json.dumps(first["hops"][0]["receipt"]))
    with pytest.raises(ValueError, match="already reserved"):
        store.change_role_url(
            second["manifest_path"], role="PLAN", url=PLAIN,
            expected_browser_endpoint_id=second["browser_endpoint_id"],
            external_command_id="cannot-steal-receipt-a",
        )
    assert store.load(first["manifest_path"])["hops"][0]["receipt"] == original
