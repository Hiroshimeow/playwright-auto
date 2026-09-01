from datetime import datetime, timedelta, timezone

import pytest

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


def test_backend_reader_classifies_transport_timeout_as_unavailable():
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
            await client.backend_conversation("conversation-1")
        assert captured.value.status_code == 0
        assert captured.value.category == "conversation"

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
        assert await second.backend_stream_status("conversation-1") == {"status": "COMPLETE"}
        assert [url for url, _kwargs in context.request.calls].count(
            "https://chatgpt.com/api/auth/session"
        ) == 1
        assert second._backend_token == "shared-token"
    asyncio.run(run())


def test_backend_search_conversations_paginates_deduplicates_and_fails_closed_at_limit():
    import asyncio
    from playwright_auto.chatgpt import backend_search_conversations
    from playwright_auto.chatgpt_graph import BackendSchemaError

    class Response:
        status = 200
        def __init__(self, payload): self.payload = payload
        async def json(self): return self.payload

    class Requests:
        def __init__(self, payloads):
            self.calls = []
            self.payloads = list(payloads)
        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/api/auth/session"):
                return Response({"accessToken": "token"})
            return Response(self.payloads.pop(0))

    class Context:
        def __init__(self, payloads): self.request = Requests(payloads)

    async def paginated():
        context = Context([
            {
                "items": [
                    {"conversation_id": "conversation-1"},
                    {"conversation_id": "conversation-2"},
                ],
                "cursor": "next cursor",
            },
            {
                "items": [
                    {"conversation_id": "conversation-2"},
                    {"conversation_id": "conversation-3"},
                ],
                "cursor": None,
            },
        ])
        assert await backend_search_conversations(
            context, "task id/+", max_candidates=4
        ) == ["conversation-1", "conversation-2", "conversation-3"]
        urls = [url for url, _kwargs in context.request.calls if "/conversations/search" in url]
        assert urls[0].endswith("query=task%20id%2F%2B")
        assert urls[1].endswith("query=task%20id%2F%2B&cursor=next%20cursor")
    asyncio.run(paginated())

    async def truncated():
        context = Context([
            {
                "items": [
                    {"conversation_id": "conversation-1"},
                    {"conversation_id": "conversation-2"},
                ],
                "cursor": "more",
            }
        ])
        with pytest.raises(BackendSchemaError, match="bounded candidate limit"):
            await backend_search_conversations(context, "task-id", max_candidates=2)
    asyncio.run(truncated())

    async def bad_item():
        context = Context([{"items": [{"bad": True}], "cursor": None}])
        with pytest.raises(BackendSchemaError, match="unknown item shape"):
            await backend_search_conversations(context, "task-id")
    asyncio.run(bad_item())

    async def repeated_cursor():
        class RepeatingRequests:
            def __init__(self):
                self.search_calls = 0

            async def get(self, url, **_kwargs):
                await asyncio.sleep(0)
                if url.endswith("/api/auth/session"):
                    return Response({"accessToken": "token"})
                self.search_calls += 1
                return Response({
                    "items": [{"conversation_id": "conversation-1"}],
                    "cursor": "same-cursor",
                })

        context = type("Context", (), {"request": RepeatingRequests()})()
        with pytest.raises(BackendSchemaError, match="pagination cursor repeated"):
            await asyncio.wait_for(
                backend_search_conversations(context, "task-id", max_candidates=4),
                timeout=0.2,
            )
        assert context.request.search_calls == 2
    asyncio.run(repeated_cursor())

    async def changing_cursor_without_progress():
        class ChangingRequests:
            def __init__(self):
                self.search_calls = 0

            async def get(self, url, **_kwargs):
                if url.endswith("/api/auth/session"):
                    return Response({"accessToken": "token"})
                self.search_calls += 1
                return Response({
                    "items": [{"conversation_id": "conversation-1"}],
                    "cursor": f"cursor-{self.search_calls}",
                })

        context = type("Context", (), {"request": ChangingRequests()})()
        with pytest.raises(BackendSchemaError, match="bounded page limit"):
            await backend_search_conversations(context, "task-id", max_candidates=3)
        assert context.request.search_calls == 3
    asyncio.run(changing_cursor_without_progress())


def test_backend_conversation_validates_graph_shape():
    import asyncio
    from playwright_auto.chatgpt import ChatGPTPage
    from playwright_auto.chatgpt_graph import BackendSchemaError

    class Response:
        status = 200
        def __init__(self, payload): self.payload = payload
        async def json(self): return self.payload
    class Requests:
        def __init__(self): self.calls = 0
        async def get(self, url, **kwargs):
            self.calls += 1
            if url.endswith("/api/auth/session"):
                return Response({"accessToken": "token"})
            return Response({"bad": True})
    class Page:
        def __init__(self):
            self.context = type("Context", (), {"request": Requests()})()

    async def run():
        client = ChatGPTPage(Page())
        with pytest.raises(BackendSchemaError):
            await client.backend_conversation("conversation-1")
    asyncio.run(run())
