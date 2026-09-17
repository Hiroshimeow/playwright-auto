from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

from .cdpa_endpoints import normalize_endpoint_record
from .config import resolve_chromium
from .file_lock import exclusive_file_lock


@contextmanager
def _windows_process(pid: int, access: int) -> Iterator[tuple[Any, Any]]:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(access, False, pid)
    if not handle and ctypes.get_last_error() != 87:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield kernel, handle
    finally:
        if handle:
            kernel.CloseHandle(handle)


def _windows_birth(kernel: Any, handle: Any) -> str | None:
    import ctypes
    from ctypes import wintypes
    status = kernel.WaitForSingleObject(handle, 0)
    if status == 0:
        return None
    if status != 0x102:
        raise ctypes.WinError(ctypes.get_last_error())
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) terminates the process on Windows; use a read-only handle.
        try:
            with _windows_process(pid, 0x00100000) as (kernel, handle):
                return bool(handle and kernel.WaitForSingleObject(handle, 0) == 0x102)
        except PermissionError:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_token(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        with _windows_process(pid, 0x00100000 | 0x1000) as (kernel, handle):
            return _windows_birth(kernel, handle) if handle else None
    stat = Path(f"/proc/{pid}/stat")
    if Path('/proc').is_dir():
        try:
            fields = stat.read_text().rsplit(')', 1)[1].split()
            return None if fields[0] == 'Z' else fields[19]
        except FileNotFoundError:
            return None
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True, timeout=3)
    return result.stdout.strip() or None


def _terminate_pid(pid: int, expected_start: str) -> None:
    if os.name == "nt":
        import ctypes
        # Validate birth and terminate the SAME handle, never a possibly reused PID.
        with _windows_process(pid, 0x00100000 | 0x1000 | 1) as (kernel, handle):
            if not handle:
                return
            if _windows_birth(kernel, handle) != expected_start:
                raise ValueError('process ownership changed; refusing to stop reused PID')
            if not kernel.TerminateProcess(handle, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if kernel.WaitForSingleObject(handle, 5000) != 0:
                raise RuntimeError('owned browser did not exit')
        return
    if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
        fd = os.pidfd_open(pid)
        try:
            if _process_start_token(pid) != expected_start:
                raise ValueError('process ownership changed; refusing to stop reused PID')
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)
        return
    if _process_start_token(pid) != expected_start:
        raise ValueError('process ownership changed; refusing to stop reused PID')
    os.kill(pid, signal.SIGTERM)


