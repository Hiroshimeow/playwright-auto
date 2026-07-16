from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

import playwright_auto.studio.controller as studio_controller
from playwright_auto.studio.controller import (
    DiscoveredTab,
    PlaywrightStudioBackend,
    StudioEngine,
)
from playwright_auto.studio.models import RunStatus, StudioSettings, WorkerStatus


@dataclass
class SendCall:
    page_id: str
    prompt: str
    timeout_ms: int
    request_context: dict[str, object]


class FakeBackend:
    def __init__(self, tabs: list[DiscoveredTab]) -> None:
        self.tabs = tabs
        self.connected = False
        self.assigned: list[tuple[str, str]] = []
        self.released: list[str] = []
        self.prepared: list[tuple[str, str]] = []
        self.sends: list[SendCall] = []
        self.stops: list[str] = []
        self.responses: dict[str, list[str]] = {}
        self.gates: dict[str, asyncio.Event] = {}

    async def connect(self, cdp_url: str) -> None:
        assert cdp_url.startswith("http://127.0.0.1:")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def discover(self) -> list[DiscoveredTab]:
        return list(self.tabs)

    async def assign_role(self, page_id: str, role: str) -> DiscoveredTab:
        self.assigned.append((page_id, role))
        for index, tab in enumerate(self.tabs):
            if tab.page_id == page_id:
                updated = DiscoveredTab(
                    page_id=tab.page_id,
                    title=tab.title,
                    url=tab.url,
                    role=role,
                    browser_state=tab.browser_state,
                    requires_login=tab.requires_login,
                )
                self.tabs[index] = updated
                return updated
        raise KeyError(page_id)

    async def release_role(self, page_id: str) -> DiscoveredTab:
        self.released.append(page_id)
        for index, tab in enumerate(self.tabs):
            if tab.page_id == page_id:
                updated = DiscoveredTab(
                    page_id=tab.page_id,
                    title=tab.title,
                    url=tab.url,
                    role=None,
                    browser_state=tab.browser_state,
                    requires_login=tab.requires_login,
                )
                self.tabs[index] = updated
                return updated
        raise KeyError(page_id)

    async def prepare_task(self, page_id: str, task_id: str) -> None:
        self.prepared.append((page_id, task_id))

    async def send_and_wait(
        self,
        page_id: str,
        prompt: str,
        timeout_ms: int,
        request_context: dict[str, object],
    ) -> str:
        self.sends.append(SendCall(page_id, prompt, timeout_ms, request_context))
        queue = self.responses.setdefault(page_id, [])
        response = queue.pop(0) if queue else f"response-{page_id}-{len(self.sends)}"
        gate = self.gates.get(page_id)
        if gate is not None:
            await gate.wait()
        return response

    async def stop(self, page_id: str) -> None:
        self.stops.append(page_id)
        gate = self.gates.get(page_id)
        if gate is not None:
            gate.set()


def tab(page_id: str, role: str | None) -> DiscoveredTab:
    return DiscoveredTab(
        page_id=page_id,
        title=f"Tab {page_id}",
        url=f"https://chatgpt.com/c/{page_id}",
        role=role,
        browser_state="ready",
        requires_login=False,
    )


def make_engine(tmp_path: Path, backend: FakeBackend) -> StudioEngine:
    return StudioEngine(
        backend,
        runtime_dir=tmp_path / "studio",
    )


def test_discovery_preserves_existing_prompt_and_order(tmp_path):
    async def scenario() -> None:
        backend = FakeBackend([tab("a", "DEV"), tab("b", "REVIEW")])
        engine = make_engine(tmp_path, backend)
        await engine.connect()
        engine.state.workers[0].prompt_template = "custom"
        engine.reorder("b", 0)

        backend.tabs = [tab("a", "DEV"), tab("b", "REVIEW"), tab("c", None)]
        await engine.refresh_tabs()

        assert [worker.page_id for worker in engine.state.workers] == ["b", "a", "c"]
        assert next(item for item in engine.state.workers if item.page_id == "a").prompt_template == "custom"
        assert engine.state.workers[-1].role is None

    asyncio.run(scenario())


def test_assign_and_release_role_update_backend_and_state(tmp_path):
    async def scenario() -> None:
        backend = FakeBackend([tab("a", None)])
        engine = make_engine(tmp_path, backend)
        await engine.connect()

        await engine.assign_role("a", "SECURITY")
        assert engine.state.workers[0].role == "SECURITY"
        assert backend.assigned == [("a", "SECURITY")]

        await engine.release_role("a")
        assert engine.state.workers[0].role is None
        assert backend.released == ["a"]

    asyncio.run(scenario())


def test_sequential_run_passes_previous_response_to_next_worker(tmp_path):
    async def scenario() -> None:
        backend = FakeBackend([tab("a", "DEV"), tab("b", "REVIEW")])
        backend.responses = {"a": ["IMPLEMENTED_ALPHA"], "b": ["REVIEWED_ALPHA"]}
        engine = make_engine(tmp_path, backend)
        await engine.connect()
        engine.state.settings = StudioSettings(
            global_goal="Ship alpha",
            task_id="TASK-ALPHA",
            rounds=1,
            response_timeout_ms=30_000,
            context_char_limit=2_000,
        )

        await engine.start_run()
        await engine.wait_for_run()

        assert engine.state.run_status is RunStatus.COMPLETED
        assert [call.page_id for call in backend.sends] == ["a", "b"]
        assert "IMPLEMENTED_ALPHA" in backend.sends[1].prompt
        assert backend.sends[0].request_context["task_id"] == "TASK-ALPHA"
        assert backend.sends[1].request_context["round_index"] == 0
        assert engine.state.workers[0].responses[-1] == "IMPLEMENTED_ALPHA"
        assert engine.state.workers[1].responses[-1] == "REVIEWED_ALPHA"
        assert (tmp_path / "studio" / "runs" / "TASK-ALPHA.json").is_file()

    asyncio.run(scenario())


