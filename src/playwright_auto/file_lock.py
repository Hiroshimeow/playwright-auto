from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _ensure_lock_byte(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)


def _lock_windows(handle: BinaryIO, *, blocking: bool, poll_seconds: float) -> None:
    _ensure_lock_byte(handle)
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(
                    errno.EWOULDBLOCK,
                    "file lock is already held",
                ) from exc
            time.sleep(poll_seconds)


def _unlock_windows(handle: BinaryIO) -> None:
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _lock_unix(handle: BinaryIO, *, blocking: bool) -> None:
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(handle.fileno(), flags)


def _unlock_unix(handle: BinaryIO) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(
    path: str | Path,
    *,
    blocking: bool = True,
    poll_seconds: float = 0.05,
) -> Iterator[BinaryIO]:
    """Hold one cross-process exclusive lock on a dedicated lock file."""
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    lock_path = Path(path).expanduser().resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            _lock_windows(handle, blocking=blocking, poll_seconds=poll_seconds)
        else:
            _lock_unix(handle, blocking=blocking)
        try:
            yield handle
        finally:
            if os.name == "nt":
                _unlock_windows(handle)
            else:
                _unlock_unix(handle)


def fsync_parent_directory(path: str | Path) -> None:
    """Durably flush a parent directory where the platform supports it."""
    if os.name == "nt":
        return
    parent = Path(path).expanduser().resolve().parent
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(parent, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
