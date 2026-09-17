from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


@contextmanager
def open_shared_read(path: str | Path) -> Iterator[BinaryIO]:
    """Open a read-only file without blocking atomic replacement on Windows."""
    target = Path(path).expanduser().resolve()
    if os.name != "nt":
        with target.open("rb") as handle:
            yield handle
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(target),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # READ | WRITE | DELETE sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if raw_handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(
            raw_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(raw_handle)
        raise
    with os.fdopen(fd, "rb") as handle:
        yield handle


def read_shared_bytes(path: str | Path) -> bytes:
    with open_shared_read(path) as handle:
        return handle.read()


def read_shared_text(path: str | Path, *, encoding: str = "utf-8") -> str:
    return read_shared_bytes(path).decode(encoding)


def _windows_posix_replace(source: Path, target: Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    raw_handle = create_file(
        str(source),
        0x00010000,  # DELETE
        0x00000001 | 0x00000002 | 0x00000004,  # READ | WRITE | DELETE sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if raw_handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        class RenameHeader(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        encoded_name = str(target).encode("utf-16-le")
        name_offset = RenameHeader.FileName.offset
        buffer = ctypes.create_string_buffer(name_offset + len(encoded_name) + 2)
        header = RenameHeader.from_buffer(buffer)
        header.Flags = 0x00000001 | 0x00000002  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
        header.RootDirectory = None
        header.FileNameLength = len(encoded_name)
        ctypes.memmove(
            ctypes.addressof(buffer) + name_offset,
            encoded_name + b"\0\0",
            len(encoded_name) + 2,
        )
        if not set_information(raw_handle, 22, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(raw_handle)


def atomic_replace(source: str | Path, target: str | Path) -> None:
    """Atomically replace target, tolerating replacement-safe readers on Windows."""
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    try:
        os.replace(source_path, target_path)
        return
    except PermissionError as primary:
        if os.name != "nt":
            raise
        try:
            _windows_posix_replace(source_path, target_path)
        except OSError as native_error:
            raise primary from native_error


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
