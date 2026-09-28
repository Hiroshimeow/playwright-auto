from __future__ import annotations

import asyncio

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
from test_cdpa_core import write_config


class FakeContext:
    def __init__(self, address):
        self.address = address
        self.pages = []


class FakeBrowser:
    def __init__(self, address):
        self.contexts = [FakeContext(address)]
        self.connected = True

    def is_connected(self):
        return self.connected

    def on(self, _event, _callback):
        pass

    async def close(self):
        raise AssertionError("An attached user browser must never be closed")


class FakePlaywright:
    def __init__(self):
        self.stops = 0

    async def stop(self):
        self.stops += 1


def make_pool(tmp_path, *, failing=(), auth_state="authenticated", duplicate=False):
    from playwright_auto.cdpa_endpoint_pool import EndpointPool
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    registry = BrowserEndpointRegistry(config)
    original = registry.resolve()
    second = registry.add("9202")
    connections = []
    now = [100.0]

    async def version(address):
        identity = "shared" if duplicate else address.rsplit(":", 1)[-1]
        return {"Browser": "Chromium/test", "webSocketDebuggerUrl": f"ws://localhost/devtools/browser/{identity}"}

    async def connect(address):
        if address in failing:
            raise OSError("simulated endpoint failure")
        pair = FakePlaywright(), FakeBrowser(address)
        connections.append(pair)
        return pair

    async def auth(_context):
        return {"auth_state": auth_state, "chatgpt_ready": auth_state == "authenticated"}

    pool = EndpointPool(registry, connector=connect, version_probe=version, auth_probe=auth, clock=lambda: now[0])
    return pool, registry, original, second, connections, now


def test_two_endpoints_connect_independently_and_detach_without_browser_close(tmp_path):
    async def run():
        pool, registry, first, second, connections, _ = make_pool(tmp_path)
        await pool.refresh(wait=True)
        assert set(pool.contexts) == {first["endpoint_id"], second["endpoint_id"]}
        assert all(item["auth_state"] == "authenticated" for item in pool.snapshot()["endpoints"])
        registry.update(second["endpoint_id"], is_default=True)
        await pool.refresh()
        assert pool.snapshot()["default_endpoint_id"] == second["endpoint_id"]
        await pool.close()
        assert all(playwright.stops == 1 for playwright, _ in connections)
    asyncio.run(run())


def test_offline_endpoint_does_not_disconnect_healthy_pool_member(tmp_path):
    async def run():
        pool, _registry, first, second, connections, _ = make_pool(tmp_path, failing={"http://127.0.0.1:9202"})
        await pool.refresh(wait=True)
        assert set(pool.contexts) == {first["endpoint_id"]}
        states = {item["endpoint_id"]: item for item in pool.snapshot()["endpoints"]}
        assert states[first["endpoint_id"]]["health"] == "online"
        assert states[second["endpoint_id"]]["health"] == "offline"
        assert connections[0][0].stops == 0
        await pool.close()
    asyncio.run(run())


def test_disconnect_reconnect_replaces_only_affected_connection(tmp_path):
    async def run():
        pool, _registry, first, second, connections, now = make_pool(tmp_path)
        await pool.refresh(wait=True)
        stable_context = pool.contexts[second["endpoint_id"]]
        failed = next(pair for pair in connections if pair[1].contexts[0].address == first["cdp_url"])
        failed[1].connected = False
        await pool.refresh()
        assert pool.contexts[second["endpoint_id"]] is stable_context
        assert failed[0].stops == 1
        now[0] += 20
        await pool.refresh(wait=True)
        assert set(pool.contexts) == {first["endpoint_id"], second["endpoint_id"]}
        assert pool.contexts[second["endpoint_id"]] is stable_context
        await pool.close()
    asyncio.run(run())


def test_address_update_keeps_endpoint_identity_and_does_not_touch_peer(tmp_path):
    async def run():
        pool, registry, first, second, connections, _ = make_pool(tmp_path)
        await pool.refresh(wait=True)
        peer = pool.contexts[second["endpoint_id"]]
        registry.update(first["endpoint_id"], cdp_url="9302")
        await pool.refresh(wait=True)
        assert pool.contexts[first["endpoint_id"]].address == "http://127.0.0.1:9302"
        assert pool.contexts[second["endpoint_id"]] is peer
        assert sum(pw.stops for pw, _ in connections) == 1
        await pool.close()
    asyncio.run(run())


