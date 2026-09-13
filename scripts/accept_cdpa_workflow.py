"""Opt-in real-browser workflow acceptance with explicit test-owned fault injection.

Uses the production scheduler, store, send ledger, page actions and controller.
Only repository-project grouping (unrelated to pool/recovery) is disabled to avoid
creating account projects for disposable test workspaces. Endpoint loss disconnects
the test worker's CDP attachment, never terminates the operator's browser process.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from playwright_auto.cdpa_actions import CDPATabActions
from playwright_auto.cdpa_browser_projection import inspect_page_metadata
from playwright_auto.cdpa_config import DEFAULT_CONFIG_PATH, DEFAULTS_ROOT, load_cdpa_config
from playwright_auto.cdpa_endpoint_pool import EndpointPool
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.durable import RequestLedger, RequestStatus
from playwright_auto.file_lock import exclusive_file_lock
from playwright_auto.cdpa_worker import task_workflow_definitions

ADDRESSES = {"live-a": "http://127.0.0.1:9222", "live-b": "http://127.0.0.1:9202"}


def targets(address):
    with urlopen(address + "/json/list", timeout=5) as response:
        return {item["id"] for item in json.load(response) if item.get("id")}


def target_types(address):
    with urlopen(address + "/json/list", timeout=5) as response:
        return {item["id"]: item.get("type") for item in json.load(response) if item.get("id")}


def make_config(workspace):
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["paths"]["constructors"] = {key: str(DEFAULTS_ROOT / value) for key, value in raw["paths"]["constructors"].items()}
    for key in ("response_guide", "independent_rule"):
        raw["paths"][key] = str(DEFAULTS_ROOT / raw["paths"][key])
    raw["repositories"] = {"allowed_roots": [str(workspace)]}
    raw["independent_agents"]["seed_builtins"] = False
    raw["cleanup"]["terminal_idle_seconds"] = 86400
    raw["browser"]["workspace_timeout_seconds"] = 30
    raw["browser"]["endpoints"] = [{"endpoint_id": key, "cdp_url": address, "label": key,
        "max_active_tabs": 2, "is_default": key == "live-a", "rate_limit_scope": "test-" + key}
        for key, address in ADDRESSES.items()]
    path = workspace / "cdpa.yaml"
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return load_cdpa_config(path, repository_root=workspace)


async def run(args):
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if not args.resume and (workspace / "checkpoint.md").exists():
        raise ValueError("use a fresh acceptance workspace; existing work is never overwritten")
    config = load_cdpa_config(workspace / "cdpa.yaml", repository_root=workspace) if args.resume else make_config(workspace)
    filesystem_tool = getattr(args, "filesystem_tool", "mcp-thinkbook")
    tool_instruction = f"Use @{filesystem_tool} filesystem tools directly."
    if filesystem_tool == "Remote_Desktop_Commander":
        if not args.device_id:
            raise ValueError("Remote_Desktop_Commander acceptance requires --device-id")
        tool_instruction += f" Target deviceId {args.device_id}; do not use mcp-thinkbook."
    store = TaskStore(config)
    if args.resume:
        state = store.load_task_id(args.resume)
        if state is None:
            raise ValueError("resume task does not exist in this acceptance workspace")
        if state["status"] in {"BLOCKED", "PAUSED"}:
            state = store.request_resume(state["manifest_path"], reason="continue acceptance from durable state after integration fix")
    else:
        if (workspace / "checkpoint.md").exists():
            raise ValueError("use a new workspace or explicit --resume; never replay an existing test task")
        token = "CHECKPOINT_" + hashlib.sha256(str(workspace).encode()).hexdigest()[:20]
        (workspace / "checkpoint.md").write_text(token + "\n", encoding="utf-8", newline="\n")
        (workspace / "AGENTS.md").write_text(
            "Isolated live acceptance workspace. " + tool_instruction + " "
            "Do not submit another CDPA task. Read checkpoint.md and write only result.txt and exact role reports. "
            "No browser, account, process, repository-outside-this-folder, commit, or learning-file changes.\n", encoding="utf-8")
        (workspace / "LEARNING.md").write_text("# Learning\nThis bounded probe needs no new learning; disposition NONE.\n", encoding="utf-8")
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        pipeline = args.scenario == "pipeline"
        task_text = (
            f"{tool_instruction} This is a bounded acceptance workflow. Work ONLY in {workspace}. "
            "Read AGENTS.md and checkpoint.md from that exact directory; do not assume its contents. "
            "Never submit another CDPA task. Never modify another repository or any browser/account configuration. "
            "The deliverable is UTF-8 result.txt containing exactly the checkpoint marker and one LF newline, without BOM or CRLF. "
            "If result.txt already exists, read and verify it; do not overwrite, append, or repeat completed work. "
            "Use separate filesystem tool calls to read the checkpoint, create/inspect the result, verify its contents, "
            "then write your full report at the exact absolute destination supplied below. "
            "All reports must quote the observed checkpoint marker and actual filesystem evidence. "
            "Do not edit LEARNING.md; record Learning NONE. "
        )
        if pipeline:
            task_text += (
                "Required route sequence: initial PLAN reads checkpoint and writes its PLAN report, routes DEV. "
                "DEV creates/verifies result.txt, writes its report, routes TEST. TEST reads result.txt and DEV report, "
                "writes verification report, routes REVIEW. REVIEW reads these files and writes review report, routes PLAN. "
                "Final PLAN reads REVIEW report and result.txt, writes final report then routes DONE. "
                "No extra roles or self-routing are needed."
            )
        else:
            task_text += "PLAN performs this bounded file verification itself, writes its report, then routes DONE. No other roles are needed."
        scope = hashlib.sha256(str(workspace).encode()).hexdigest()[:10]
        state = store.create_task(task_text, requested_team="accept-" + args.scenario + "-" + scope,
            task_id="accept-" + workspace.name[:40] + "-" + scope, roles=["PLAN", "DEV", "TEST", "REVIEW"] if pipeline else ["PLAN"],
            context_policy="fresh", temporary=args.mode == "temporary", browser_endpoint_id=args.endpoint)
    task_id = state["task_id"]
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    worker._schedule_repository_project = lambda *_args, **_kwargs: None
    pool = EndpointPool(store.endpoints, retry_delay=2)
    blocked = set()
    connector = pool.connector
    async def test_partition(address):
        if address in blocked:
            raise ConnectionError("acceptance-injected endpoint partition")
        return await connector(address)
    pool.connector = test_partition
    evidence_path = workspace / "acceptance.json"
    previous = json.loads(evidence_path.read_text(encoding="utf-8")) if args.resume and evidence_path.exists() else {}
    evidence = {**previous, "workspace": str(workspace), "task_id": task_id, "mode": args.mode,
        "scenario": args.scenario, "events": previous.get("events", []), "run_started_at": datetime.now(timezone.utc).isoformat(),
        "source_hashes": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (ROOT / "src/playwright_auto").rglob("*.py")},
        "fault_injection": previous.get("fault_injection"), "project_grouping_disabled": True,
        "filesystem_tool": filesystem_tool,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checkpoint_sha256_before": hashlib.sha256((workspace / "checkpoint.md").read_bytes()).hexdigest()}
    if previous.get("error"):
        evidence.setdefault("previous_run_errors", []).append(previous["error"])
    evidence.pop("error", None)
    evidence["passed"] = False
    existing_result = workspace / "result.txt"
    evidence["result_sha256_before_run"] = hashlib.sha256(existing_result.read_bytes()).hexdigest() if existing_result.exists() else None
    before = {key: await asyncio.to_thread(targets, url) for key, url in ADDRESSES.items()}
    evidence["target_ids_before"] = {key: sorted(ids) for key, ids in before.items()}
    evidence["target_types_before"] = {key: await asyncio.to_thread(target_types, url) for key, url in ADDRESSES.items()}
    def save():
        evidence_path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    def event(kind, **values):
        item = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **values}
        evidence["events"].append(item)
        save()
        print(json.dumps(item, default=str), flush=True)
    fault_done = bool(evidence.get("fault_injection"))
    original_send = worker._sending
    async def partition(endpoint_id, current, hop, actions, reload_first=False):
        nonlocal fault_done
        acquired = await actions.locate_owned_metadata(current, str(hop["target_role"]))
        if acquired is not None:
            if reload_first:
                old_token = await worker._temporary_document_token(acquired.client, create=True)
                await acquired.client.page.reload(wait_until="domcontentloaded", timeout=30000)
                new_token = await worker._temporary_document_token(acquired.client)
                event("temporary_f5", document_changed=bool(old_token and old_token != new_token))
            else:
                await acquired.client.page.close()
        fault_done = True
        evidence["fault_injection"] = {"endpoint": endpoint_id, "request_id": hop["request_id"], "hop_id": hop["hop_id"],
            "hop_state": hop["state"], "fault_type": "F5 and attachment partition" if reload_first else "test-owned page loss and attachment partition"}
        save()
        blocked.add(ADDRESSES[endpoint_id])
        await pool.mark_unavailable(endpoint_id, "acceptance-injected endpoint partition")
        event("partition", **evidence["fault_injection"])
    async def send_with_fault(current, hop, actions):
        if args.scenario == "pre-send" and not fault_done:
            endpoint_id = current["roles"][hop["target_role"]]["browser_endpoint_id"]
            ledger = RequestLedger(hop["ledger_path"])
            constructor = task_workflow_definitions(current, config)[hop["target_role"]]["system_prompt"]
            record = ledger.begin(role=hop["physical_role"], prompt=hop["prompt"], request_id=hop["request_id"],
                source_context={"task_id": current["task_id"], "team": current["team"], "hop_id": hop["hop_id"], "manifest": current["manifest_path"]},
                role_prompt_hash=hashlib.sha256(str(constructor).encode()).hexdigest(), render_request_marker=False)
            if args.pre_send_state == "prompt_set":
                acquired = await actions.locate_owned(current, hop["target_role"])
                await acquired.client.set_text(hop["prompt"])
                record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
            event("pristine_ledger", status=record.status.value, attempts=record.attempts)
            await partition(endpoint_id, current, hop, actions)
            raise ConnectionError("browser has been closed: acceptance pre-send partition")
        await original_send(current, hop, actions)
    worker._sending = send_with_fault
    last = None
    started = time.monotonic()
    heartbeat = asyncio.create_task(worker._heartbeat_loop())
    try:
        await pool.refresh(wait=True)
        event("initial_pool", endpoints=pool.snapshot()["endpoints"])
        while time.monotonic() - started < args.timeout:
            await pool.refresh()
            await worker.run_once_pool(pool)
            state = store.load_task_id(task_id)
            hop = _active_hop(state) if state.get("active_hop_id") is not None else state["hops"][-1]
            key = (state["status"], hop["hop_id"], hop["state"], state.get("active_action"), state.get("block_code"))
            if key != last:
                event("state", state=key, endpoint=state["roles"][hop["target_role"]].get("browser_endpoint_id"), reason=state.get("block_reason"))
                last = key
            if args.scenario == "pipeline" and hop["target_role"] == "DEV" and hop["state"] == "pre_send" and not evidence.get("placement_cooldown"):
                # Simulate a scoped cooldown, without generating actual abusive traffic.
                source = state["roles"]["PLAN"].get("browser_endpoint_id")
                scope = store.endpoints.get(source)["rate_limit_scope"]
                worker._rate_limit_cooldowns[scope] = {"state": "active", "release_not_before": "2999-01-01T00:00:00+00:00"}
                evidence["placement_cooldown"] = source
                event("scoped_cooldown_injected", endpoint=source)
            if args.scenario == "pipeline" and hop["target_role"] == "TEST":
                worker._rate_limit_cooldowns.clear()
            if args.scenario in {"accepted", "f5"} and not fault_done and hop["state"] in {"sent", "waiting"}:
                # Cross Send deliberately. Existing checkpoint is authoritative regardless of partial prose.
                source = state["roles"][hop["target_role"]].get("browser_endpoint_id")
                await partition(source, state, hop, CDPATabActions(pool.contexts[source], config), reload_first=args.scenario == "f5")
            if state["status"] in {"DONE", "BLOCKED", "PAUSED", "STOPPED"}:
                break
            await asyncio.sleep(0.3)
        state = store.load_task_id(task_id)
        evidence["final_status"] = state["status"]
        evidence["reports"] = state.get("reports", [])
        evidence["roles"] = state["roles"]
        ledger = RequestLedger(state["hops"][0]["ledger_path"])
        evidence["requests"] = [{"hop_id": hop["hop_id"], "kind": hop["kind"], "request_id": hop["request_id"],
            "recovery_of_request_id": hop.get("recovery_of_request_id"), "endpoint": hop.get("browser_endpoint_id"),
            "attempts": record.attempts if (record := ledger.peek(hop["request_id"])) else 0}
            for hop in state["hops"]]
        evidence["result_matches_checkpoint"] = (workspace / "result.txt").is_file() and (
            (workspace / "result.txt").read_bytes() == (workspace / "checkpoint.md").read_bytes())
        assert state["status"] == "DONE", (state.get("block_code"), state.get("block_reason"))
        assert evidence["result_matches_checkpoint"], "result bytes differ from checkpoint"
        assert hashlib.sha256((workspace / "checkpoint.md").read_bytes()).hexdigest() == evidence["checkpoint_sha256_before"], "checkpoint changed"
        assert all(item["attempts"] == 1 for item in evidence["requests"]), "every executed request must have exactly one Send attempt"
        if args.scenario == "pipeline":
            assert state["roles"]["PLAN"]["browser_endpoint_id"] != state["roles"]["DEV"]["browser_endpoint_id"]
        if args.scenario in {"pre-send", "accepted", "f5"}:
            assert fault_done
            assert state["roles"]["PLAN"]["browser_endpoint_id"] != evidence["fault_injection"]["endpoint"]
            if args.scenario == "pre-send":
                assert state["hops"][0]["request_id"] == evidence["fault_injection"]["request_id"]
            else:
                assert any(hop.get("recovery_of_request_id") == evidence["fault_injection"]["request_id"] for hop in state["hops"])
        evidence["result_sha256_after_run"] = hashlib.sha256((workspace / "result.txt").read_bytes()).hexdigest()
        if evidence["result_sha256_before_run"]:
            assert evidence["result_sha256_before_run"] == evidence["result_sha256_after_run"], "recovery rewrote existing result bytes"
        evidence["passed"] = True
        evidence.pop("error", None)
    except Exception as exc:
        evidence["passed"] = False
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        blocked.clear()
        await pool.refresh(wait=True)
        for context in pool.contexts.values():
            for page in tuple(context.pages):
                try:
                    metadata = await inspect_page_metadata(page)
                    if metadata.get("task_id") == task_id:
                        if not evidence.get("passed"):
                            diagnostics = await page.evaluate('''() => ({url:location.href, dialogs:[...document.querySelectorAll('[role="dialog"],[data-testid^="modal-"]')].filter(e=>e.id!=='playwright-auto-role-control-v1').map(e=>({text:e.innerText, html:e.outerHTML.slice(0,20000)}))})''')
                            evidence.setdefault("failed_page_diagnostics", []).append(diagnostics)
                            await page.screenshot(path=str(workspace / "failed-page.png"))
                        await page.close()
                except Exception as exc:
                    evidence.setdefault("cleanup_errors", []).append(type(exc).__name__)
        await pool.close()
        worker.runtime_db.close()
        after = {key: await asyncio.to_thread(targets, url) for key, url in ADDRESSES.items()}
        evidence["target_types_cleanup_start"] = {key: await asyncio.to_thread(target_types, url) for key, url in ADDRESSES.items()}
        for _ in range(300):
            if before == after:
                break
            await asyncio.sleep(0.2)
            after = {key: await asyncio.to_thread(targets, url) for key, url in ADDRESSES.items()}
        evidence["target_ids_after"] = {key: sorted(ids) for key, ids in after.items()}
        evidence["target_types_after"] = {key: await asyncio.to_thread(target_types, url) for key, url in ADDRESSES.items()}
        evidence["target_delta"] = {key: {"added": sorted(after[key] - before[key]), "removed": sorted(before[key] - after[key])} for key in before}
        evidence["original_targets_preserved"] = {key: before[key].issubset(after[key]) for key in before}
        evidence["target_sets_unchanged"] = {key: before[key] == after[key] for key in before}
        cleanup_safe = all(evidence["target_sets_unchanged"].values()) and not evidence.get("cleanup_errors")
        if not cleanup_safe:
            evidence["passed"] = False
            evidence["error"] = evidence.get("error") or "acceptance cleanup did not preserve exact browser target sets"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        print(json.dumps({"passed": evidence.get("passed"), "status": evidence.get("final_status"), "evidence": str(evidence_path), "error": evidence.get("error")}), flush=True)
        if not cleanup_safe:
            raise AssertionError(evidence["error"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--mode", choices=["persistent", "temporary"], default="persistent")
    parser.add_argument("--scenario", choices=["smoke", "pipeline", "pre-send", "accepted", "f5"], default="smoke")
    parser.add_argument("--endpoint", choices=list(ADDRESSES), default="live-a")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--resume")
    parser.add_argument("--filesystem-tool", choices=["mcp-thinkbook", "Remote_Desktop_Commander"], default="mcp-thinkbook")
    parser.add_argument("--pre-send-state", choices=["new", "prompt_set"], default="new")
    parser.add_argument("--device-id")
    args = parser.parse_args()
    with exclusive_file_lock(args.workspace.resolve() / ".acceptance.lock", blocking=False):
        asyncio.run(run(args))
