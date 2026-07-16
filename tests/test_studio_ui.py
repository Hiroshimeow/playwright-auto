from __future__ import annotations

import tkinter as tk
from concurrent.futures import Future

import pytest

from playwright_auto.studio.app import StudioApp
from playwright_auto.studio.models import StudioState, WorkerModel, WorkerStatus
from playwright_auto.studio.widgets import (
    LogNotebook,
    WorkerCard,
    drop_target_index,
)


class FakeController:
    def __init__(self) -> None:
        self.state = StudioState(
            workers=[
                WorkerModel(
                    page_id="a",
                    title="DEV tab",
                    url="https://chatgpt.com/c/a",
                    role="DEV",
                    order=0,
                    status=WorkerStatus.IDLE,
                ),
                WorkerModel(
                    page_id="b",
                    title="REVIEW tab",
                    url="https://chatgpt.com/c/b",
                    role="REVIEW",
                    order=1,
                    status=WorkerStatus.QUEUED,
                ),
            ]
        )
        self.calls: list[tuple] = []

    def snapshot(self):
        return StudioState.from_dict(self.state.to_dict())

    def poll_events(self, limit=256):
        return []

    def _future(self, *call):
        self.calls.append(call)
        future: Future[None] = Future()
        future.set_result(None)
        return future

    def connect(self): return self._future("connect")
    def refresh_tabs(self): return self._future("refresh")
    def assign_role(self, page_id, role): return self._future("assign", page_id, role)
    def release_role(self, page_id): return self._future("release", page_id)
    def set_enabled(self, page_id, enabled): return self._future("enabled", page_id, enabled)
    def reorder(self, page_id, index): return self._future("reorder", page_id, index)
    def update_prompt(self, page_id, prompt, stop_and_retry=False):
        return self._future("prompt", page_id, prompt, stop_and_retry)
    def update_settings(self, settings): return self._future("settings", settings)
    def start_run(self): return self._future("start")
    def pause_run(self): return self._future("pause")
    def resume_run(self): return self._future("resume")
    def stop_worker(self, page_id): return self._future("stop_worker", page_id)
    def stop_run(self): return self._future("stop")
    def shutdown(self): self.calls.append(("shutdown",))


@pytest.fixture
def root():
    try:
        instance = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk is unavailable: {exc}")
    instance.withdraw()
    yield instance
    if instance.winfo_exists():
        instance.destroy()


def test_drop_target_index_uses_card_midpoints():
    bounds = [(0, 100), (110, 210), (220, 320)]

    assert drop_target_index(bounds, 20) == 0
    assert drop_target_index(bounds, 160) == 1
    assert drop_target_index(bounds, 400) == 2


def test_worker_card_running_state_enables_glow(root):
    model = WorkerModel(
        page_id="a",
        title="DEV tab",
        url="https://chatgpt.com/c/a",
        role="DEV",
        status=WorkerStatus.RUNNING,
    )
    card = WorkerCard(root, model=model)
    card.pack()
    root.update_idletasks()

    assert card.glow_active is True
    assert card.status_text.get() == "RUNNING"

    model.status = WorkerStatus.COMPLETED
    card.update_model(model)
    root.update_idletasks()
    assert card.glow_active is False
    assert card.status_text.get() == "COMPLETED"

    card.destroy()


def test_log_notebook_creates_worker_tab_and_bounds_lines(root):
    logs = LogNotebook(root, max_lines=5)
    logs.pack()
    for index in range(10):
        logs.append("DEV", f"line-{index}")
    root.update_idletasks()

    assert "DEV" in logs.channels
    text = logs.get_text("DEV")
    assert "line-9" in text
    assert "line-0" not in text
    assert len(text.splitlines()) <= 5

    logs.destroy()


def test_studio_app_hidden_root_smoke_and_worker_actions(root):
    controller = FakeController()
    app = StudioApp(root, controller=controller, auto_connect=False)
    app.pack(fill="both", expand=True)
    root.update_idletasks()

    assert set(app.worker_list.cards) == {"a", "b"}
    assert app.connection_text.get() in {"OFFLINE", "CONNECTING", "CONNECTED"}

    app._assign_role("a", "SECURITY")
    app._set_enabled("b", False)
    app._reorder_worker("b", 0)
    root.update_idletasks()

    assert ("assign", "a", "SECURITY") in controller.calls
    assert ("enabled", "b", False) in controller.calls
    assert ("reorder", "b", 0) in controller.calls

    app.close()
    assert ("shutdown",) in controller.calls
