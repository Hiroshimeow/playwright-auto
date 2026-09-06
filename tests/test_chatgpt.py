import asyncio
from datetime import datetime, timedelta, timezone
import json
import time

import pytest

import playwright_auto.chatgpt as chatgpt_module
from playwright_auto.cdpa_response import (
    begin_refresh,
    observe_response_activity,
    refresh_due,
    start_wait_budget,
)
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    classify_chatgpt_state,
    extract_session_id,
    recent_assistant_messages,
    recent_assistant_turns,
    response_activity_signature,
    validate_page_role,
)


def _activity_snapshot(
    *,
    activity_text: str,
    activity_length: int,
    activity_turn_id: str = "turn-active",
    activity_structure: str = "bounded",
    messages=(),
    state: ChatGPTState = ChatGPTState.RESPONDING,
    stop_visible: bool = True,
    error_texts=(),
    blocking_dialogs=(),
) -> ChatGPTSnapshot:
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/activity",
        session_id="activity",
        page_id="page-activity",
        page_role="PLAN",
        page_task_id="task-activity",
        page_team="activity",
        state=state,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="",
        send_visible=False,
        send_enabled=False,
        stop_visible=stop_visible,
        blocking_dialogs=tuple(blocking_dialogs),
        attachment_markers=(),
        error_texts=tuple(error_texts),
        messages=tuple(messages),
        response_activity_text=activity_text,
        response_activity_structure=activity_structure,
        response_activity_turn_id=activity_turn_id,
        response_activity_length=activity_length,
    )


def test_response_activity_signature_is_canonical_across_probe_representations():
    assert ChatGPTPage._adaptive_wait_seconds(5000, 0) == 5.0
    assert ChatGPTPage._adaptive_wait_seconds(5000, 4) == 7.5
    assert ChatGPTPage._adaptive_wait_seconds(5000, 5) == 10.0

    full_text = "prefix-" + ("x" * 600)
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    lightweight = _activity_snapshot(
        activity_text=full_text[-160:],
        activity_length=len(full_text),
        activity_structure=f"bounded:{len(full_text)}",
        state=ChatGPTState.RESPONDING,
        stop_visible=True,
        error_texts=("temporary transport notice",),
    )
    full = _activity_snapshot(
        activity_text=full_text,
        activity_length=len(full_text),
        activity_structure="tool-call:finished>tool-result:finished",
        state=ChatGPTState.ERROR,
        stop_visible=False,
        blocking_dialogs=("representation-only dialog",),
    )

    lightweight_signature, lightweight_length = response_activity_signature(
        lightweight, baseline
    )
    full_signature, full_length = response_activity_signature(full, baseline)

    assert lightweight_signature == full_signature
    assert lightweight_length == full_length == len(full_text)


def test_alternating_probe_representations_do_not_extend_no_progress_budget():
    start = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)
    full_text = "y" * 607
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    signatures = [
        response_activity_signature(
            _activity_snapshot(
                activity_text=full_text[-160:],
                activity_length=len(full_text),
                activity_structure=f"bounded:{len(full_text)}",
            ),
            baseline,
        ),
        response_activity_signature(
            _activity_snapshot(
                activity_text=full_text,
                activity_length=len(full_text),
                activity_structure="full DOM tool structure",
                state=ChatGPTState.ERROR,
                stop_visible=False,
            ),
            baseline,
        ),
    ]
    wait = {}
    start_wait_budget(wait, timeout_seconds=7200, now=start)

    assert observe_response_activity(
        wait,
        signature=signatures[0][0],
        length=signatures[0][1],
        now=start + timedelta(minutes=1),
    )
    changed_at = wait["activity_changed_at"]
    assert not observe_response_activity(
        wait,
        signature=signatures[1][0],
        length=signatures[1][1],
        now=start + timedelta(minutes=10),
    )
    assert wait["activity_changed_at"] == changed_at
    assert refresh_due(
        wait,
        refresh_after_seconds=1200,
        now=start + timedelta(minutes=21),
    )
    begin_refresh(wait, now=start + timedelta(minutes=21))
    assert not refresh_due(
        wait,
        refresh_after_seconds=1200,
        now=start + timedelta(minutes=21, seconds=1),
    )