def _endpoint_online(address: str) -> bool:
    endpoint = urlsplit(address)
    try:
        with socket.create_connection((endpoint.hostname, endpoint.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


def _profile_arg_from_argv(argv: list[str]) -> str | None:
    for index, argument in enumerate(argv):
        if argument.startswith('--user-data-dir='):
            return argument.split('=', 1)[1]
        if argument == '--user-data-dir' and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _profile_in_use(profile: Path) -> bool:
    """Read browser command lines only to compare explicit profile paths; do not log them."""
    if not profile.exists():
        return False
    if os.name == 'nt':
        command = "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='chromium.exe' OR Name='msedge.exe' OR Name='thorium.exe'\" | Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=8, check=True)
        rows = json.loads(result.stdout.strip() or '[]')
        commands = [rows] if isinstance(rows, str) else rows or []
    elif Path('/proc').is_dir():
        commands = []
        for path in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                commands.append(path.read_bytes()[:65536].split(b'\0'))
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    else:
        result = subprocess.run(['ps', '-ax', '-o', 'command='], capture_output=True, text=True, timeout=5, check=True)
        commands = result.stdout.splitlines()
    wanted = os.path.normcase(str(profile.resolve()))
    for command in commands:
        if isinstance(command, list):
            argv = [part.decode('utf-8', 'replace') for part in command if part]
            value = _profile_arg_from_argv(argv)
        else:
            match = re.search(r'(?:^|\s)(?:"--user-data-dir=([^"]+)"|--user-data-dir(?:=|\s+)(?:"([^"]+)"|(\S+)))', str(command or ''))
            value = next((part for part in match.groups() if part is not None), None) if match else None
        if value and os.path.normcase(str(Path(value).resolve())) == wanted:
            return True
    return False


def _profile_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not value or not path.is_absolute():
        raise ValueError('profile_path must be an explicit absolute path')
    if any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (path, *path.parents)):
        raise ValueError('profile paths must not traverse a symlink or junction')
    return path.resolve()


def _assert_no_profile_links(source: Path) -> None:
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in (*dirs, *files):
            entry = Path(directory) / name
            if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
                raise ValueError('profile clone contains a symlink or junction; use a safe stopped profile')


class ManagedBrowserLifecycle:
    def __init__(self, config: Any, *, popen_factory: Callable[..., Any] = subprocess.Popen,
                 pid_alive: Callable[[int], bool] = _pid_alive,
                 process_token: Callable[[int], str | None] = _process_start_token,
                 terminate_pid: Callable[[int, str], None] = _terminate_pid) -> None:
        self.config, self.popen_factory = config, popen_factory
        self.pid_alive, self.process_token, self.terminate_pid = pid_alive, process_token, terminate_pid
        self.runtime_root = config.repository_root / '.runtime' / 'cdpa-browsers'

    def runtime_dir(self, endpoint_id: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', endpoint_id):
            raise ValueError('invalid browser endpoint identity')
        return self.runtime_root / endpoint_id

    def _profile_lock_path(self, profile: Path) -> Path:
        canonical = os.path.normcase(str(profile.resolve()))
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        return self.runtime_root / 'profile-locks' / f'{digest}.lock'

    @contextmanager
    def _profile_locks(self, *profiles: Path) -> Iterator[None]:
        unique = sorted({profile.resolve() for profile in profiles}, key=lambda path: os.path.normcase(str(path)))
        with ExitStack() as stack:
            for profile in unique:
                stack.enter_context(exclusive_file_lock(self._profile_lock_path(profile)))
            yield

    def pid_file(self, endpoint_id: str) -> Path:
        return self.runtime_dir(endpoint_id) / 'browser.pid'

    def log_file(self, endpoint_id: str) -> Path:
        return self.runtime_dir(endpoint_id) / 'browser.log'

    def read_pid(self, endpoint_id: str) -> int | None:
        path = self.pid_file(endpoint_id)
        try:
            pid = int(path.read_text(encoding='utf-8').strip())
            if not 0 < pid <= 0x7fffffff:
                raise ValueError('process identity is outside the supported PID range')
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            raise ValueError('invalid process ownership receipt') from exc
        if not self.pid_alive(pid):
            return None
        try:
            receipt = json.loads((path.parent / 'process.json').read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise ValueError('live PID has no verifiable ownership receipt; refusing to adopt it') from exc
        if not isinstance(receipt, Mapping):
            raise ValueError('invalid process ownership receipt')
        if (receipt.get('pid') != pid or receipt.get('endpoint_id') != endpoint_id
                or not receipt.get('started') or self.process_token(pid) != receipt['started']):
            raise ValueError('process ownership changed or PID was reused; refusing to adopt it')
        return pid

    def process_state(self, endpoint_id: str) -> dict[str, Any]:
        try:
            return {'managed_running': self.read_pid(endpoint_id) is not None, 'process_ownership': 'verified', 'process_error': None}
        except (ValueError, OSError) as exc:
            return {'managed_running': None, 'process_ownership': 'unverified', 'process_error': str(exc)}

    @staticmethod
    def _managed(record: Mapping[str, Any]) -> dict[str, Any]:
        if record.get('management_mode') != 'managed-local':
            raise ValueError('browser endpoint is not managed-local')
        return normalize_endpoint_record(record, endpoint_id=str(record.get('endpoint_id') or ''))

    def prepare_profile(self, *, profile_path: str, clone_from: Mapping[str, Any] | None = None,
                        source_running: bool = False) -> Path:
        target = _profile_path(profile_path)
        source = None
        if clone_from is not None:
            source_record = self._managed(clone_from)
            source = _profile_path(str(source_record['profile_path']))
        with self._profile_locks(*(path for path in (source, target) if path is not None)):
            return self._prepare_profile_locked(target, source, source_running)

    def _prepare_profile_locked(self, target: Path, source: Path | None, source_running: bool) -> Path:
        if _profile_in_use(target):
            raise ValueError('target profile is in use by a running browser')
        if source is None:
            target.mkdir(parents=True, exist_ok=True)
            return target
        if source == target or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError('clone source and target must not overlap')
        if source_running or _profile_in_use(source):
            raise ValueError('cannot clone a running browser profile')
        if not source.is_dir():
            raise ValueError('clone source profile does not exist')
        if target.exists() and any(target.iterdir()):
            raise ValueError('clone target profile must be absent or empty')
        _assert_no_profile_links(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.cdpa-clone-', dir=target.parent))
        try:
            shutil.copytree(source, staging, dirs_exist_ok=True, symlinks=True)
            _assert_no_profile_links(staging)
            if target.exists():
                target.rmdir()  # Never delete pre-existing user content.
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return target

    def start(self, record: Mapping[str, Any]) -> int:
        record = self._managed(record)
        if record['draining'] or record['removed']:
            raise ValueError('cannot start a draining or removed browser endpoint')
        endpoint_id = record['endpoint_id']
        runtime = self.runtime_dir(endpoint_id)
        runtime.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(runtime / 'lifecycle.lock'):
            existing = self.read_pid(endpoint_id)
            if existing is not None:
                return existing
            if _endpoint_online(record['cdp_url']):
                raise ValueError('CDP port is occupied by an unowned browser; attach as external instead')
            profile = _profile_path(record['profile_path'])
            with self._profile_locks(profile):
                if _profile_in_use(profile):
                    raise ValueError('target profile is in use by a running browser')
                profile.mkdir(parents=True, exist_ok=True)
                executable = str(record.get('executable_path') or resolve_chromium())
                if not Path(executable).is_file() and not shutil.which(executable):
                    raise ValueError('Chromium executable not found; supply executable_path')
                parsed = urlsplit(record['cdp_url'])
                command = [executable, f'--remote-debugging-address={parsed.hostname}', f'--remote-debugging-port={parsed.port}',
                           f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', 'https://chatgpt.com/']
                kwargs: dict[str, Any] = {'cwd': str(self.config.repository_root), 'env': dict(os.environ), 'stderr': subprocess.STDOUT}
                if os.name == 'nt':
                    kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs['start_new_session'] = True
                with self.log_file(endpoint_id).open('ab') as log:
                    process = self.popen_factory(command, stdout=log, **kwargs)
                pid = int(process.pid)
                started = self.process_token(pid)
                if started is None:
                    raise RuntimeError('managed browser exited before process identity could be recorded; inspect browser.log')
                receipt = {'endpoint_id': endpoint_id, 'pid': pid, 'started': started, 'profile_path': str(profile), 'cdp_url': record['cdp_url']}
                temporary = runtime / 'process.json.tmp'
                with temporary.open('w', encoding='utf-8') as handle:
                    json.dump(receipt, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, runtime / 'process.json')
                self.pid_file(endpoint_id).write_text(str(pid), encoding='utf-8')
                return pid

    def stop(self, record: Mapping[str, Any]) -> bool:
        record = self._managed(record)
        endpoint_id = record['endpoint_id']
        runtime = self.runtime_dir(endpoint_id)
        runtime.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(runtime / 'lifecycle.lock'):
            pid = self.read_pid(endpoint_id)
            if pid is None:
                self.pid_file(endpoint_id).unlink(missing_ok=True)
                return False
            receipt = json.loads((runtime / 'process.json').read_text(encoding='utf-8'))
            self.terminate_pid(pid, receipt['started'])
            self.pid_file(endpoint_id).unlink(missing_ok=True)
            return True
