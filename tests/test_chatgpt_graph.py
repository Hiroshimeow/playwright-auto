import pytest

from playwright_auto.chatgpt_graph import (
    BackendNotReadyError,
    BackendSchemaError,
    GraphIdentityError,
    resolve_terminal_assistant,
)


def msg(message_id, role, *, parent=None, recipient="all", text="", content_type="text", children=()):
    return {
        "id": message_id,
        "message": {
            "id": message_id,
            "author": {"role": role},
            "recipient": recipient,
            "content": {"content_type": content_type, "parts": [text] if text else []},
        },
        "parent": parent,
        "children": list(children),
    }


def test_resolve_terminal_assistant_on_exact_current_branch():
    graph = {
        "current_node": "a1",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="final"),
        },
    }
    resolved = resolve_terminal_assistant(graph, "u1")
    assert resolved.message_id == "a1"
    assert resolved.text == "final"
    assert resolved.content_type == "text"


def test_resolver_stops_at_exact_user_before_structural_client_root():
    graph = {
        "current_node": "a1",
        "mapping": {
            "client-created-root": {
                "id": "client-created-root",
                "message": None,
                "parent": None,
                "children": ["u1"],
            },
            "u1": msg("u1", "user", parent="client-created-root", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="final"),
        },
    }

    resolved = resolve_terminal_assistant(graph, "u1")

    assert (resolved.message_id, resolved.text) == ("a1", "final")


def test_resolve_terminal_assistant_handles_tool_chain_and_internal_user_continuation():
    graph = {
        "current_node": "a-final",
        "mapping": {
            "u1": msg("u1", "user", children=("a-call",)),
            "a-call": msg("a-call", "assistant", parent="u1", recipient="web.run", text="call", children=("tool",)),
            "tool": msg("tool", "tool", parent="a-call", recipient="assistant", text="result", children=("u-internal",)),
            "u-internal": msg("u-internal", "user", parent="tool", recipient="all", text="continue", children=("a-final",)),
            "a-final": msg("a-final", "assistant", parent="u-internal", text="done"),
        },
    }
    resolved = resolve_terminal_assistant(graph, "u1")
    assert (resolved.message_id, resolved.text) == ("a-final", "done")


def test_resolver_fails_closed_for_off_branch_later_human_and_malformed_graph():
    off_branch = {
        "current_node": "a2",
        "mapping": {
            "u1": msg("u1", "user"),
            "u2": msg("u2", "user", children=("a2",)),
            "a2": msg("a2", "assistant", parent="u2", text="other"),
        },
    }
    with pytest.raises(GraphIdentityError):
        resolve_terminal_assistant(off_branch, "u1")

    later_human = {
        "current_node": "a2",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="first", children=("u2",)),
            "u2": msg("u2", "user", parent="a1", text="real follow-up", children=("a2",)),
            "a2": msg("a2", "assistant", parent="u2", text="second"),
        },
    }
    with pytest.raises(GraphIdentityError):
        resolve_terminal_assistant(later_human, "u1")

    with pytest.raises(BackendSchemaError):
        resolve_terminal_assistant({"mapping": {}, "current_node": "missing"}, "u1")


def test_resolver_not_ready_when_exact_user_or_terminal_assistant_not_materialized():
    graph = {"current_node": "u1", "mapping": {"u1": msg("u1", "user")}}
    with pytest.raises(BackendNotReadyError):
        resolve_terminal_assistant(graph, "u1")
    with pytest.raises(BackendNotReadyError):
        resolve_terminal_assistant(graph, "future-user")


def test_resolver_does_not_return_stale_assistant_while_tool_call_is_unresolved():
    graph = {
        "current_node": "a-call",
        "mapping": {
            "u1": msg("u1", "user", children=("a-visible",)),
            "a-visible": msg(
                "a-visible", "assistant", parent="u1", text="intermediate", children=("a-call",)
            ),
            "a-call": msg(
                "a-call", "assistant", parent="a-visible", recipient="web.run", text="call"
            ),
        },
    }
    with pytest.raises(BackendNotReadyError):
        resolve_terminal_assistant(graph, "u1")


def test_resolver_accepts_terminal_multimodal_text():
    graph = {
        "current_node": "a1",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg(
                "a1", "assistant", parent="u1", text="multimodal final", content_type="multimodal_text"
            ),
        },
    }
    resolved = resolve_terminal_assistant(graph, "u1")
    assert (resolved.message_id, resolved.text, resolved.content_type) == (
        "a1", "multimodal final", "multimodal_text"
    )


def test_resolver_rejects_cycle_and_canonical_id_mismatch_as_schema():
    cycle = {
        "current_node": "a1",
        "mapping": {
            "u1": {**msg("u1", "user", parent="a1", children=("a1",))},
            "a1": msg("a1", "assistant", parent="u1", text="never terminal"),
        },
    }
    with pytest.raises(BackendSchemaError):
        resolve_terminal_assistant(cycle, "u1")

    mismatch = {
        "current_node": "a1",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": {
                **msg("a1", "assistant", parent="u1", text="final"),
                "message": {
                    **msg("wrong", "assistant", parent="u1", text="final")["message"],
                },
            },
        },
    }
    with pytest.raises(BackendSchemaError):
        resolve_terminal_assistant(mismatch, "u1")


def test_resolver_rejects_assistant_final_without_tool_result_after_tool_call():
    graph = {
        "current_node": "a-final",
        "mapping": {
            "u1": msg("u1", "user", children=("a-call",)),
            "a-call": msg(
                "a-call",
                "assistant",
                parent="u1",
                recipient="web.run",
                text="call",
                children=("a-final",),
            ),
            "a-final": msg(
                "a-final",
                "assistant",
                parent="a-call",
                text="should-not-be-terminal",
            ),
        },
    }
    with pytest.raises(BackendNotReadyError):
        resolve_terminal_assistant(graph, "u1")


def test_resolver_requires_new_terminal_after_completed_tool_result():
    graph = {
        "current_node": "tool",
        "mapping": {
            "u1": msg("u1", "user", children=("a-visible",)),
            "a-visible": msg(
                "a-visible",
                "assistant",
                parent="u1",
                text="intermediate",
                children=("a-call",),
            ),
            "a-call": msg(
                "a-call",
                "assistant",
                parent="a-visible",
                recipient="web.run",
                text="call",
                children=("tool",),
            ),
            "tool": msg(
                "tool",
                "tool",
                parent="a-call",
                recipient="assistant",
                text="result",
            ),
        },
    }
    with pytest.raises(BackendNotReadyError):
        resolve_terminal_assistant(graph, "u1")