@pytest.mark.parametrize(
    "progressed",
    [
        _activity_snapshot(
            activity_text="stable output plus one token",
            activity_length=28,
        ),
        _activity_snapshot(
            activity_text="stable output",
            activity_length=13,
            activity_turn_id="turn-next",
        ),
        _activity_snapshot(
            activity_text="stable output",
            activity_length=13,
            messages=(
                MessageSnapshot(
                    "assistant", "assistant-1", "assistant-turn-1", "new assistant text", ()
                ),
            ),
        ),
    ],
)
def test_response_activity_signature_changes_only_for_semantic_progress(progressed):
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    stable = _activity_snapshot(activity_text="stable output", activity_length=13)

    stable_signature, _ = response_activity_signature(stable, baseline)
    progressed_signature, _ = response_activity_signature(progressed, baseline)

    assert progressed_signature != stable_signature


def test_extract_session_id_from_supported_routes():
    assert extract_session_id("https://chatgpt.com/c/abc-123") == "abc-123"
    assert extract_session_id("https://chatgpt.com/g/g-demo/c/session-9") == "session-9"
    assert extract_session_id("https://chatgpt.com/") is None
    assert extract_session_id("https://auth.openai.com/c/not-a-chat") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"error_present": True}, ChatGPTState.ERROR),
        ({"requires_login": True}, ChatGPTState.AUTH_REQUIRED),
        ({"requires_login": True, "stop_visible": True}, ChatGPTState.RESPONDING),
        ({"requires_login": True, "composer_text": "draft"}, ChatGPTState.DRAFT),
        ({"messages": [{"role": "user"}]}, ChatGPTState.SUBMITTING),
        ({"messages": [{"role": "assistant"}]}, ChatGPTState.WAITING_PROMPT),
        ({"composer_present": True}, ChatGPTState.NEW_CHAT),
        ({}, ChatGPTState.UNKNOWN),
    ],
)
def test_classify_chatgpt_state(raw, expected):
    assert classify_chatgpt_state(raw) is expected


def test_recent_assistant_messages_returns_last_n():
    messages = [
        MessageSnapshot("user", "u1", "t1", "one", ()),
        MessageSnapshot("assistant", "a1", "t1", "first", ("copy-turn-action-button",)),
        MessageSnapshot("user", "u2", "t2", "two", ()),
        MessageSnapshot("assistant", "a2", "t2", "second", ("copy-turn-action-button",)),
    ]
    assert [item.message_id for item in recent_assistant_messages(messages, 1)] == ["a2"]
    assert [item.message_id for item in recent_assistant_messages(messages, 2)] == ["a1", "a2"]
    with pytest.raises(ValueError):
        recent_assistant_messages(messages, 0)


def test_recent_assistant_turns_deduplicates_message_nodes():
    messages = [
        MessageSnapshot("assistant", "a1-analysis", "t1", "analysis", ()),
        MessageSnapshot("assistant", "a1-final", "t1", "final", ()),
        MessageSnapshot("user", "u2", "t2", "next", ()),
        MessageSnapshot("assistant", "a2", "t2", "second", ()),
    ]
    assert [item.message_id for item in recent_assistant_turns(messages, 2)] == [
        "a1-final",
        "a2",
    ]


def test_validate_page_role():
    assert validate_page_role("PLAN") == "PLAN"
    assert validate_page_role("review-2") == "review-2"
    with pytest.raises(ValueError):
        validate_page_role("2bad role")


