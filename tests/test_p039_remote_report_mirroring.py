from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_routes import expected_report_relative
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.chatgpt import MessageBaseline, PageBinding, SendReceipt, prompt_digest
from playwright_auto.chatgpt_graph import BackendSchemaError, resolve_completed_file_write
from playwright_auto.dashboard_api import DashboardAPI

from test_cdpa_core import write_config


REMOTE_REPOSITORY = r"E:\python_project\Screens-Trans-Chatbot"
REMOTE_TASK = f"""Remote report fixture.

HARD EXECUTION AUTHORITY / BASELINE
- Actual product repository: {REMOTE_REPOSITORY} on the user's Windows ThinkBook. Use @mcp-thinkbook ONLY for product inspection, edits, tests, Git and bounded runtime probes.
- Linux control repository is orchestration only.
"""


def msg(
    message_id: str,
    role: str,
    *,
    parent: str | None = None,
    recipient: str = "all",
    text: str = "",
    children: tuple[str, ...] = (),
):
    return {
        "id": message_id,
        "message": {
            "id": message_id,
            "author": {"role": role},
            "recipient": recipient,
            "content": {"content_type": "text", "parts": [text] if text else []},
        },
        "parent": parent,
        "children": list(children),
    }


def report_graph(*, remote_path: str, content: str, duplicate: bool = False):
    args = json.dumps({"path": remote_path, "content": content})
    mapping = {
        "u1": msg("u1", "user", children=("call1",)),
        "call1": msg(
            "call1",
            "assistant",
            parent="u1",
            recipient="mcp-thinkbook.write_file",
            text=args,
            children=("tool1",),
        ),
        "tool1": msg("tool1", "tool", parent="call1", recipient="assistant", text="ok"),
    }
    if duplicate:
        mapping["tool1"]["children"] = ["call2"]
        mapping["call2"] = msg(
            "call2",
            "assistant",
            parent="tool1",
            recipient="mcp-thinkbook.write_file",
            text=args,
            children=("tool2",),
        )
        mapping["tool2"] = msg(
            "tool2", "tool", parent="call2", recipient="assistant", text="ok", children=("final",)
        )
        final_parent = "tool2"
    else:
        mapping["tool1"]["children"] = ["final"]
        final_parent = "tool1"
    mapping["final"] = msg(
        "final",
        "assistant",
        parent=final_parent,
        text='{"route":"DEV","handoff":"placeholder"}',
    )
    return {"current_node": "final", "mapping": mapping}


def setup_remote_task(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        REMOTE_TASK,
        requested_team="alpha",
        task_id="task-remote",
        report_mode="file",
        roles=("PLAN", "DEV", "REVIEW"),
        repository=tmp_path,
    )
    hop = _active_hop(state)
    hop["expected_report_path"] = expected_report_relative(
        plans_root=config.plans_root,
        repository_root=config.repository_root,
        team=str(state["team"]),
        physical_role=str(hop["physical_role"]),
        turn=int(hop["turn"]),
        task_id=str(state["task_id"]),
    )
    return config, state, CDPAWorker(config, store=store)


def expected_remote_path(hop: dict) -> str:
    return REMOTE_REPOSITORY + "\\" + str(hop["expected_report_path"]).replace("/", "\\")


def test_resolve_completed_file_write_requires_unique_completed_exact_write():
    expected = REMOTE_REPOSITORY + r"\.plan\alpha\alpha-plan_turn1_task-remote.md"
    graph = report_graph(remote_path=expected, content="exact remote bytes")

    assert resolve_completed_file_write(
        graph,
        "u1",
        "final",
        recipient="mcp-thinkbook.write_file",
        expected_path=expected,
    ) == "exact remote bytes"

    wrong = report_graph(remote_path=REMOTE_REPOSITORY + r"\.plan\other\stolen.md", content="bad")
    with pytest.raises(BackendSchemaError):
        resolve_completed_file_write(
            wrong,
            "u1",
            "final",
            recipient="mcp-thinkbook.write_file",
            expected_path=expected,
        )

    duplicate = report_graph(remote_path=expected, content="exact remote bytes", duplicate=True)
    with pytest.raises(BackendSchemaError):
        resolve_completed_file_write(
            duplicate,
            "u1",
            "final",
            recipient="mcp-thinkbook.write_file",
            expected_path=expected,
        )


def test_passively_observed_completion_captures_remote_report_before_route_validation(tmp_path: Path):
    _config, state, worker = setup_remote_task(tmp_path)
    hop = _active_hop(state)
    report = "normal completion bytes\n"
    route = json.dumps({"route": "DEV", "handoff": hop["expected_report_path"]}, separators=(",", ":"))
    graph = report_graph(remote_path=expected_remote_path(hop), content=report)
    graph["mapping"]["final"]["message"]["content"]["parts"] = [route]

    worker._capture_remote_report_mirror(
        state,
        hop,
        graph,
        accepted_user_message_id="u1",
        terminal_assistant_message_id="final",
        response_text=route,
    )
    hop["response"] = route
    hop["response_sha256"] = hashlib.sha256(route.encode("utf-8")).hexdigest()
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert hop["state"] == "routed"
    assert hop["route"] == "DEV"
    assert Path(hop["mirrored_report_path"]).read_bytes() == report.encode("utf-8")
    assert hop["report_sha256"] == hashlib.sha256(report.encode("utf-8")).hexdigest()
    assert hop["validation_error"] is None


