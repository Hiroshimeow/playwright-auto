from __future__ import annotations

import asyncio

from playwright_auto.cdpa_endpoint_pool import EndpointPool
from test_cdpa_endpoint_pool import FakeBrowser, FakePlaywright, make_pool


def test_empty_browser_context_does_not_leak_playwright(tmp_path):
    async def run():
        pool, _, _, _, _, _ = make_pool(tmp_path)
        attached = []
        async def connect(address):
            pw, browser = FakePlaywright(), FakeBrowser(address)
            browser.contexts = []
            attached.append(pw)
            return pw, browser
        pool.connector = connect
        await pool.refresh(wait=True)
        assert not pool.contexts
        assert all(pw.stops == 1 for pw in attached)
        await pool.close()
    asyncio.run(run())


def test_login_transition_is_reprobed_without_reconnecting(tmp_path):
    async def run():
        pool, _, first, _, connections, now = make_pool(tmp_path, auth_state="login_required")
        await pool.refresh(wait=True)
        context = pool.contexts[first["endpoint_id"]]
        async def authenticated(_context):
            return {"auth_state": "authenticated", "chatgpt_ready": True}
        pool.auth_probe = authenticated
        now[0] += 60
        await pool.refresh()
        assert pool.contexts[first["endpoint_id"]] is context
        assert len(connections) == 2
        assert all(item["chatgpt_ready"] for item in pool.snapshot()["endpoints"])
        await pool.close()
    asyncio.run(run())


def test_browser_identity_matches_alias_hosts_by_devtools_browser_id():
    identity = "01234567-89ab-cdef-0123-456789abcdef"
    a = {"webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/browser/{identity}"}
    b = {"webSocketDebuggerUrl": f"ws://thinkbook:9222/devtools/browser/{identity}"}
    assert EndpointPool._physical_identity(a, "http://127.0.0.1:9222") == EndpointPool._physical_identity(b, "http://thinkbook:9222")


def test_unspecified_account_scopes_do_not_globally_freeze_pool(tmp_path):
    async def run():
        pool, registry, first, second, _, _ = make_pool(tmp_path)
        assert first["rate_limit_scope"] != second["rate_limit_scope"]
        await pool.refresh(wait=True)
        assert pool.select_endpoint([], cooldown_scopes={first["rate_limit_scope"]}) == second["endpoint_id"]
        await pool.close()
    asyncio.run(run())


def test_conflicting_alias_is_reconsidered_after_owner_removed(tmp_path):
    async def run():
        pool, registry, _, _, _, now = make_pool(tmp_path, duplicate=True)
        await pool.refresh(wait=True)
        owner = next(iter(pool.contexts))
        registry.remove(owner, has_nonterminal_tasks=False)
        now[0] += 60
        await pool.refresh()
        assert len(pool.contexts) == 1
        assert owner not in pool.contexts
        await pool.close()
    asyncio.run(run())


def test_failed_auth_probe_does_not_disconnect_healthy_peer(tmp_path):
    async def run():
        pool, _, first, second, _, now = make_pool(tmp_path)
        await pool.refresh(wait=True)
        peer = pool.contexts[second["endpoint_id"]]
        async def auth(context):
            if context.address == first["cdp_url"]:
                raise TimeoutError("authentication probe timeout")
            return {"auth_state": "authenticated", "chatgpt_ready": True}
        pool.auth_probe = auth
        now[0] += 60
        await pool.refresh()
        states = {row["endpoint_id"]: row for row in pool.snapshot()["endpoints"]}
        assert pool.contexts[second["endpoint_id"]] is peer
        assert not states[first["endpoint_id"]]["chatgpt_ready"]
        assert states[second["endpoint_id"]]["chatgpt_ready"]
        await pool.close()
    asyncio.run(run())