def test_frontend_identity_matcher_and_reducer_are_exact_and_conflict_safe():
    import playwright_auto.chatgpt as cg

    class Request:
        method = "POST"
        post_data_json = {"messages": [{"id": "u1", "author": {"role": "user"}}]}

    class Response:
        request = Request()
        url = "https://chatgpt.com/backend-api/f/conversation?x=1"

    assert cg._matches_frontend_conversation_response(Response()) is True
    assert cg._frontend_user_message_id(Response().request) == "u1"
    body = b'event: message\ndata: not-json\n\ndata: {"conversation_id":"c1"}\n\ndata: {"conversation_id":"c1"}\n'
    assert cg._reduce_frontend_conversation_body(body, "u1") == {
        "observed_user_message_id": "u1",
        "conversation_id": "c1",
    }
    conflict = b'data: {"conversation_id":"c1"}\n\ndata: {"conversation_id":"c2"}\n'
    assert cg._reduce_frontend_conversation_body(conflict, "u1") is None
    Response.request.method = "GET"
    assert cg._matches_frontend_conversation_response(Response()) is False


def test_frontend_natural_sse_reducer_keeps_only_exact_request_branch():
    import playwright_auto.chatgpt as cg

    body = "\n".join(
        [
            'data: {"conversation_id":"c1","message":{"id":"call1","author":{"role":"assistant"},"recipient":"mcp-g8.write_file","content":{"content_type":"text","parts":["{\\"path\\":\\"x\\",\\"content\\":\\"y\\"}"]}}}',
            'data: {"conversation_id":"c1","message":{"id":"tool1","author":{"role":"tool"},"recipient":"assistant","content":{"content_type":"text","parts":["ok"]}}}',
            'data: {"conversation_id":"c1","message":{"id":"final","author":{"role":"assistant"},"recipient":"all","content":{"content_type":"text","parts":["{\\"route\\":\\"TEST\\",\\"handoff\\":\\".plan/x.md\\"}"]}}}',
            "data: [DONE]",
        ]
    )

    observed = cg._reduce_frontend_conversation_observation_body(body, "u1")

    assert observed is not None
    assert observed["conversation_id"] == "c1"
    assert observed["observed_user_message_id"] == "u1"
    assert observed["coverage"] == "complete"
    graph = observed["graph"]
    assert graph["current_node"] == "final"
    assert list(graph["mapping"]) == ["u1", "call1", "tool1", "final"]
    assert graph["mapping"]["call1"]["message"]["recipient"] == "mcp-g8.write_file"


def test_natural_paged_messages_reducer_accepts_observed_messages_schema_without_mapping():
    import playwright_auto.chatgpt as cg

    final_id = "08289956-8023-4063-823f-6436e0836d09"
    body = json.dumps(
        {
            "messages": [
                {
                    "id": "historical",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["old"]},
                },
                {
                    "id": "u1",
                    "author": {"role": "user"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["current request"]},
                },
                {
                    "id": final_id,
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {
                        "content_type": "text",
                        "parts": [
                            '{"route":"TEST","handoff":".plan/cdpa-listen-controls-acceptance/cdpa-listen-controls-acceptance-dev_turn1_cdpa-idem-05059b42ae633eb49d7636cd.md"}'
                        ],
                    },
                },
            ],
            "current_node": final_id,
            "page_info": {"has_more": False},
            "context_truncation_continuation": None,
        }
    )

    observed = cg._reduce_paged_conversation_body(
        body,
        conversation_id="6a9d3b50-f82c-83e8-983e-2bef62ebde41",
        observed_user_message_id="u1",
    )

    assert observed is not None
    assert observed["source"] == "paged_messages"
    assert observed["graph"]["current_node"] == final_id
    assert list(observed["graph"]["mapping"]) == ["u1", final_id]
    assert "historical" not in observed["graph"]["mapping"]