def test_login_state_is_separate_from_connection_health(tmp_path):
    async def run():
        pool, _registry, first, _second, _connections, _ = make_pool(tmp_path, auth_state="login_required")
        await pool.refresh(wait=True)
        assert first["endpoint_id"] in pool.contexts
        record = next(item for item in pool.snapshot()["endpoints"] if item["endpoint_id"] == first["endpoint_id"])
        assert record["health"] == "online"
        assert record["chatgpt_ready"] is False
        assert record["auth_state"] == "login_required"
        await pool.close()
    asyncio.run(run())


def test_two_registry_aliases_cannot_own_the_same_physical_browser(tmp_path):
    async def run():
        pool, _registry, _first, _second, connections, _ = make_pool(tmp_path, duplicate=True)
        await pool.refresh(wait=True)
        assert len(pool.contexts) == 1
        assert any(item["health"] == "conflict" for item in pool.snapshot()["endpoints"])
        assert sum(pw.stops for pw, _ in connections) == 1
        await pool.close()
    asyncio.run(run())


def test_draining_keeps_connection_until_removal_without_stopping_browser(tmp_path):
    async def run():
        pool, registry, first, _second, connections, _ = make_pool(tmp_path)
        await pool.refresh(wait=True)
        registry.remove(first["endpoint_id"], has_nonterminal_tasks=True)
        await pool.refresh()
        assert first["endpoint_id"] in pool.contexts
        registry.remove(first["endpoint_id"], has_nonterminal_tasks=False)
        await pool.refresh()
        assert first["endpoint_id"] not in pool.contexts
        assert sum(pw.stops for pw, _ in connections) == 1
        await pool.close()
    asyncio.run(run())



def _execution(endpoint_id, *, task="task", team="alpha", role="DEV", request="request"):
    return {
        "endpoint_id": endpoint_id,
        "task_id": task,
        "team": team,
        "role": role,
        "request_id": request,
    }


def test_allocator_uses_free_peer_when_preferred_endpoint_is_at_capacity(tmp_path):
    async def run():
        pool, _registry, first, second, _connections, _ = make_pool(tmp_path)
        await pool.refresh(wait=True)
        running = [
            _execution(first["endpoint_id"], task="a", request="a"),
            _execution(first["endpoint_id"], task="b", request="b"),
        ]
        assert pool.select_endpoint(running, affinity_endpoint_id=first["endpoint_id"]) == second["endpoint_id"]
        snapshot = pool.snapshot(running_executions=running)
        states = {item["endpoint_id"]: item for item in snapshot["endpoints"]}
        assert states[first["endpoint_id"]]["active_count"] == 2
        assert states[first["endpoint_id"]]["available_slots"] == 0
        assert states[second["endpoint_id"]]["available_slots"] == 2
        await pool.close()
    asyncio.run(run())


def test_allocator_prefers_safe_same_role_affinity_before_least_loaded(tmp_path):
    async def run():
        pool, _registry, first, second, _connections, _ = make_pool(tmp_path)
        await pool.refresh(wait=True)
        running = [_execution(second["endpoint_id"], task="other", request="other")]
        assert pool.select_endpoint(running, affinity_endpoint_id=second["endpoint_id"]) == second["endpoint_id"]
        assert pool.select_endpoint(running) == first["endpoint_id"]
        await pool.close()
    asyncio.run(run())


def test_allocator_excludes_draining_auth_unusable_and_scope_cooldown(tmp_path):
    async def run():
        pool, registry, first, second, _connections, _ = make_pool(tmp_path)
        registry.update(first["endpoint_id"], rate_limit_scope="account-a")
        registry.update(second["endpoint_id"], rate_limit_scope="account-b")
        await pool.refresh(wait=True)
        assert pool.select_endpoint([], cooldown_scopes={"account-a"}) == second["endpoint_id"]
        registry.remove(first["endpoint_id"], has_nonterminal_tasks=True)
        await pool.refresh()
        assert pool.select_endpoint([]) == second["endpoint_id"]
        pool._runtimes[second["endpoint_id"]].chatgpt_ready = False
        assert pool.select_endpoint([]) is None
        await pool.close()
    asyncio.run(run())


def test_shared_rate_limit_scope_can_exhaust_only_that_scope(tmp_path):
    async def run():
        pool, registry, first, second, _connections, _ = make_pool(tmp_path)
        registry.update(first["endpoint_id"], rate_limit_scope="shared-x")
        registry.update(second["endpoint_id"], rate_limit_scope="shared-x")
        await pool.refresh(wait=True)
        assert pool.select_endpoint([], cooldown_scopes={"shared-x"}) is None
        await pool.close()
    asyncio.run(run())