def test_passive_observation_can_materialize_exact_remote_report_without_graph_request(tmp_path: Path):
    _config, state, worker = setup_remote_task(tmp_path)
    hop = _active_hop(state)
    report = "# passive remote report\n\nExact bytes.\n"
    route = json.dumps(
        {"route": "DEV", "handoff": hop["expected_report_path"]},
        separators=(",", ":"),
    )
    graph = report_graph(remote_path=expected_remote_path(hop), content=report)
    graph["mapping"]["final"]["message"]["content"]["parts"] = [route]
    receipt = SendReceipt(
        prompt="remote passive completion",
        prompt_sha256=prompt_digest("remote passive completion"),
        binding=PageBinding("page-PLAN", "PLAN"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="u1",
        conversation_id="conversation-passive-remote",
    )
    response = SimpleNamespace(message_id="final", text=route)

    class Client:
        def passive_observation(self, **kwargs):
            assert kwargs == {"request_id": hop["request_id"], "generation": 0}
            return {
                "coverage": "complete",
                "observed_user_message_id": "u1",
                "conversation_id": "conversation-passive-remote",
                "graph": graph,
            }

    acquired = SimpleNamespace(client=Client())
    worker._capture_passive_remote_report_if_available(
        state, hop, acquired, receipt, response
    )

    mirrored = Path(hop["mirrored_report_path"])
    assert mirrored.read_bytes() == report.encode("utf-8")
    assert hop["mirrored_report_sha256"] == hashlib.sha256(report.encode("utf-8")).hexdigest()


def test_remote_report_mirror_is_central_durable_and_served_after_remote_source_is_absent(tmp_path: Path):
    config, state, worker = setup_remote_task(tmp_path)
    hop = _active_hop(state)
    report = "# PLAN report\n\nExact UTF-8 remote content.\n"
    route = json.dumps({"route": "DEV", "handoff": hop["expected_report_path"]}, separators=(",", ":"))
    graph = report_graph(remote_path=expected_remote_path(hop), content=report)
    graph["mapping"]["final"]["message"]["content"]["parts"] = [route]

    worker._capture_remote_report_mirror(
        state,
        hop,
        graph,
        accepted_user_message_id="u1",
        terminal_assistant_message_id="final",
        response_text=route,
    )
    hop["response"] = route
    hop["validation_error"] = None
    worker._responded(state, hop)

    data = report.encode("utf-8")
    central_path = tmp_path / hop["expected_report_path"]
    assert central_path.read_bytes() == data
    assert hop["report_path"] == str(central_path.resolve())
    assert hop["report_sha256"] == hashlib.sha256(data).hexdigest()
    assert hop["report_size"] == len(data)

    projection = build_task_projection(
        state,
        tasks=[state],
        repository_allowed_roots=(tmp_path,),
    )
    public = projection.detail["reports"][0]
    assert public["availability"] == "available"
    assert public["url"] == "/api/reports/task-remote/1"

    class ProjectionDB:
        def ensure_schema(self):
            pass

        def get_task_private(self, task_id):
            return projection.private if task_id == projection.task_id else None

    api = DashboardAPI(
        SimpleNamespace(
            repository_root=tmp_path,
            plans_root=tmp_path / ".plan",
            repository_allowed_roots=(tmp_path.resolve(),),
        ),
        db=ProjectionDB(),
    )
    assert api.report_bytes("task-remote", "1", maintenance=False) == data


def test_remote_report_mirror_fails_closed_when_central_hash_or_size_changes(tmp_path: Path):
    _config, state, worker = setup_remote_task(tmp_path)
    hop = _active_hop(state)
    report = "immutable report\n"
    route = json.dumps({"route": "DEV", "handoff": hop["expected_report_path"]}, separators=(",", ":"))
    graph = report_graph(remote_path=expected_remote_path(hop), content=report)
    graph["mapping"]["final"]["message"]["content"]["parts"] = [route]

    worker._capture_remote_report_mirror(
        state,
        hop,
        graph,
        accepted_user_message_id="u1",
        terminal_assistant_message_id="final",
        response_text=route,
    )
    Path(hop["mirrored_report_path"]).write_text("tampered\n", encoding="utf-8")
    hop["response"] = route
    hop["validation_error"] = None

    worker._responded(state, hop)

    assert hop["state"] == "routed"
    assert hop["route"] == "PLAN"
    assert not state.get("reports")
    assert "mirror" in str(hop.get("validation_error") or "").lower()
    assert _active_hop(state)["kind"] == "route_repair"