def test_pause_waits_before_starting_next_worker(tmp_path):
    async def scenario() -> None:
        backend = FakeBackend([tab("a", "DEV"), tab("b", "REVIEW")])
        backend.gates["a"] = asyncio.Event()
        engine = make_engine(tmp_path, backend)
        await engine.connect()
        engine.state.settings.global_goal = "Goal"
        engine.state.settings.task_id = "TASK-PAUSE"

        await engine.start_run()
        while not backend.sends:
            await asyncio.sleep(0)
        engine.pause_run()
        backend.gates["a"].set()
        await asyncio.sleep(0.02)

        assert [call.page_id for call in backend.sends] == ["a"]
        assert engine.state.run_status in {RunStatus.PAUSING, RunStatus.PAUSED}

        engine.resume_run()
        await engine.wait_for_run()
        assert [call.page_id for call in backend.sends] == ["a", "b"]

    asyncio.run(scenario())


def test_stop_run_stops_active_and_skips_remaining_workers(tmp_path):
    async def scenario() -> None:
        backend = FakeBackend([tab("a", "DEV"), tab("b", "REVIEW")])
        backend.gates["a"] = asyncio.Event()
        engine = make_engine(tmp_path, backend)
        await engine.connect()
        engine.state.settings.global_goal = "Goal"
        engine.state.settings.task_id = "TASK-STOP"

        await engine.start_run()
        while not backend.sends:
            await asyncio.sleep(0)
        await engine.stop_run()
        await engine.wait_for_run()

        assert backend.stops == ["a"]
        assert [call.page_id for call in backend.sends] == ["a"]
        assert engine.state.run_status is RunStatus.STOPPED
        assert engine.state.workers[1].status is WorkerStatus.STOPPED

    asyncio.run(scenario())


def test_edit_active_prompt_stop_and_retry_requeues_same_worker(tmp_path):
    async def scenario() -> None:
        backend = FakeBackend([tab("a", "DEV")])
        backend.gates["a"] = asyncio.Event()
        backend.responses["a"] = ["OLD", "NEW"]
        engine = make_engine(tmp_path, backend)
        await engine.connect()
        engine.state.settings.global_goal = "Goal"
        engine.state.settings.task_id = "TASK-RETRY"

        await engine.start_run()
        while not backend.sends:
            await asyncio.sleep(0)
        await engine.update_prompt("a", "Use the edited prompt", stop_and_retry=True)
        backend.gates["a"] = asyncio.Event()
        backend.gates["a"].set()
        await engine.wait_for_run()

        assert len(backend.sends) == 2
        assert "Use the edited prompt" in backend.sends[1].prompt
        assert backend.stops == ["a"]
        assert engine.state.workers[0].responses[-1] == "NEW"

    asyncio.run(scenario())


def test_playwright_backend_uses_durable_send_marker_and_studio_ledger(
    tmp_path, monkeypatch
):
    captured: dict[str, object] = {}

    class FakeBlock:
        def __init__(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)

        async def run(self, context):
            captured["client"] = context.client
            return {"response": {"text": "DURABLE_RESPONSE"}}

    class FakeClient:
        binding = object()

        @asynccontextmanager
        async def workflow_guard(self):
            yield

    monkeypatch.setattr(studio_controller, "DurableSendBlock", FakeBlock)
    backend = PlaywrightStudioBackend(ledger_path=tmp_path / "studio-ledger.json")
    backend._clients["page-a"] = FakeClient()  # type: ignore[assignment]

    result = asyncio.run(
        backend.send_and_wait(
            "page-a",
            "PROMPT",
            45_000,
            {"task_id": "TASK", "round_index": 2, "role": "DEV"},
        )
    )

    assert result == "DURABLE_RESPONSE"
    assert captured["ledger_path"] == tmp_path / "studio-ledger.json"
    assert captured["source_context"] == {
        "task_id": "TASK",
        "round_index": 2,
        "role": "DEV",
    }
    assert captured["response_timeout_ms"] == 45_000


def test_playwright_backend_stop_bypasses_workflow_guard():
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []
            self.binding = object()

        def workflow_guard(self):
            raise AssertionError("stop must not wait for the workflow guard")

        async def stop(self, *, timeout_ms: int) -> str:
            self.calls.append(("stop", timeout_ms))
            return "clicked"

    async def scenario() -> None:
        backend = PlaywrightStudioBackend()
        client = FakeClient()
        backend._clients["page-a"] = client  # type: ignore[assignment]

        await backend.stop("page-a")

        assert client.calls == [("stop", 15_000)]

    asyncio.run(scenario())


def test_start_rejects_duplicate_or_unassigned_roles(tmp_path):
    async def scenario() -> None:
        backend = FakeBackend([tab("a", "DEV"), tab("b", "DEV")])
        engine = make_engine(tmp_path, backend)
        await engine.connect()
        engine.state.settings.global_goal = "Goal"

        with pytest.raises(ValueError, match="duplicate roles"):
            await engine.start_run()

        backend.tabs = [tab("a", "DEV"), tab("b", None)]
        await engine.refresh_tabs()
        with pytest.raises(ValueError, match="unassigned"):
            await engine.start_run()

    asyncio.run(scenario())
