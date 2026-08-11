import pytest

from playwright_auto.chatgpt_graph import (
    BackendNotReadyError,
    BackendSchemaError,
    GraphIdentityError,
    resolve_exact_new_user_message,
    resolve_terminal_assistant,
    resolve_latest_terminal_assistant,
    resolve_inherited_assistant,
    resolve_bootstrap_donor,
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

    resolved = resolve_terminal_assistant(
        later_human,
        "u1",
        proven_later_human_message_ids={"u2"},
    )
    assert (resolved.message_id, resolved.text) == ("a1", "first")

    with pytest.raises(BackendSchemaError):
        resolve_terminal_assistant({"mapping": {}, "current_node": "missing"}, "u1")


def test_force_resume_resolver_accepts_manual_steering_and_unique_detached_branch():
    steered = {
        "current_node": "a2",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="pre-steer", children=("u2",)),
            "u2": msg("u2", "user", parent="a1", text="route dev", children=("a2",)),
            "a2": msg("a2", "assistant", parent="u2", text="final after steer"),
        },
    }
    with pytest.raises(GraphIdentityError):
        resolve_terminal_assistant(steered, "u1")

    resolved = resolve_terminal_assistant(
        steered,
        "u1",
        allow_manual_steering=True,
    )
    assert (resolved.message_id, resolved.text) == ("a2", "final after steer")

    detached = {
        "current_node": "other-a",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="detached final"),
            "other-u": msg("other-u", "user", children=("other-a",)),
            "other-a": msg("other-a", "assistant", parent="other-u", text="unrelated current"),
        },
    }
    with pytest.raises(GraphIdentityError):
        resolve_terminal_assistant(detached, "u1")

    resolved = resolve_terminal_assistant(
        detached,
        "u1",
        allow_detached_branch=True,
    )
    assert (resolved.message_id, resolved.text) == ("a1", "detached final")

    detached["mapping"]["u1"]["children"] = ["a1", "a1b"]
    detached["mapping"]["a1b"] = msg("a1b", "assistant", parent="u1", text="competing final")
    with pytest.raises(GraphIdentityError):
        resolve_terminal_assistant(
            detached,
            "u1",
            allow_detached_branch=True,
        )


def test_historical_resolver_requires_proof_for_every_later_human():
    graph = {
        "current_node": "a3",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="accepted terminal", children=("u2",)),
            "u2": msg("u2", "user", parent="a1", text="foreign one", children=("a2",)),
            "a2": msg("a2", "assistant", parent="u2", text="foreign response", children=("u3",)),
            "u3": msg("u3", "user", parent="a2", text="manual or unproven", children=("a3",)),
            "a3": msg("a3", "assistant", parent="u3", text="latest response"),
        },
    }

    with pytest.raises(GraphIdentityError):
        resolve_terminal_assistant(
            graph,
            "u1",
            proven_later_human_message_ids={"u2"},
        )

    resolved = resolve_terminal_assistant(
        graph,
        "u1",
        proven_later_human_message_ids={"u2", "u3"},
    )
    assert (resolved.message_id, resolved.text) == ("a1", "accepted terminal")


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


def test_exact_new_user_uses_latest_baseline_anchor_not_unrendered_history():
    graph = {
        "current_node": "u-accepted",
        "mapping": {
            "u-old": msg("u-old", "user", text="historical", children=("a-old",)),
            "a-old": msg("a-old", "assistant", parent="u-old", text="old", children=("u-base",)),
            "u-base": msg("u-base", "user", parent="a-old", text="recent", children=("a-base",)),
            "a-base": msg("a-base", "assistant", parent="u-base", text="recent answer", children=("u-accepted",)),
            "u-accepted": msg("u-accepted", "user", parent="a-base", text="durable prompt"),
        },
    }

    resolved = resolve_exact_new_user_message(
        graph,
        "durable prompt",
        excluded_message_ids={"u-base", "a-base"},
    )

    assert resolved == "u-accepted"


def test_exact_new_user_ignores_tool_internal_user_continuation():
    graph = {
        "current_node": "a-final",
        "mapping": {
            "u-base": msg("u-base", "user", text="recent", children=("a-base",)),
            "a-base": msg("a-base", "assistant", parent="u-base", text="recent answer", children=("u-accepted",)),
            "u-accepted": msg("u-accepted", "user", parent="a-base", text="durable prompt", children=("a-call",)),
            "a-call": msg("a-call", "assistant", parent="u-accepted", recipient="web.run", text="call", children=("tool",)),
            "tool": msg("tool", "tool", parent="a-call", recipient="assistant", text="result", children=("u-internal",)),
            "u-internal": msg("u-internal", "user", parent="tool", text="continue", children=("a-final",)),
            "a-final": msg("a-final", "assistant", parent="u-internal", text="done"),
        },
    }

    resolved = resolve_exact_new_user_message(
        graph,
        "durable prompt",
        excluded_message_ids={"u-base", "a-base"},
    )

    assert resolved == "u-accepted"


def test_exact_new_user_fails_closed_for_two_humans_after_baseline_anchor():
    graph = {
        "current_node": "u-followup",
        "mapping": {
            "u-base": msg("u-base", "user", text="recent", children=("a-base",)),
            "a-base": msg("a-base", "assistant", parent="u-base", text="recent answer", children=("u-accepted",)),
            "u-accepted": msg("u-accepted", "user", parent="a-base", text="durable prompt", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u-accepted", text="answer", children=("u-followup",)),
            "u-followup": msg("u-followup", "user", parent="a1", text="manual follow-up"),
        },
    }

    with pytest.raises(GraphIdentityError):
        resolve_exact_new_user_message(
            graph,
            "durable prompt",
            excluded_message_ids={"u-base", "a-base"},
        )


def test_latest_terminal_assistant_uses_latest_user_branch():
    graph = {
        "current_node": "a2",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="old", children=("u2",)),
            "u2": msg("u2", "user", parent="a1", text="next", children=("a2",)),
            "a2": msg("a2", "assistant", parent="u2", text="latest"),
        },
    }
    resolved = resolve_latest_terminal_assistant(graph)
    assert (resolved.message_id, resolved.text) == ("a2", "latest")


def test_inherited_assistant_is_child_local_fork_point_before_role_user():
    graph = {
        "current_node": "a-role",
        "mapping": {
            "u-bootstrap": msg("u-bootstrap", "user", children=("a-child-bootstrap",)),
            "a-child-bootstrap": msg(
                "a-child-bootstrap", "assistant", parent="u-bootstrap", text="bootstrap prefix", children=("u-role",)
            ),
            "u-role": msg("u-role", "user", parent="a-child-bootstrap", text="PLAN role prompt", children=("a-role",)),
            "a-role": msg("a-role", "assistant", parent="u-role", text="PLAN response"),
        },
    }
    inherited = resolve_inherited_assistant(graph, "u-role")
    assert (inherited.message_id, inherited.text) == ("a-child-bootstrap", "bootstrap prefix")


def test_bootstrap_donor_requires_exact_public_assistant_message():
    graph = {
        "current_node": "a1",
        "mapping": {
            "u1": msg("u1", "user", children=("a1",)),
            "a1": msg("a1", "assistant", parent="u1", text="bootstrap"),
        },
    }
    donor = resolve_bootstrap_donor(graph, "a1")
    assert (donor.message_id, donor.text) == ("a1", "bootstrap")
    with pytest.raises(GraphIdentityError):
        resolve_bootstrap_donor(graph, "missing")
    with pytest.raises(GraphIdentityError):
        resolve_bootstrap_donor(graph, "u1")
