from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_managed_browser as managed
from playwright_auto.cdpa_endpoints import normalize_endpoint_record


@pytest.mark.parametrize("link_method", ["is_symlink", "is_junction"])
def test_endpoint_normalization_rejects_profile_links_before_resolving(tmp_path, monkeypatch, link_method):
    link = tmp_path / "profile-link"
    monkeypatch.setattr(Path, link_method, lambda path: path == link, raising=False)
    with pytest.raises(ValueError, match="symlink|junction"):
        normalize_endpoint_record({"cdp_url": "http://127.0.0.1:9402", "management_mode": "managed-local",
                                   "profile_path": str(link)}, endpoint_id="linked")


@pytest.mark.parametrize("argument", ['"--user-data-dir={path}"', '--user-data-dir="{path}"', '--user-data-dir "{path}"'])
def test_windows_profile_in_use_detects_quoted_paths(tmp_path, monkeypatch, argument):
    if managed.os.name != "nt":
        pytest.skip("Windows command line inspection")
    profile = tmp_path / "profile with spaces"
    profile.mkdir()
    monkeypatch.setattr(managed.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps("chrome.exe " + argument.format(path=profile))))
    assert managed._profile_in_use(profile)


@pytest.mark.parametrize("ledger_status", ["new", "prompt_set"])
@pytest.mark.skip(reason="workflow tasks wait on their pinned endpoint instead of failing over across endpoints")
def test_pristine_pool_failover_preserves_request_and_records_new_endpoint(tmp_path, monkeypatch, ledger_status):
    import playwright_auto.cdpa_worker as module
    from playwright_auto.durable import RequestLedger, RequestStatus
    from test_cdpa_worker import setup_task, FakeActions, RecordingCDPASendActions, RecordingCDPASendClient
    from test_cdpa_worker_endpoints import _pool, _noop
    _, store, state, worker = setup_task(tmp_path)
    hop = module._active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    original = (state["task_id"], hop["hop_id"], hop["request_id"])
    original_prompt = hop["prompt"]
    source = store.endpoints.resolve()["endpoint_id"]
    target = store.endpoints.add("9202")["endpoint_id"]
    constructor = module.task_workflow_definitions(state, worker.config)["PLAN"]["system_prompt"]
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(role=hop["physical_role"], prompt=hop["prompt"], request_id=hop["request_id"],
                          source_context={"task_id": state["task_id"], "team": state["team"], "hop_id": hop["hop_id"], "manifest": state["manifest_path"]},
                          role_prompt_hash=module._sha(str(constructor)), render_request_marker=False)
    if ledger_status == "prompt_set":
        ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    state.update(status="RUNNING")
    state["roles"]["PLAN"].update(browser_endpoint_id=source, online=False)
    store.save(state["manifest_path"], state)
    worker.hydrate_runtime()
    worker.registry.update_task(state, now=0)
    from dataclasses import replace
    class Client(RecordingCDPASendClient):
        async def assert_ownership(self):
            return replace(await super().assert_ownership(), page_role=hop["physical_role"])
    client = Client()
    client.binding = replace(client.binding, role=hop["physical_role"])
    class Actions(RecordingCDPASendActions):
        async def acquire(self, *_args, **_kwargs):
            return module.AcquiredRole(
                client=self.client,
                page_id="page-PLAN",
                url="https://chatgpt.com/",
                created=True,
                new_chat=True,
            )
    monkeypatch.setattr(module, "CDPATabActions", lambda *a, **kw: Actions(client))
    for name in ("_maintain_ambient_page_automation", "_refresh_rate_limit_cooldown", "_close_idle_independent_tabs"):
        monkeypatch.setattr(worker, name, _noop)
    async def run():
        pool = await _pool(store)
        try:
            await pool.mark_unavailable(source, "test partition")
            for _ in range(3):
                await worker.run_once_pool(pool)
                worker.registry.update_task(
                    store.load(state["manifest_path"]), now=0
                )
        finally:
            await pool.close()
    asyncio.run(run())
    saved = store.load(state["manifest_path"])
    active = module._active_hop(saved)
    assert saved["status"] != "BLOCKED", (saved.get("block_code"), saved.get("block_reason"))
    assert (saved["task_id"], active["hop_id"], active["request_id"]) == original
    assert active["prompt"] != original_prompt
    assert "RECOVERY_SNAPSHOT" in active["prompt"]
    assert client.send_calls == [active["prompt"]]
    assert ledger.peek(record.request_id).attempts == 1
    assert saved["roles"]["PLAN"]["browser_endpoint_id"] == target
    assert active["browser_endpoint_id"] == target
    assert not worker._execution_leases


@pytest.mark.parametrize("removed", [False, True])
def test_draining_or_removed_endpoint_cannot_start_a_new_process(tmp_path, monkeypatch, removed):
    from test_cdpa_managed_browser import _setup
    config, registry = _setup(tmp_path)
    endpoint = registry.add("9462", management_mode="managed-local", profile_path=str(tmp_path / "profile"))
    endpoint = registry.remove(endpoint["endpoint_id"], has_nonterminal_tasks=not removed)
    lifecycle = managed.ManagedBrowserLifecycle(config)
    monkeypatch.setattr(lifecycle, "prepare_profile", lambda **kwargs: pytest.fail("must reject before profile preparation"))
    with pytest.raises(ValueError, match="draining|removed"):
        lifecycle.start(endpoint)
    assert not (tmp_path / "profile").exists()


@pytest.mark.parametrize("section", [False, True])
def test_learning_editor_preserves_unrelated_crlf_bytes(tmp_path, section):
    from playwright_auto.cdpa_learning import update_learning
    before = b"# LEARNING.md\r\n\r\n## Durable transport\r\n\r\n- Old policy.\r\n\r\n## Unrelated\r\n\r\n- Keep these exact bytes.\r\n"
    path = tmp_path / "LEARNING.md"
    path.write_bytes(before)
    old = "## Durable transport\n\n- Old policy." if section else "- Old policy."
    update_learning(tmp_path, disposition="REVISED", old_text=old, new_text=old.replace("Old policy", "Updated policy"))
    assert path.read_bytes() == before.replace(b"Old policy", b"Updated policy")


@pytest.mark.parametrize("pid_text", ["0", "-1", "4294967297", "18446744073709551617"])
def test_malformed_pid_receipt_never_reaches_native_process_lookup(tmp_path, pid_text):
    from test_cdpa_managed_browser import _setup
    config, registry = _setup(tmp_path)
    calls = []
    lifecycle = managed.ManagedBrowserLifecycle(config, pid_alive=lambda pid: calls.append(pid) or False)
    endpoint = registry.resolve()["endpoint_id"]
    pidfile = lifecycle.pid_file(endpoint)
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(pid_text)
    state = lifecycle.process_state(endpoint)
    assert state["managed_running"] is None
    assert state["process_ownership"] == "unverified"
    assert not calls
