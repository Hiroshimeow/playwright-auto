from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from playwright_auto.durable import RequestLedger
from playwright_auto.file_lock import exclusive_file_lock


def _hold_lock(path: str, ready, release) -> None:
    with exclusive_file_lock(Path(path)):
        ready.put("locked")
        release.get(timeout=10)


def test_nonblocking_file_lock_rejects_second_process(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Queue()
    lock_path = tmp_path / "exclusive.lock"
    process = context.Process(
        target=_hold_lock,
        args=(str(lock_path), ready, release),
    )
    process.start()
    try:
        assert ready.get(timeout=10) == "locked"
        with pytest.raises(BlockingIOError):
            with exclusive_file_lock(lock_path, blocking=False):
                pass
    finally:
        release.put("release")
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0


def test_request_ledger_persists_on_current_platform(tmp_path: Path) -> None:
    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(role="REVIEW", prompt="cross-platform persistence")

    reloaded = RequestLedger(tmp_path / "ledger.json").get(record.request_id)

    assert reloaded is not None
    assert reloaded.request_id == record.request_id
    assert reloaded.role == "REVIEW"