def test_frontend_identity_observer_can_be_taken_before_late_response():
    import asyncio
    import playwright_auto.chatgpt as cg

    class Page:
        def __init__(self):
            self.listeners = {}

        def on(self, event, callback):
            self.listeners[event] = callback

        def remove_listener(self, event, callback):
            if self.listeners.get(event) is callback:
                self.listeners.pop(event, None)

        def off(self, event, callback):
            self.remove_listener(event, callback)

    class Request:
        method = "POST"
        post_data_json = {
            "messages": [{"id": "u-late", "author": {"role": "user"}}]
        }

    class Response:
        request = Request()
        url = "https://chatgpt.com/backend-api/f/conversation"

        async def body(self):
            return b'data: {"conversation_id":"c-late"}\n'

    async def scenario():
        page = Page()
        client = cg.ChatGPTPage(page)
        client._arm_frontend_identity_observer()
        pending = client.take_frontend_identity_task()
        assert pending is not None
        assert pending.done() is False
        listener = page.listeners["response"]
        listener(Response())
        assert await pending == {
            "observed_user_message_id": "u-late",
            "conversation_id": "c-late",
        }
        assert "response" not in page.listeners

    asyncio.run(scenario())


def test_passive_observer_attaches_once_reduces_natural_payloads_and_cleans_up():
    class Page:
        def __init__(self):
            self.listeners: dict[str, list] = {}

        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)

        def remove_listener(self, event, callback):
            callbacks = self.listeners.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)
            if not callbacks:
                self.listeners.pop(event, None)

        def off(self, event, callback):
            self.remove_listener(event, callback)

    class PostRequest:
        method = "POST"
        post_data_json = {"messages": [{"id": "u1", "author": {"role": "user"}}]}

    class PostResponse:
        request = PostRequest()
        url = "https://chatgpt.com/backend-api/f/conversation"

        async def body(self):
            return b'data: {"conversation_id":"c1","message":{"id":"a1","author":{"role":"assistant"},"recipient":"all","content":{"content_type":"text","parts":["working"]}}}\ndata: [DONE]\n'

    class PagedRequest:
        method = "GET"

    class PagedResponse:
        request = PagedRequest()
        url = "https://chatgpt.com/backend-api/conversations/c1?include_has_versions=true&num_turns=10"

        async def body(self):
            return json.dumps(
                {
                    "messages": [
                        {
                            "id": "u1",
                            "author": {"role": "user"},
                            "recipient": "all",
                            "content": {"content_type": "text", "parts": ["task"]},
                        },
                        {
                            "id": "a2",
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "content": {"content_type": "text", "parts": ["done"]},
                        },
                    ],
                    "current_node": "a2",
                    "page_info": {"has_more": False},
                    "context_truncation_continuation": None,
                }
            ).encode()

    async def scenario():
        page = Page()
        first = chatgpt_module.ChatGPTPage(page)
        second = chatgpt_module.ChatGPTPage(page)
        first.arm_passive_observer(request_id="request-1", generation=3)
        second.arm_passive_observer(request_id="request-1", generation=3)
        assert len(page.listeners["response"]) == 1
        assert len(page.listeners["close"]) == 1

        page.listeners["response"][0](PostResponse())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        observed = second.passive_observation(request_id="request-1", generation=3)
        assert observed["conversation_id"] == "c1"
        assert observed["observed_user_message_id"] == "u1"
        assert observed["event_count"] == 1

        second.arm_passive_observer(
            request_id="request-1",
            generation=3,
            conversation_id="c1",
            accepted_user_message_id="u1",
        )
        page.listeners["response"][0](PagedResponse())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        paged = first.passive_observation(request_id="request-1", generation=3)
        assert paged["source"] == "paged_messages"
        assert paged["coverage"] == "complete"
        assert paged["graph"]["current_node"] == "a2"
        assert paged["event_count"] == 2

        second.arm_passive_observer(request_id="request-2", generation=4)
        assert second.passive_observation(request_id="request-2", generation=4)["coverage"] == "unknown"
        page.listeners["close"][0]()
        assert "response" not in page.listeners
        assert "close" not in page.listeners

    asyncio.run(scenario())


