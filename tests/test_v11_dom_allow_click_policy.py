"""One-shot DOM permission click must not be directed by stale LISTEN metadata."""
import asyncio

from playwright_auto.chatgpt import ChatGPTPage
from test_chatgpt_mcp_permission import _with_page


def test_stale_listen_action_cannot_trigger_hidden_react_permission():
    async def run(page):
        await page.evaluate("""() => {
            const root=document.querySelector('[data-message-id]');
            root.__reactProps$fixture={onSelectOption:(_,action)=>window.__approved=action};
        }""")
        client=ChatGPTPage(page,timeout_ms=5000)
        result=await client.auto_allow_mcp_permission(passive_action={
            "type":"allow","target_message_id":"stale-from-listen","remember_answer":True,
        })
        return result,await page.evaluate("window.__approved || null")

    result, approved=asyncio.run(_with_page(
        '<main><div data-message-id="old-turn">stale tool result</div>'
        '<div contenteditable="true" role="textbox"></div></main>',run))
    assert result is None
    assert approved is None


def test_visible_allow_once_outweighs_stale_network_react_metadata():
    async def run(page):
        await page.evaluate("""() => {
            document.querySelector('[data-message-id]').__reactProps$fixture={
                onSelectOption:(_,action)=>window.__approved=action
            };
        }""")
        client=ChatGPTPage(page,timeout_ms=5000)
        result=await client.auto_allow_mcp_permission(passive_action={
            "type":"allow","target_message_id":"old-tool","remember_answer":True,
        })
        return result,await page.evaluate("({once:window.__once||0,approved:window.__approved||null})")

    result, state=asyncio.run(_with_page(
        '<main><div data-message-id="old-turn">old</div><div data-codex-approval-surface="true">'
        '<button onclick="window.__once=(window.__once||0)+1">Allow once</button>'
        '<button aria-label="Approval options"></button></div></main>',run))
    assert result and result["remember_answer"]=="false"
    assert state=={"once":1,"approved":None}


def test_plain_unrelated_allow_button_never_approves_anything():
    async def run(page):
        client=ChatGPTPage(page,timeout_ms=5000)
        result=await client.auto_allow_mcp_permission()
        return result,await page.evaluate("window.__clicks || 0")

    result, clicks=asyncio.run(_with_page(
        '<main><button onclick="window.__clicks=(window.__clicks||0)+1">Allow</button></main>',run))
    assert result is None
    assert clicks==0


def test_permission_surface_must_match_fresh_dom_before_click():
    async def run(page):
        from playwright_auto.chatgpt import inspect_chatgpt_wait_probe
        client = ChatGPTPage(page, timeout_ms=5000)
        initial = await inspect_chatgpt_wait_probe(page)
        assert initial.mcp_permission_surface_key
        await page.locator('[data-codex-approval-surface="true"] .tool-title').evaluate(
            "node => node.textContent = 'new-tool-call'"
        )
        rejected = await client.auto_allow_mcp_permission(
            expected_surface_key=initial.mcp_permission_surface_key,
        )
        changed = await inspect_chatgpt_wait_probe(page)
        assert changed.mcp_permission_surface_key != initial.mcp_permission_surface_key
        accepted = await client.auto_allow_mcp_permission(
            expected_surface_key=changed.mcp_permission_surface_key,
        )
        return rejected, accepted, await page.evaluate("window.__clicks || 0")

    rejected, accepted, clicks = asyncio.run(_with_page(
        '<main><div data-codex-approval-surface="true">'
        '<div class="tool-title">original-tool-call</div>'
        '<button onclick="window.__clicks=(window.__clicks||0)+1">Allow once</button>'
        '</div></main>', run,
    ))
    assert rejected is None
    assert accepted and accepted["remember_answer"] == "false"
    assert clicks == 1


def test_nested_group_inside_split_surface_is_one_permission_and_only_allow_once_clicked():
    async def run(page):
        from playwright_auto.chatgpt import inspect_chatgpt_wait_probe
        probe = await inspect_chatgpt_wait_probe(page)
        client = ChatGPTPage(page, timeout_ms=5000)
        result = await client.auto_allow_mcp_permission(
            expected_surface_key=probe.mcp_permission_surface_key,
        )
        counts = await page.evaluate("({once:window.__once||0,wide:window.__wide||0})")
        return probe, result, counts

    probe, result, counts = asyncio.run(_with_page(
        '<main><div data-codex-approval-surface="true">'
        '<div>tool-call</div><button onclick="window.__once=(window.__once||0)+1">Allow once</button>'
        '<div><button onclick="window.__wide=(window.__wide||0)+1">Allow</button>'
        '<button aria-label="Allow md for this conversation"></button></div>'
        '</div></main>', run,
    ))
    assert probe.mcp_permission_allow_count == 1
    assert probe.mcp_permission_node_count == 1
    assert result and result["remember_answer"] == "false"
    assert counts == {"once": 1, "wide": 0}


def test_unactionable_split_permission_stops_before_durable_intent():
    from playwright_auto.chatgpt import inspect_chatgpt_page
    from playwright_auto.role_runtime import Action, RoleController
    from datetime import datetime, timezone

    async def run(page):
        current = await inspect_chatgpt_page(page)
        decision = RoleController().decide(current, {}, now=datetime.now(timezone.utc))
        result = await ChatGPTPage(page, timeout_ms=5000).auto_allow_mcp_permission(
            expected_surface_key=current.mcp_permission_surface_key,
        )
        return decision, result, await page.evaluate("window.__clicks || 0")

    decision, result, clicks = asyncio.run(_with_page(
        '<main><div data-codex-approval-surface="true">'
        '<div>tool-call</div><button onclick="window.__clicks=(window.__clicks||0)+1">Allow once</button>'
        '<button onclick="window.__clicks=(window.__clicks||0)+1">Allow once</button>'
        '</div></main>', run,
    ))
    assert (decision.action, decision.reason) == (Action.BLOCK, "permission_surface_unactionable")
    assert result is None
    assert clicks == 0


def test_surface_changed_before_allow_dispatch_never_clicks_or_confirms_intent():
    from playwright_auto.chatgpt import PageBinding
    from playwright_auto.role_runtime import Action, RoleController
    from test_role_runtime_browser import environment

    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5000)
        client.binding = PageBinding("page-1", "DEV")
        real_allow = client.auto_allow_mcp_permission

        async def changed_after_snapshot(**kwargs):
            await page.locator(".tool-title").evaluate("node => node.textContent = 'replacement-call'")
            return await real_allow(**kwargs)

        client.auto_allow_mcp_permission = changed_after_snapshot
        state, hop, worker, acquired = environment(client)
        result = await RoleController().run(worker, state, hop, acquired, None, wait_ms=0)
        with worker.runtime_db.connection() as conn:
            statuses = [row["status"] for row in conn.execute(
                "SELECT status FROM command_queue WHERE kind='mcp_allow'"
            ).fetchall()]
        return result, statuses, await page.evaluate("window.__clicks || 0")

    decision, statuses, clicks = asyncio.run(_with_page(
        '<main><div data-message-author-role="user" data-message-id="user-original">original task</div>'
        '<div data-codex-approval-surface="true"><span class="tool-title">original-call</span>'
        '<button onclick="window.__clicks=(window.__clicks||0)+1">Allow once</button>'
        '</div><div contenteditable="true" role="textbox"></div></main>', run,
    ))
    assert (decision.action, decision.reason) == (Action.WAIT, "mcp_allow_handler_pending")
    assert statuses == ["recovery_required"]
    assert clicks == 0
