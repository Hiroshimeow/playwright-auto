from __future__ import annotations

import os
import threading
from pathlib import Path

from playwright_auto.cdpa_telemetry import TelemetrySampler


def write_proc(root: Path, *, idle: int, total_extra: int, rx: int = 10, tx: int = 20):
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text(f"cpu  {total_extra} 0 0 {idle} 0 0 0 0 0 0\n", encoding="utf-8")
    (root / "meminfo").write_text(
        "MemTotal:       1000 kB\nMemAvailable:    400 kB\n", encoding="utf-8"
    )
    (root / "loadavg").write_text("1.00 2.00 3.00 1/100 1\n", encoding="utf-8")
    (root / "net" ).mkdir(exist_ok=True)
    (root / "net" / "dev").write_text(
        "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        f"  lo: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n",
        encoding="utf-8",
    )


def test_sampler_reads_proc_and_calculates_cpu_between_samples(tmp_path: Path):
    proc = tmp_path / "proc"
    write_proc(proc, idle=70, total_extra=30)
    sampler = TelemetrySampler(proc_root=proc, disk_path=tmp_path, api_pid=os.getpid())

    first = sampler.sample_once()
    write_proc(proc, idle=80, total_extra=50, rx=30, tx=40)
    second = sampler.sample_once()

    assert first["host"]["cpu_percent"] == 0.0
    assert second["host"]["cpu_percent"] == 66.67
    assert second["host"]["memory_total_bytes"] == 1000 * 1024
    assert second["host"]["memory_used_bytes"] == 600 * 1024
    assert second["host"]["load_1"] == 1.0
    assert second["host"]["load_5"] == 2.0
    assert second["host"]["load_15"] == 3.0
    assert second["host"]["network_rx_bytes"] == 30
    assert second["host"]["network_tx_bytes"] == 40
    assert second["host"]["disk_total_bytes"] > 0
    assert second["gpu"] is None


def test_sampler_missing_worker_and_gpu_are_nonfatal(tmp_path: Path):
    proc = tmp_path / "proc"
    write_proc(proc, idle=1, total_extra=1)
    sampler = TelemetrySampler(
        proc_root=proc,
        disk_path=tmp_path,
        api_pid=999999999,
        worker_pid_provider=lambda: None,
    )

    snapshot = sampler.sample_once()

    assert snapshot["processes"]["api"] is None
    assert snapshot["processes"]["worker"] is None
    assert snapshot["gpu"] is None


def test_sampler_run_publishes_latest_snapshot(tmp_path: Path):
    proc = tmp_path / "proc"
    write_proc(proc, idle=1, total_extra=1)
    stop = threading.Event()
    sampler = TelemetrySampler(proc_root=proc, disk_path=tmp_path, interval_seconds=0.01)
    stop.set()

    sampler.run(stop)

    assert sampler.snapshot()["sample_version"] == 1