def test_passive_observer_ignores_unrelated_paged_conversation_and_old_generation():
    class Page:
        def __init__(self):
            self.listeners: dict[str, list] = {}

        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)

        def remove_listener(self, event, callback):
            callbacks = self.listeners.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)
            if not callbacks:
                self.listeners.pop(event, None)

        off = remove_listener

    class Request:
        method = "GET"

    class Response:
        request = Request()
        url = "https://chatgpt.com/backend-api/conversations/other?num_turns=10"

        async def body(self):
            raise AssertionError("unrelated response body must not be read")

    async def scenario():
        page = Page()
        client = chatgpt_module.ChatGPTPage(page)
        client.arm_passive_observer(
            request_id="request-1",
            generation=1,
            conversation_id="c1",
            accepted_user_message_id="u1",
        )
        page.listeners["response"][0](Response())
        await asyncio.sleep(0)
        assert client.passive_observation(request_id="request-1", generation=1)["coverage"] == "unknown"
        client.arm_passive_observer(request_id="request-2", generation=2)
        assert client.passive_observation(request_id="request-1", generation=1)["coverage"] == "unknown"
        client.detach_passive_observer()

    asyncio.run(scenario())


def test_passive_observer_scope_refinement_clears_mismatched_evidence_and_blocks_late_task():
    class Page:
        def __init__(self):
            self.listeners: dict[str, list] = {}
        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)
        def remove_listener(self, event, callback):
            callbacks = self.listeners.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)
        off = remove_listener

    class Request:
        method = "POST"
        post_data_json = {"messages": [{"id": "u-wrong", "author": {"role": "user"}}]}

    class Response:
        request = Request()
        url = "https://chatgpt.com/backend-api/f/conversation"
        def __init__(self, release=None):
            self.release = release
        async def body(self):
            if self.release is not None:
                await self.release.wait()
            return b'data: {"conversation_id":"c-wrong","message":{"id":"a1","author":{"role":"assistant"},"recipient":"all","content":{"content_type":"text","parts":["done"]}}}\ndata: [DONE]\n'

    async def scenario():
        page = Page()
        client = chatgpt_module.ChatGPTPage(page)
        client.arm_passive_observer(request_id="r1", generation=1)
        page.listeners["response"][0](Response())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        before = client.passive_observation(request_id="r1", generation=1)
        assert before["conversation_id"] == "c-wrong"
        assert before["observed_user_message_id"] == "u-wrong"

        client.arm_passive_observer(
            request_id="r1",
            generation=1,
            conversation_id="c-good",
            accepted_user_message_id="u-good",
        )
        assert client.passive_observation(request_id="r1", generation=1)["coverage"] == "unknown"
        assert len(page.listeners["response"]) == 1

        release = asyncio.Event()
        client.arm_passive_observer(request_id="r2", generation=2)
        page.listeners["response"][0](Response(release))
        await asyncio.sleep(0)
        client.arm_passive_observer(
            request_id="r2",
            generation=2,
            conversation_id="c-good",
            accepted_user_message_id="u-good",
        )
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client.passive_observation(request_id="r2", generation=2)["coverage"] == "unknown"

    asyncio.run(scenario())