def test_capacity_release_is_derived_from_running_execution_set(tmp_path):
    async def run():
        pool, _registry, first, second, _connections, _ = make_pool(tmp_path)
        await pool.refresh(wait=True)
        full = [
            _execution(first["endpoint_id"], task="a", request="a"),
            _execution(first["endpoint_id"], task="b", request="b"),
            _execution(second["endpoint_id"], task="c", request="c"),
            _execution(second["endpoint_id"], task="d", request="d"),
        ]
        assert pool.select_endpoint(full) is None
        released = full[:-1]
        assert pool.select_endpoint(released) == second["endpoint_id"]
        await pool.close()
    asyncio.run(run())



def test_version_probe_uses_json_version_for_http_endpoint(monkeypatch):
    import playwright_auto.cdpa_endpoint_pool as module
    seen = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _limit):
            return b'{"Browser":"Chromium/test","webSocketDebuggerUrl":"ws://browser/id"}'

    def urlopen(request, timeout):
        seen.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(module, "urlopen", urlopen)
    result = asyncio.run(module.probe_cdp_version("http://127.0.0.1:9222/some-path"))
    assert seen[0][0] == "http://127.0.0.1:9222/json/version"
    assert result["webSocketDebuggerUrl"] == "ws://browser/id"


def test_auth_probe_uses_existing_chatgpt_page_without_creating_one(monkeypatch):
    import playwright_auto.cdpa_endpoint_pool as module

    class Client:
        def __init__(self, page, timeout_ms):
            self.page = page
        async def snapshot(self):
            return type("Snapshot", (), {
                "requires_login": False,
                "composer_present": True,
                "composer_editable": True,
            })()

    monkeypatch.setattr(module, "ChatGPTPage", Client)
    context = type("Context", (), {"pages": [type("Page", (), {"url": "https://chatgpt.com/"})()]})()
    result = asyncio.run(module.probe_chatgpt_auth(context))
    assert result == {"auth_state": "authenticated", "chatgpt_ready": True}


def test_same_address_physical_identity_change_reattaches_only_changed_endpoint(tmp_path):
    async def run():
        pool, _registry, first, second, connections, now = make_pool(tmp_path)
        await pool.refresh(wait=True)
        first_context = pool.contexts[first["endpoint_id"]]
        peer_context = pool.contexts[second["endpoint_id"]]
        first_playwright = next(
            playwright for playwright, browser in connections
            if browser.contexts[0].address == first["cdp_url"]
        )

        async def changed_version(address):
            identity = "changed-browser" if address == first["cdp_url"] else address.rsplit(":", 1)[-1]
            return {"webSocketDebuggerUrl": f"ws://localhost/devtools/browser/{identity}"}

        pool.version_probe = changed_version
        now[0] += 20
        await pool.refresh()

        assert pool.contexts[first["endpoint_id"]] is not first_context
        assert pool.contexts[second["endpoint_id"]] is peer_context
        assert first_playwright.stops == 1
        await pool.close()

    asyncio.run(run())


def test_periodic_identity_probe_same_identity_does_not_reconnect(tmp_path):
    async def run():
        pool, _registry, first, second, connections, now = make_pool(tmp_path)
        await pool.refresh(wait=True)
        first_context = pool.contexts[first["endpoint_id"]]
        peer_context = pool.contexts[second["endpoint_id"]]
        count = len(connections)

        now[0] += 20
        await pool.refresh()

        assert len(connections) == count
        assert pool.contexts[first["endpoint_id"]] is first_context
        assert pool.contexts[second["endpoint_id"]] is peer_context
        await pool.close()

    asyncio.run(run())


def test_identity_change_collision_fails_closed_without_touching_peer(tmp_path):
    async def run():
        pool, _registry, first, second, _connections, now = make_pool(tmp_path)
        await pool.refresh(wait=True)
        peer_context = pool.contexts[second["endpoint_id"]]

        async def colliding_version(address):
            identity = second["cdp_url"].rsplit(":", 1)[-1] if address == first["cdp_url"] else address.rsplit(":", 1)[-1]
            return {"webSocketDebuggerUrl": f"ws://localhost/devtools/browser/{identity}"}

        pool.version_probe = colliding_version
        now[0] += 20
        snapshot = await pool.refresh()
        states = {item["endpoint_id"]: item for item in snapshot["endpoints"]}

        assert first["endpoint_id"] not in pool.contexts
        assert states[first["endpoint_id"]]["health"] == "conflict"
        assert pool.contexts[second["endpoint_id"]] is peer_context
        await pool.close()

    asyncio.run(run())
