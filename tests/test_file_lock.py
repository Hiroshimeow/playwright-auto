from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

import playwright_auto.file_lock as file_lock_module
from playwright_auto.durable import RequestLedger
from playwright_auto.file_lock import exclusive_file_lock


def _hold_lock(path: str, ready, release) -> None:
    with exclusive_file_lock(Path(path)):
        ready.put("locked")
        release.get(timeout=10)


def _increment_under_lock(lock_path: str, counter_path: str, count: int, start) -> None:
    lock = Path(lock_path)
    counter = Path(counter_path)
    if not start.wait(timeout=10):
        raise RuntimeError("counter start gate timed out")
    for _ in range(count):
        with exclusive_file_lock(lock):
            current = int(counter.read_text(encoding="utf-8"))
            counter.write_text(str(current + 1), encoding="utf-8")


def test_file_lock_serializes_repeated_cross_process_read_modify_write(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    lock_path = tmp_path / "counter.lock"
    counter_path = tmp_path / "counter.txt"
    counter_path.write_text("0", encoding="utf-8")
    start = context.Event()
    processes = [
        context.Process(
            target=_increment_under_lock,
            args=(str(lock_path), str(counter_path), 20, start),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert counter_path.read_text(encoding="utf-8") == "80"


def test_file_lock_is_released_after_holder_process_is_terminated(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Queue()
    lock_path = tmp_path / "crash-release.lock"
    process = context.Process(target=_hold_lock, args=(str(lock_path), ready, release))
    process.start()
    assert ready.get(timeout=10) == "locked"
    process.terminate()
    process.join(timeout=10)
    assert not process.is_alive()

    with exclusive_file_lock(lock_path, blocking=False):
        pass


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


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_shared_reader_allows_atomic_replace_while_read_handle_is_open(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    temporary = tmp_path / "state.json.tmp"
    target.write_bytes(b"old")
    temporary.write_bytes(b"new")
    shared_reader = getattr(file_lock_module, "open_shared_read", None)
    atomic_replace = getattr(file_lock_module, "atomic_replace", None)
    assert shared_reader is not None, "replacement-safe Windows reader is required"
    assert atomic_replace is not None, "replacement-safe Windows rename is required"

    with shared_reader(target) as handle:
        assert handle.read() == b"old"
        atomic_replace(temporary, target)
        handle.seek(0)
        assert handle.read() == b"old"

    assert target.read_bytes() == b"new"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_atomic_replace_fails_closed_for_incompatible_windows_reader(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    temporary = tmp_path / "state.json.tmp"
    target.write_bytes(b"old")
    temporary.write_bytes(b"new")

    with target.open("rb"):
        with pytest.raises(PermissionError):
            file_lock_module.atomic_replace(temporary, target)

    assert target.read_bytes() == b"old"
    assert temporary.read_bytes() == b"new"


def test_request_ledger_persists_on_current_platform(tmp_path: Path) -> None:
    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(role="REVIEW", prompt="cross-platform persistence")

    reloaded = RequestLedger(tmp_path / "ledger.json").get(record.request_id)

    assert reloaded is not None
    assert reloaded.request_id == record.request_id
    assert reloaded.role == "REVIEW"