def test_passive_observer_matching_refinement_retains_evidence_and_wakes_exact_scope():
    class Page:
        def __init__(self):
            self.listeners: dict[str, list] = {}
        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)
        def remove_listener(self, event, callback):
            callbacks = self.listeners.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)
        off = remove_listener

    class Request:
        method = "POST"
        post_data_json = {"messages": [{"id": "u1", "author": {"role": "user"}}]}
    class Response:
        request = Request()
        url = "https://chatgpt.com/backend-api/f/conversation"
        async def body(self):
            return b'data: {"conversation_id":"c1","message":{"id":"a1","author":{"role":"assistant"},"recipient":"all","content":{"content_type":"text","parts":["done"]}}}\ndata: [DONE]\n'

    async def scenario():
        page = Page()
        client = chatgpt_module.ChatGPTPage(page)
        client.arm_passive_observer(request_id="r1", generation=1)
        waiter = asyncio.create_task(
            client.wait_for_passive_observation(request_id="r1", generation=1, timeout_ms=500)
        )
        page.listeners["response"][0](Response())
        assert await waiter is True
        client.arm_passive_observer(
            request_id="r1",
            generation=1,
            conversation_id="c1",
            accepted_user_message_id="u1",
        )
        retained = client.passive_observation(request_id="r1", generation=1)
        assert retained["conversation_id"] == "c1"
        assert retained["observed_user_message_id"] == "u1"
        assert await client.wait_for_passive_observation(
            request_id="other", generation=1, timeout_ms=1
        ) is None

        client.detach_passive_observer()
        assert await client.wait_for_passive_observation(
            request_id="r1", generation=1, timeout_ms=1
        ) is None
        client.arm_passive_observer(
            request_id="r2",
            generation=2,
            conversation_id="c2",
            accepted_user_message_id="u2",
        )
        assert await client.wait_for_passive_observation(
            request_id="r2", generation=2, timeout_ms=1
        ) is False

    asyncio.run(scenario())


def test_backend_reader_refreshes_once_on_401_and_classifies_statuses():
    import asyncio
    from playwright_auto.chatgpt import ChatGPTPage
    from playwright_auto.chatgpt_graph import (
        BackendAuthError,
        BackendNotReadyError,
        BackendSchemaError,
        BackendUnavailableError,
    )

    class Response:
        def __init__(self, status, payload):
            self.status = status
            self.payload = payload
        async def json(self):
            if isinstance(self.payload, BaseException):
                raise self.payload
            return self.payload

    class Requests:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    class Context:
        def __init__(self, requests): self.request = requests
    class Page:
        def __init__(self, requests): self.context = Context(requests)

    async def refreshed():
        requests = Requests([
            Response(200, {"accessToken": "token-1"}),
            Response(401, {}),
            Response(200, {"accessToken": "token-2"}),
            Response(200, {"status": "COMPLETE"}),
        ])
        client = ChatGPTPage(Page(requests))
        assert await client.backend_stream_status("conversation-1") == {"status": "COMPLETE"}
        assert len(requests.calls) == 4
        assert requests.calls[-1][1]["headers"]["Authorization"] == "Bearer token-2"
        assert client._backend_token == "token-2"
    asyncio.run(refreshed())

    async def failure(response, error_type):
        requests = Requests([
            Response(200, {"accessToken": "token"}), response,
        ])
        client = ChatGPTPage(Page(requests))
        with pytest.raises(error_type):
            await client.backend_stream_status("conversation-1")

    asyncio.run(failure(Response(404, {}), BackendNotReadyError))
    asyncio.run(failure(Response(429, {}), BackendUnavailableError))
    asyncio.run(failure(Response(503, {}), BackendUnavailableError))
    asyncio.run(failure(Response(200, ValueError("secret raw body")), BackendSchemaError))
    asyncio.run(failure(Response(200, {"status": "UNKNOWN"}), BackendSchemaError))
    asyncio.run(failure(Response(200, {}), BackendSchemaError))

    async def auth_failure():
        requests = Requests([
            Response(200, {"accessToken": "token-1"}), Response(401, {}),
            Response(200, {"accessToken": "token-2"}), Response(401, {}),
        ])
        client = ChatGPTPage(Page(requests))
        with pytest.raises(BackendAuthError):
            await client.backend_stream_status("conversation-1")
    asyncio.run(auth_failure())


