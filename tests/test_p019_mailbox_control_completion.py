from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker

from test_cdpa_core import write_config


class GuardActions:
    def __init__(self, calls: dict[str, int]):
        self.calls = calls
        calls["constructed"] += 1

    async def locate_owned(self, _state, _role):
        self.calls["locate"] += 1
        return SimpleNamespace()

    async def stop_if_active(self, _acquired):
        self.calls["stop"] += 1
        return False

    async def _forbid(self, *_args, **_kwargs):
        self.calls["transport"] += 1
        raise AssertionError("pre-transport control must not mutate browser transport")

    acquire = restart = new_chat = reopen = open_tab = preflight_team = close_team = _forbid


def _waiting_task(tmp_path: Path, *, task_id: str):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "unfinished parent",
        requested_team=f"{task_id}-parent",
        task_id=f"{task_id}-parent",
    )
    child = store.create_task(
        "dependency-held child",
        requested_team=f"{task_id}-child",
        task_id=task_id,
        depends_on_task_ids=(parent["task_id"],),
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    return config, store, child, worker


def _enqueue_control(
    worker: CDPAWorker,
    state: dict,
    *,
    command_id: str,
    action: str,
    expected_task_version: int | None = None,
):
    return worker.runtime_db.enqueue_command(
        command_id=command_id,
        idempotency_key=command_id,
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=expected_task_version,
        payload={"action": action, "reason": f"P-019 {action} fixture"},
    )


def _guard(monkeypatch):
    calls = {"constructed": 0, "locate": 0, "stop": 0, "transport": 0}
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: GuardActions(calls),
    )
    return calls


def test_waiting_stop_mailbox_tracks_durable_control_and_restart_replay(
    tmp_path: Path,
    monkeypatch,
):
    config, store, child, worker = _waiting_task(tmp_path, task_id="p019-stop")
    path = Path(child["manifest_path"])
    calls = _guard(monkeypatch)
    _enqueue_control(worker, child, command_id="cmd-p019-stop", action="stop")

    delivered = worker.dispatch_command_once()
    requested = store.load(path)
    assert delivered["status"] == "running"
    assert requested["status"] == "WAITING"
    assert requested["controls"][-1]["status"] == "requested"
    assert requested["controls"][-1]["applied_at"] is None
    assert len(requested["controls"]) == 1
    assert calls == {"constructed": 0, "locate": 0, "stop": 0, "transport": 0}

    assert worker.runtime_db.requeue_running_commands() == 1
    restarted = CDPAWorker(config, store=store)
    restarted.hydrate_runtime()
    replayed_delivery = restarted.dispatch_command_once()
    assert replayed_delivery["status"] == "running"
    assert len(store.load(path)["controls"]) == 1

    stopped = asyncio.run(
        restarted.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=store.discover(),
        )
    )
    command = restarted.runtime_db.get_command("cmd-p019-stop")
    assert stopped["status"] == "STOPPED"
    assert stopped["controls"][-1]["status"] == "applied"
    assert stopped["controls"][-1]["applied_at"] is not None
    assert command["status"] == "applied"
    assert command["result"]["stopped_response"] is False
    assert len(stopped["controls"]) == 1
    assert calls == {"constructed": 1, "locate": 1, "stop": 1, "transport": 0}

    with restarted.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            ("cmd-p019-stop",),
        )
    terminal_replay = restarted.dispatch_command_once()
    assert terminal_replay["status"] == "applied"
    assert len(store.load(path)["controls"]) == 1
    assert calls == {"constructed": 1, "locate": 1, "stop": 1, "transport": 0}


def test_waiting_pause_mailbox_mirrors_existing_rejection_without_transport(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, child, worker = _waiting_task(tmp_path, task_id="p019-pause")
    path = Path(child["manifest_path"])
    calls = _guard(monkeypatch)
    _enqueue_control(worker, child, command_id="cmd-p019-pause", action="pause")

    delivered = worker.dispatch_command_once()
    requested = store.load(path)
    assert delivered["status"] == "running"
    assert requested["status"] == "WAITING"
    assert requested["controls"][-1]["status"] == "requested"
    assert requested["controls"][-1]["applied_at"] is None

    waiting = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=store.discover(),
        )
    )
    command = worker.runtime_db.get_command("cmd-p019-pause")
    control = waiting["controls"][-1]
    assert waiting["status"] == "WAITING"
    assert control["status"] == "rejected"
    assert control["applied_at"] is not None
    assert "Pause is valid only for an INBOX or RUNNING task" in str(control["result"])
    assert command["status"] == "failed"
    assert "Pause is valid only for an INBOX or RUNNING task" in command["error"]
    assert calls == {"constructed": 1, "locate": 0, "stop": 0, "transport": 0}


def test_stale_task_control_version_fails_before_control_mutation(tmp_path: Path):
    _config, store, child, worker = _waiting_task(tmp_path, task_id="p019-stale")
    path = Path(child["manifest_path"])
    before = path.read_bytes()
    version = worker.runtime_db.get_task_version(child["task_id"])
    assert version is not None
    _enqueue_control(
        worker,
        child,
        command_id="cmd-p019-stale",
        action="pause",
        expected_task_version=version + 1,
    )

    failed = worker.dispatch_command_once()

    assert failed["status"] == "failed"
    assert "stale task version" in failed["error"]
    assert path.read_bytes() == before
    assert store.load(path)["controls"] == []
