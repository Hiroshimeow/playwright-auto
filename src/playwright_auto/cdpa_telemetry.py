from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _numbers(path: Path) -> list[int]:
    return [int(value) for value in path.read_text(encoding="utf-8").splitlines()[0].split()[1:]]


class TelemetrySampler:
    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        disk_path: str | Path = "/",
        api_pid: int | None = None,
        worker_pid_provider: Callable[[], int | None] | None = None,
        gpu_provider: Callable[[], dict[str, Any] | None] | None = None,
        interval_seconds: float = 2.0,
    ) -> None:
        self.proc_root = Path(proc_root)
        self.disk_path = Path(disk_path)
        self.api_pid = os.getpid() if api_pid is None else int(api_pid)
        self.worker_pid_provider = worker_pid_provider or (lambda: None)
        self.gpu_provider = gpu_provider
        self.interval_seconds = float(interval_seconds)
        self._lock = threading.Lock()
        self._latest: dict[str, Any] = {
            "sample_version": 0,
            "sampled_at": None,
            "host": {},
            "processes": {"api": None, "worker": None},
            "gpu": None,
        }
        self._previous_cpu: tuple[int, int] | None = None

    def _cpu(self) -> float:
        values = _numbers(self.proc_root / "stat")
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None:
            return 0.0
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return 0.0
        return round(max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta)), 2)

    def _memory(self) -> tuple[int, int]:
        values: dict[str, int] = {}
        for line in (self.proc_root / "meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        return max(0, total - available), total

    def _loads(self) -> tuple[float, float, float]:
        fields = (self.proc_root / "loadavg").read_text(encoding="utf-8").split()
        return float(fields[0]), float(fields[1]), float(fields[2])

    def _network(self) -> tuple[int, int]:
        rx = tx = 0
        for line in (self.proc_root / "net" / "dev").read_text(encoding="utf-8").splitlines()[2:]:
            if ":" not in line:
                continue
            _name, raw = line.split(":", 1)
            fields = raw.split()
            if len(fields) >= 9:
                rx += int(fields[0])
                tx += int(fields[8])
        return rx, tx

    def _process(self, pid: int | None) -> dict[str, Any] | None:
        if pid is None:
            return None
        root = self.proc_root / str(pid)
        try:
            stat = (root / "stat").read_text(encoding="utf-8").split()
            status: dict[str, str] = {}
            for line in (root / "status").read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
            io_values: dict[str, int] = {}
            for line in (root / "io").read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    io_values[key] = int(value.strip())
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
            return None
        return {
            "pid": pid,
            "state": stat[2] if len(stat) > 2 else None,
            "cpu_ticks": int(stat[13]) + int(stat[14]) if len(stat) > 14 else 0,
            "rss_bytes": int(status.get("VmRSS", "0 kB").split()[0]) * 1024,
            "read_bytes": io_values.get("read_bytes", 0),
            "write_bytes": io_values.get("write_bytes", 0),
        }

    def sample_once(self) -> dict[str, Any]:
        cpu = self._cpu()
        memory_used, memory_total = self._memory()
        load_1, load_5, load_15 = self._loads()
        disk = os.statvfs(self.disk_path)
        disk_total = disk.f_blocks * disk.f_frsize
        disk_available = disk.f_bavail * disk.f_frsize
        rx, tx = self._network()
        worker_pid = self.worker_pid_provider()
        gpu = self.gpu_provider() if self.gpu_provider is not None else None
        with self._lock:
            version = int(self._latest["sample_version"]) + 1
            self._latest = {
                "sample_version": version,
                "sampled_at": _now(),
                "host": {
                    "cpu_percent": cpu,
                    "load_1": load_1,
                    "load_5": load_5,
                    "load_15": load_15,
                    "memory_used_bytes": memory_used,
                    "memory_total_bytes": memory_total,
                    "disk_used_bytes": max(0, disk_total - disk_available),
                    "disk_total_bytes": disk_total,
                    "network_rx_bytes": rx,
                    "network_tx_bytes": tx,
                },
                "processes": {
                    "api": self._process(self.api_pid),
                    "worker": self._process(worker_pid),
                },
                "gpu": gpu,
            }
            return dict(self._latest)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._latest)

    def run(self, stop_event: threading.Event) -> None:
        while True:
            try:
                self.sample_once()
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                pass
            if stop_event.is_set() or stop_event.wait(self.interval_seconds):
                return