def test_stream_status_shares_exact_conversation_freshness_and_inflight(monkeypatch):
    calls: list[tuple[object, str, str]] = []
    release = asyncio.Event()

    class Context:
        pass

    context = Context()

    async def fake_get(request_context, path, *, category):
        calls.append((request_context, path, category))
        await release.wait()
        return {"status": "IS_STREAMING"}

    monkeypatch.setattr(chatgpt_module, "_backend_get_object", fake_get)

    async def scenario():
        first = asyncio.create_task(chatgpt_module.backend_stream_status(context, "conversation-1"))
        second = asyncio.create_task(chatgpt_module.backend_stream_status(context, "conversation-1"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        assert await first == {"status": "IS_STREAMING"}
        assert await second == {"status": "IS_STREAMING"}
        assert await chatgpt_module.backend_stream_status(context, "conversation-1") == {
            "status": "IS_STREAMING"
        }
        assert len(calls) == 1

        backend_state = chatgpt_module._backend_context_state(context)
        backend_state["stream_status"]["conversation-1"]["next_poll_at"] = time.monotonic() - 1
        assert await chatgpt_module.backend_stream_status(context, "conversation-1") == {
            "status": "IS_STREAMING"
        }
        assert len(calls) == 2

    asyncio.run(scenario())


def test_stream_status_cache_is_bounded_and_release_is_exact(monkeypatch):
    class Context:
        pass

    context = Context()

    async def fake_get(_context, path, **_kwargs):
        return {"status": "COMPLETE", "path": path}

    monkeypatch.setattr(chatgpt_module, "_backend_get_object", fake_get)

    async def scenario():
        for index in range(64):
            result = await chatgpt_module.backend_stream_status(context, f"conversation-{index}")
            assert result["status"] == "COMPLETE"
        state = chatgpt_module._backend_context_state(context)
        slots = state["stream_status"]
        assert len(slots) <= chatgpt_module._STREAM_STATUS_CACHE_LIMIT
        assert "conversation-63" in slots

        active = slots["conversation-63"]
        active["payload"] = {"status": "IS_STREAMING"}
        active["next_poll_at"] = time.monotonic() + 30
        chatgpt_module.release_backend_stream_status(context, "conversation-0")
        assert "conversation-63" in slots
        chatgpt_module.release_backend_stream_status(context, "conversation-63")
        assert "conversation-63" not in slots

    asyncio.run(scenario())


def test_stream_status_release_preserves_other_inflight_slot(monkeypatch):
    class Context:
        pass

    context = Context()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_get(_context, path, **_kwargs):
        if "conversation-active" in path:
            started.set()
            await release.wait()
            return {"status": "IS_STREAMING"}
        return {"status": "COMPLETE"}

    monkeypatch.setattr(chatgpt_module, "_backend_get_object", fake_get)

    async def scenario():
        pending = asyncio.create_task(
            chatgpt_module.backend_stream_status(context, "conversation-active")
        )
        await started.wait()
        await chatgpt_module.backend_stream_status(context, "conversation-done")
        chatgpt_module.release_backend_stream_status(context, "conversation-done")
        state = chatgpt_module._backend_context_state(context)["stream_status"]
        assert "conversation-active" in state
        assert not state["conversation-active"]["in_flight"].done()
        release.set()
        assert await pending == {"status": "IS_STREAMING"}

    asyncio.run(scenario())


def test_stream_status_reader_classifies_transport_timeout_as_unavailable():
    import asyncio
    from playwright_auto.chatgpt import ChatGPTPage
    from playwright_auto.chatgpt_graph import BackendUnavailableError

    class Response:
        status = 200
        async def json(self):
            return {"accessToken": "token"}

    class Requests:
        def __init__(self):
            self.calls = 0
        async def get(self, _url, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return Response()
            raise TimeoutError("transport timed out")

    class Page:
        def __init__(self):
            self.context = type("Context", (), {"request": Requests()})()

    async def run():
        client = ChatGPTPage(Page())
        with pytest.raises(BackendUnavailableError) as captured:
            await client.backend_stream_status("conversation-1")
        assert captured.value.status_code == 0
        assert captured.value.category == "stream_status"

    asyncio.run(run())


def _wait_receipt() -> SendReceipt:
    return SendReceipt(
        prompt="probe",
        prompt_sha256="probe-sha",
        binding=PageBinding(page_id="page-plan", role="PLAN"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="test",
        session_id_before=None,
    )


def test_snapshot_bounds_never_returning_full_evaluate():
    class Page:
        async def evaluate(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    async def run():
        client = ChatGPTPage(Page(), timeout_ms=30)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.snapshot(), timeout=0.2)
        assert time.monotonic() - started < 0.12

    asyncio.run(run())


def test_assert_ownership_bounds_role_indicator_evaluate(monkeypatch):
    snapshot = _activity_snapshot(activity_text="", activity_length=0)

    class Page:
        async def add_init_script(self, **_kwargs):
            return None

        async def evaluate(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    async def fake_snapshot(_page):
        return snapshot

    monkeypatch.setattr(chatgpt_module, "inspect_chatgpt_page", fake_snapshot)

    async def run():
        client = ChatGPTPage(Page(), timeout_ms=30)
        client.binding = PageBinding(snapshot.page_id or "", snapshot.page_role or "")
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.assert_ownership(), timeout=0.2)
        assert time.monotonic() - started < 0.12

    asyncio.run(run())


def test_wait_snapshot_bounds_never_returning_sparse_probe():
    class Page:
        url = "https://chatgpt.com/c/test"

        async def evaluate(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    async def run():
        client = ChatGPTPage(Page(), timeout_ms=30)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.wait_snapshot(_wait_receipt()), timeout=0.2)
        assert time.monotonic() - started < 0.12

    asyncio.run(run())


def test_wait_snapshot_sparse_error_and_full_fallback_share_one_timeout_budget():
    class Page:
        url = "https://chatgpt.com/c/test"

        def __init__(self):
            self.calls = 0

        async def evaluate(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Page.evaluate: Target crashed")
            await asyncio.Event().wait()

    async def run():
        page = Page()
        client = ChatGPTPage(page, timeout_ms=30)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.wait_snapshot(_wait_receipt()), timeout=0.2)
        assert time.monotonic() - started < 0.12
        assert page.calls == 2

    asyncio.run(run())


def test_backend_auth_token_is_reused_across_page_wrappers_for_one_context():
    import asyncio
    from playwright_auto.chatgpt import ChatGPTPage

    class Response:
        status = 200
        def __init__(self, payload): self.payload = payload
        async def json(self): return self.payload
    class Requests:
        def __init__(self):
            self.calls = []
            self.responses = [
                Response({"accessToken": "shared-token"}),
                Response({"status": "IS_STREAMING"}),
                Response({"status": "COMPLETE"}),
            ]
        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)
    class Context:
        def __init__(self): self.request = Requests()
    class Page:
        def __init__(self, context): self.context = context

    async def run():
        context = Context()
        first = ChatGPTPage(Page(context))
        second = ChatGPTPage(Page(context))
        assert await first.backend_stream_status("conversation-1") == {"status": "IS_STREAMING"}
        assert await second.backend_stream_status("conversation-1") == {"status": "IS_STREAMING"}
        assert len(context.request.calls) == 2
        assert [url for url, _kwargs in context.request.calls].count(
            "https://chatgpt.com/api/auth/session"
        ) == 1
        assert second._backend_token == "shared-token"
    asyncio.run(run())



def test_full_conversation_graph_request_surfaces_are_removed():
    import playwright_auto.chatgpt as cg
    from playwright_auto.cdpa_actions import CDPATabActions

    assert not hasattr(cg, "backend_conversation")
    assert not hasattr(cg, "backend_search_conversations")
    assert not hasattr(cg.ChatGPTPage, "backend_conversation")
    assert not hasattr(CDPATabActions, "backend_conversation")
    assert not hasattr(CDPATabActions, "backend_search_conversations")
