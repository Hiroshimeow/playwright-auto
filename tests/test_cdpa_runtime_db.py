from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import playwright_auto.cdpa_runtime_db as runtime_db_module
from playwright_auto.cdpa_projection import TaskProjection
from playwright_auto.cdpa_runtime_db import IdempotencyConflict, RuntimeDB


def projection(task_id: str, *, status: str = "RUNNING", updated: str = "2026-07-25T00:00:00+00:00") -> TaskProjection:
    summary = {
        "task_id": task_id,
        "team": task_id,
        "status": status,
        "surface": "history" if status in {"DONE", "STOPPED"} else "active",
        "updated_at": updated,
        "version": 0,
    }
    return TaskProjection(
        task_id=task_id,
        team=task_id,
        status=status,
        surface=summary["surface"],
        active_role="DEV" if status == "RUNNING" else None,
        updated_at=updated,
        summary=summary,
        detail={**summary, "timeline": []},
        private={"manifest_path": f"/private/{task_id}.json", "reports": {}, "maintenance_reports": {}},
    )


def test_schema_has_exactly_three_tables_and_required_pragmas(tmp_path: Path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()

    with db.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {"runtime_snapshot", "task_projection", "command_queue"}
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000




def test_projection_replace_and_noop_upsert_preserve_board_generation(tmp_path: Path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    catalog = {"complete": True, "discovered_at": "now", "errors": [], "control_repository": "/repo"}

    first_generation = db.replace_task_projections([projection("a")], catalog=catalog)
    assert first_generation == 1
    assert db.get_task_version("a") == 1
    assert db.get_board()["generation"] == 1

    assert db.upsert_task_projections([projection("a")]) == 1
    assert db.get_task_version("a") == 1

    changed = projection("a", status="DONE", updated="2026-07-25T01:00:00+00:00")
    assert db.upsert_task_projections([changed]) == 2
    assert db.get_task_version("a") == 2
    assert db.get_task_detail("a")["version"] == 2




def test_idempotent_enqueue_conflict_claim_finish_and_requeue(tmp_path: Path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    first = db.enqueue_command(
        command_id="cmd-1",
        idempotency_key="same",
        kind="task_control",
        task_id="a",
        expected_task_version=1,
        payload={"action": "pause"},
    )
    duplicate = db.enqueue_command(
        command_id="cmd-other",
        idempotency_key="same",
        kind="task_control",
        task_id="a",
        expected_task_version=1,
        payload={"action": "pause"},
    )
    assert duplicate["command_id"] == first["command_id"] == "cmd-1"

    with pytest.raises(IdempotencyConflict):
        db.enqueue_command(
            command_id="cmd-2",
            idempotency_key="same",
            kind="task_control",
            task_id="a",
            expected_task_version=1,
            payload={"action": "stop"},
        )

    claimed = db.claim_next_command()
    assert claimed["command_id"] == "cmd-1"
    assert claimed["status"] == "running"
    assert db.requeue_running_commands() == 1
    assert db.claim_next_command()["command_id"] == "cmd-1"
    db.finish_command("cmd-1", result={"ok": True})
    assert db.get_command("cmd-1")["status"] == "applied"




def test_board_includes_terminal_summaries_without_moving_detail_into_payload(tmp_path: Path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    rows = [projection("running"), projection("done", status="DONE"), projection("stopped", status="STOPPED")]
    db.replace_task_projections(
        rows,
        catalog={"complete": True, "discovered_at": "now", "errors": [], "control_repository": "/repo"},
    )

    board = db.get_board()

    assert {item["task_id"] for item in board["items"]} == {"running", "done", "stopped"}
    assert all("timeline" not in item for item in board["items"])
    assert len(json.dumps(board, separators=(",", ":")).encode()) < 100 * 1024








def test_runtime_db_reuses_one_connection_across_read_paths(tmp_path: Path, monkeypatch):
    real_connect = sqlite3.connect
    calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(runtime_db_module.sqlite3, "connect", counted_connect)
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    with db.connection() as first:
        first.execute("SELECT 1").fetchone()
    with db.connection() as second:
        second.execute("SELECT 1").fetchone()

    assert first is second
    assert calls == 1
    db.close()
