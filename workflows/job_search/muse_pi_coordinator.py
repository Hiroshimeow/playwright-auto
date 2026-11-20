#!/usr/bin/env python3
"""Pi Coding CLI adapter: model routing, durable run receipt and status for Muse via MD.

One CLI invocation has its own process, no browser profile dependency.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PI = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
JOBS = ROOT / ".runtime" / "job-search" / "pi-jobs"
PROVIDER = "cliproxy-2080"
MODELS = {"engineering": "gpt-6.2-sol", "monitoring": "gpt-5.6-luna",
          "review": "gpt-6.2-sol", "planning": "gpt-6.2-sol"}
THINKING = {"engineering": "high", "monitoring": "medium",
            "review": "high", "planning": "high"}


def now():
    return datetime.now(timezone.utc).isoformat()


def job_paths(job_id):
    if not job_id or not all(c.isalnum() or c in "-_" for c in job_id):
        raise ValueError("invalid id")
    return JOBS / (job_id + ".json"), JOBS / (job_id + ".out.log"), JOBS / (job_id + ".err.log")


def read_job(job_id):
    path, stdout, stderr = job_paths(job_id)
    job = json.loads(path.read_text(encoding="utf-8"))
    if job.get("state") == "STARTED":
        pid = job.get("pid")
        import ctypes
        import ctypes.wintypes as wt
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        alive = False
        if handle:
            exit_code = wt.DWORD()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                alive = exit_code.value == 259
            ctypes.windll.kernel32.CloseHandle(handle)
        job["runtime_alive"] = alive
        if not alive:
            job["state"] = "EXITED_REVIEW_OUTPUT"
    job["stdout_tail"] = stdout.read_text(encoding="utf-8", errors="replace")[-1600:] if stdout.exists() else ""
    job["stderr_tail"] = stderr.read_text(encoding="utf-8", errors="replace")[-800:] if stderr.exists() else ""
    return job


def command(kind, prompt):
    if kind not in MODELS:
        raise ValueError("unsupported work kind")
    if not PI.exists():
        raise FileNotFoundError(str(PI))
    if not (20 <= len(prompt) <= 16000):
        raise ValueError("provide bounded task prompt 20..16000 chars")
    return ["node.exe", str(PI), "-p", "--model",
            PROVIDER + "/" + MODELS[kind], "--thinking", THINKING[kind],
            "--offline", "--no-session", "--no-approve",
            "--no-context-files", "--no-prompt-templates",
            "--no-skills", "--no-mcp", "--no-extensions",
            "--tools", "read,bash", prompt]


def run(kind, prompt, dry_run=False):
    cmd = command(kind, prompt)
    if dry_run:
        return {"state": "READY", "provider": PROVIDER, "model": MODELS[kind],
                "thinking": THINKING[kind], "prompt_chars": len(prompt),
                "cli": str(PI), "mode": "dry-run"}
    JOBS.mkdir(parents=True, exist_ok=True)
    job_id = "pi-" + uuid.uuid4().hex[:12]
    path, out, err = job_paths(job_id)
    record = {"id": job_id, "state": "INTENT_PERSISTED", "kind": kind,
              "model": PROVIDER + "/" + MODELS[kind],
              "thinking": THINKING[kind], "created_at": now(),
              "pid": None, "prompt_sha256": __import__("hashlib").sha256(prompt.encode()).hexdigest()}
    with path.open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    with out.open("w", encoding="utf-8") as output, err.open("w", encoding="utf-8") as error:
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=error, creationflags=flags)
        except Exception as exc:
            record["state"] = "START_FAILED"
            record["error"] = str(exc)[:250]
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return record
    record["pid"] = proc.pid
    record["state"] = "STARTED"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    a = sub.add_parser("route")
    a.add_argument("--kind", choices=sorted(MODELS), required=True)
    b = sub.add_parser("run")
    b.add_argument("--kind", choices=sorted(MODELS), required=True)
    b.add_argument("--prompt", required=True)
    b.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("status")
    c.add_argument("--id", required=True)
    args = parser.parse_args()
    try:
        if args.action == "route":
            result = {"kind": args.kind, "provider": PROVIDER,
                      "model": MODELS[args.kind], "thinking": THINKING[args.kind]}
        elif args.action == "status":
            result = read_job(args.id)
        else:
            result = run(args.kind, args.prompt, args.dry_run)
        print(json.dumps(result, ensure_ascii=True))  # cp1252 console: keep JSON ASCII-safe
        return 0
    except Exception as exc:
        print(json.dumps({"state": "ERROR", "reason": str(exc)[:350]}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
