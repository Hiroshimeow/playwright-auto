from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import queue
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_store as store_module
import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import (
    AcquiredRole,
    BranchBootstrapError,
    RoleOwnershipError,
    TeamCloseError,
)
from playwright_auto.cdpa_bootstraps import BootstrapCatalog
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_independent import (
    canonical_independent_events,
    validate_trigger_settings,
)
from playwright_auto.cdpa_routes import RouteContractError
from playwright_auto.cdpa_store import TaskStore, utc_now
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop, _report_mode
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    PageOwnershipError,
    RateLimitBlockedError,
    SendReceipt,
    StableMalformedResponseError,
    UnsafePageStateError,
    capture_message_baseline,
    capture_response_recovery_baseline,
    response_activity_signature,
    prompt_digest,
)
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_core import (
    install_cycle_isolation_graph,
    install_duplicate_task_graph,
    poison_catalog_identity_entry,
    write_config,
)


def _run_worker_lease_probe(repository: str, entered) -> None:
    import asyncio as _asyncio
    from pathlib import Path as _Path
    from types import SimpleNamespace as _SimpleNamespace

    import playwright_auto.cdpa_worker as _worker_module

    class _RuntimeDB:
        def ensure_schema(self) -> None:
            pass

        def requeue_running_commands(self) -> None:
            pass

        def close(self) -> None:
            pass

    class _Worker:
        def __init__(self, config) -> None:
            self.store = _SimpleNamespace(
                endpoints={}, recover_phase4_replacement=lambda: None
            )
            self.runtime_db = _RuntimeDB()
            self._browser_command_tasks = set()
            self._worker_lease_acquired_at = None

        def _restore_rate_limit_cooldown(self) -> None:
            pass

        def _publish_heartbeat(self, **_kwargs) -> None:
            pass

        def hydrate_runtime(self, *, startup=False) -> None:
            entered.put(os.getpid())

        async def run_command_loop(self) -> None:
            await _asyncio.Event().wait()

        async def _heartbeat_loop(self) -> None:
            await _asyncio.Event().wait()

        async def run_pool_forever(self, _pool) -> None:
            await _asyncio.Event().wait()

    class _EndpointPool:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def close(self) -> None:
            pass

    _worker_module.CDPAWorker = _Worker
    _worker_module.EndpointPool = _EndpointPool
    config = _SimpleNamespace(repository_root=_Path(repository), worker_poll_seconds=0.1)
    _asyncio.run(_worker_module._run(config))


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _linked_worktree_fixture(root: Path) -> tuple[Path, Path, str]:
    control = root / "control"
    target = root / "task-worktree"
    control.mkdir()
    _git(control, "init")
    _git(control, "config", "user.email", "cdpa-tests@example.invalid")
    _git(control, "config", "user.name", "CDPA Tests")
    (control / ".gitignore").write_text(".plan/\n.runtime/\n", encoding="utf-8")
    (control / "base.txt").write_text("base\n", encoding="utf-8")
    _git(control, "add", ".gitignore", "base.txt")
    _git(control, "commit", "-m", "base")
    head = _git(control, "rev-parse", "HEAD")
    _git(control, "worktree", "add", "--detach", str(target), head)
    return control, target, head


def canonical_recovery_events(state: dict) -> list[dict]:
    agent = {
        "task_mode": "independent",
        "task_id": "agent-test-maintainers-g1",
        "team": "agent-test-maintainers",
        "status": "WAITING",
        "independent": {
            "agent_name": "Test Maintainers",
            "agent_key": "test maintainers",
            "enabled": True,
            "trigger_settings": validate_trigger_settings({"recovery": True}),
            "active_event": None,
            "watermarks": {"seen_event_keys": []},
            "occurrence_counts": {},
        },
    }
    return [
        event
        for event in canonical_independent_events(agent, [state, agent])
        if event["trigger_type"] == "recovery"
    ]


def ready_client(state, role):
    record = state["roles"][role]
    current = ChatGPTSnapshot(
        url="https://chatgpt.com/", session_id=None,
        page_id=f"page-{record['physical_role']}", page_role=record['physical_role'],
        page_task_id=state['task_id'], page_team=state['team'],
        state=ChatGPTState.NEW_CHAT, requires_login=False, composer_present=True,
        composer_editable=True, composer_text="", send_visible=True, send_enabled=True,
        stop_visible=False, blocking_dialogs=(), attachment_markers=(), error_texts=(), messages=(),
    )
    return SimpleNamespace(assert_ownership=AsyncMock(return_value=current))


class FakeActions:
    def __init__(self):
        self.restart_roles = []
        self.located_roles = []
        self.closed_teams = 0
        self.preflight_calls = 0
        self.stop_calls = 0
        self.pages = ["assigned-page"]
        self.fresh_calls = []

    async def acquire(self, state, role, **_kwargs):
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=f"page-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=record.get("page_id") is None,
            new_chat=False,
        )

    async def restart(self, state, role, *, known_automated_draft=None):
        self.restart_roles.append(role)
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=f"restarted-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=True,
            new_chat=True,
        )

    async def locate_owned(self, state, role):
        self.located_roles.append(role)
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=f"page-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=False,
            new_chat=False,
        )

    async def stop_if_active(self, acquired):
        self.stop_calls += 1
        return True

    async def new_chat(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=f"new-chat-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=False,
            new_chat=True,
        )

    async def fresh_chat(self, state, role, *, url, clean_for_send=False):
        record = state["roles"][role]
        self.fresh_calls.append((role, url, clean_for_send))
        return AcquiredRole(
            client=ready_client(state, role),
            page_id=f"fresh-{record['physical_role']}",
            url=url,
            created=True,
            new_chat=True,
        )

    async def preflight_team(self, _state):
        self.preflight_calls += 1
        return list(self.pages)

    async def close_team(self, _state, *, preflighted_pages=None):
        selected = list(preflighted_pages or [])
        assert all(page in self.pages for page in selected)
        self.closed_teams += 1
        for page in selected:
            self.pages.remove(page)
        return len(selected)


def test_blocked_temporary_workflow_preserves_live_tab(tmp_path: Path):
    _config, _store, state, worker = setup_task(tmp_path, task_id="task-temp-block")
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["conversation_mode"] = "temporary"
    actions = FakeActions()

    asyncio.run(worker._release_blocked_workflow_tabs(state, actions))

    assert actions.preflight_calls == 0
    assert actions.closed_teams == 0
    assert actions.pages == ["assigned-page"]


def setup_task(
    tmp_path: Path,
    *,
    task_id="task-a",
    report_mode="file",
    roles=None,
    repository=None,
    bootstrap=None,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id=task_id,
        report_mode=report_mode,
        roles=roles,
        repository=repository,
        bootstrap=bootstrap,
        governing_objective="Implement exact production behavior",
        governing_directives=[],
    )
    return config, store, state, CDPAWorker(config, store=store)


def _persist_removed_endpoint_cleanup(store, state):
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["active_role"] = None
    state["active_hop_id"] = None
    role = state["roles"]["PLAN"]
    role["browser_endpoint_id"] = "legacy"
    role["page_id"] = "legacy-page"
    role["page_url"] = "https://chatgpt.com/c/legacy"
    role["online"] = False
    state["cleanup"] = {
        "state": "CLEARING",
        "phase": "close_pending",
        "retry_count": 0,
        "last_error": None,
        "last_error_at": None,
    }
    return store.save(path, state)


def test_worker_scheduler_lease_serializes_spawned_worker_lifetimes_and_crash_handoffs(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    entered = context.Queue()
    processes = []

    def start_probe():
        process = context.Process(target=_run_worker_lease_probe, args=(str(tmp_path), entered))
        process.start()
        processes.append(process)
        return process

    first = start_probe()
    try:
        assert entered.get(timeout=10) == first.pid
        second = start_probe()
        with pytest.raises(queue.Empty):
            entered.get(timeout=1)

        first.terminate()
        first.join(timeout=10)
        assert not first.is_alive()
        assert entered.get(timeout=10) == second.pid

        third = start_probe()
        with pytest.raises(queue.Empty):
            entered.get(timeout=1)

        second.terminate()
        second.join(timeout=10)
        assert not second.is_alive()
        assert entered.get(timeout=10) == third.pid
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=10)


def test_presend_block_closes_team_tabs_and_preserves_block_truth(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "required.txt"
    attachment.write_text("required context", encoding="utf-8")
    state = store.create_task(
        "Requires attachment",
        requested_team="blocked-release",
        task_id="task-presend-block-release",
        upload_paths=[attachment],
        governing_objective="Requires attachment",
        governing_directives=(),
    )
    path = Path(state["manifest_path"])
    attachment.unlink()
    worker = CDPAWorker(config, store=store)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    saved = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert saved is not None
    assert saved["status"] == "BLOCKED"
    assert saved["block_code"] == "attachment_missing"
    assert "required.txt" in saved["block_reason"]
    assert actions.closed_teams == 1
    assert actions.pages == []
    reloaded = store.load(path)
    assert reloaded["status"] == "BLOCKED"
    assert reloaded["block_code"] == "attachment_missing"
    assert reloaded["block_reason"] == saved["block_reason"]


def test_blocked_ambiguous_sending_preserves_team_tabs(tmp_path: Path, monkeypatch):
    config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-blocked-sending-preserves-tabs",
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["prompt"] = "durable prompt"
    hop["prompt_sha256"] = worker_module._sha(hop["prompt"])
    hop["state"] = "sending"
    hop["timestamps"]["sending_at"] = utc_now()
    ledger = RequestLedger(hop["ledger_path"])
    ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": state["manifest_path"],
        },
        role_prompt_hash="a" * 64,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(hop["request_id"], status=RequestStatus.PROMPT_SET)
    ledger.update(hop["request_id"], status=RequestStatus.SENDING, attempts=1)
    state = store.save(path, state)
    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    async def force_ambiguous_block(current, current_hop, _actions):
        assert current_hop["state"] == "sending"
        worker._block(
            current,
            "send acceptance remains ambiguous",
            code="send_acceptance_ambiguous",
            retryable=False,
        )

    monkeypatch.setattr(worker, "_sending", force_ambiguous_block)
    saved = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert saved is not None
    assert saved["status"] == "BLOCKED"
    assert saved["block_code"] == "send_acceptance_ambiguous"
    assert actions.closed_teams == 0
    assert actions.pages == ["assigned-page"]
    durable = ledger.get(hop["request_id"])
    assert durable is not None
    assert durable.status is RequestStatus.SENDING
    assert durable.attempts == 1


def test_archive_worktree_control_preserves_evidence_and_head_before_removal(tmp_path: Path):
    control, target, head = _linked_worktree_fixture(tmp_path)
    config, store, state, worker = setup_task(
        control,
        task_id="task-worktree-archive",
        repository=str(target),
    )
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["kanban_column"] = "DONE"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["cleanup"] = {
        "state": "CLEARED",
        "verified_empty_at": utc_now(),
        "closed_tabs": 0,
    }
    state = store.save(path, state)
    evidence = target / ".plan" / state["team"] / "archive-proof.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("durable archive evidence\n", encoding="utf-8")

    state = store.request_control(
        path,
        "archive_worktree",
        reason="operator archival after verified cleanup",
        confirmed=True,
    )
    persisted = []
    assert asyncio.run(
        worker._apply_control(state, FakeActions(), path, persisted_result=persisted)
    ) is True

    saved = store.load(path)
    archive = saved["worktree_archive"]
    assert archive["state"] == "ARCHIVED"
    assert archive["head"] == head
    assert archive["preservation_ref"].startswith("refs/cdpa/worktree-archive/")
    assert archive["evidence_root"] == f".plan/{state['team']}"
    assert _git(control, "rev-parse", archive["preservation_ref"]) == head
    assert not target.exists()
    archived_evidence = control / ".plan" / state["team"] / "archive-proof.md"
    assert archived_evidence.read_text(encoding="utf-8") == "durable archive evidence\n"
    assert str(target) not in _git(control, "worktree", "list", "--porcelain")
    assert persisted == [True]


def test_archive_worktree_control_rejects_dirty_worktree_without_preservation_mutation(tmp_path: Path):
    control, target, _head = _linked_worktree_fixture(tmp_path)
    _config, store, state, worker = setup_task(
        control,
        task_id="task-worktree-dirty",
        repository=str(target),
    )
    path = Path(state["manifest_path"])
    state.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None)
    state["cleanup"] = {"state": "CLEARED", "verified_empty_at": utc_now()}
    state = store.save(path, state)
    (target / "base.txt").write_text("dirty\n", encoding="utf-8")
    state = store.request_control(path, "archive_worktree", confirmed=True)

    assert asyncio.run(worker._apply_control(state, FakeActions(), path)) is True

    saved = store.load(path)
    assert saved["controls"][-1]["status"] == "rejected"
    assert "modified or untracked" in str(saved["controls"][-1]["result"]).lower()
    assert "worktree_archive" not in saved
    assert target.exists()
    assert "refs/cdpa/worktree-archive/" not in _git(control, "show-ref")


def test_archive_worktree_control_rejects_repository_with_nonterminal_owner(tmp_path: Path):
    control, target, _head = _linked_worktree_fixture(tmp_path)
    _config, store, state, worker = setup_task(
        control,
        task_id="task-worktree-terminal",
        repository=str(target),
    )
    path = Path(state["manifest_path"])
    state.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None)
    state["cleanup"] = {"state": "CLEARED", "verified_empty_at": utc_now()}
    state = store.save(path, state)
    owner = store.create_task(
        "Still owns this repository",
        requested_team="active-owner",
        task_id="task-worktree-owner",
        repository=str(target),
        governing_objective="Still owns this repository",
        governing_directives=(),
    )
    assert owner["status"] not in {"DONE", "STOPPED"}
    state = store.request_control(path, "archive_worktree", confirmed=True)

    assert asyncio.run(worker._apply_control(state, FakeActions(), path)) is True

    saved = store.load(path)
    assert saved["controls"][-1]["status"] == "rejected"
    assert "nonterminal" in str(saved["controls"][-1]["result"]).lower()
    assert target.exists()
    assert "worktree_archive" not in saved


def test_archive_worktree_control_serializes_final_owner_proof_and_removal_with_task_creation(
    tmp_path: Path, monkeypatch
):
    control, target, _head = _linked_worktree_fixture(tmp_path)
    _config, store, state, worker = setup_task(
        control,
        task_id="task-worktree-owner-race",
        repository=str(target),
    )
    path = Path(state["manifest_path"])
    state.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None)
    state["cleanup"] = {"state": "CLEARED", "verified_empty_at": utc_now()}
    state = store.save(path, state)
    state = store.request_control(path, "archive_worktree", confirmed=True)

    real_git_command = worker._git_command
    owner_started = threading.Event()
    owner_finished = threading.Event()
    ordering: list[str] = []
    failures: list[BaseException] = []
    threads: list[threading.Thread] = []

    def create_competing_owner() -> None:
        owner_started.set()
        try:
            store.create_task(
                "Concurrent owner",
                requested_team="concurrent-owner",
                task_id="task-worktree-concurrent-owner",
                repository=str(target),
                governing_objective="Concurrent owner",
                governing_directives=(),
            )
            ordering.append("owner-created")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            owner_finished.set()

    def interleaved_git_command(repository: Path, *args: str, **kwargs):
        if tuple(args[:2]) == ("worktree", "remove"):
            thread = threading.Thread(target=create_competing_owner)
            threads.append(thread)
            thread.start()
            assert owner_started.wait(timeout=2)
            owner_finished.wait(timeout=0.5)
            ordering.append("remove-started")
        return real_git_command(repository, *args, **kwargs)

    monkeypatch.setattr(worker, "_git_command", interleaved_git_command)
    assert asyncio.run(worker._apply_control(state, FakeActions(), path)) is True

    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert failures == []
    assert ordering.index("remove-started") < ordering.index("owner-created")


def test_archive_worktree_control_rejects_clean_head_drift_after_preparation(tmp_path: Path):
    control, target, head = _linked_worktree_fixture(tmp_path)
    _config, store, state, worker = setup_task(
        control,
        task_id="task-worktree-head-drift",
        repository=str(target),
    )
    path = Path(state["manifest_path"])
    state.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None)
    state["cleanup"] = {"state": "CLEARED", "verified_empty_at": utc_now()}
    state = store.save(path, state)
    state = store.request_control(path, "archive_worktree", confirmed=True)
    prepared_archive = worker._prepare_worktree_archive(state)
    store.update(path, lambda current: {**current, "worktree_archive": prepared_archive})

    (target / "base.txt").write_text("new committed state\n", encoding="utf-8")
    _git(target, "add", "base.txt")
    _git(target, "commit", "-m", "post-prepare head drift")
    changed_head = _git(target, "rev-parse", "HEAD")
    assert changed_head != head
    assert _git(target, "status", "--porcelain") == ""

    reloaded = store.load(path)
    assert asyncio.run(worker._apply_control(reloaded, FakeActions(), path)) is True

    rejected = store.load(path)
    assert rejected["controls"][-1]["status"] == "rejected"
    assert "head" in str(rejected["controls"][-1]["result"]).lower()
    assert target.exists()
    assert rejected["worktree_archive"]["state"] == "PREPARED"
    assert _git(control, "rev-parse", prepared_archive["preservation_ref"]) == head


def test_archive_worktree_control_finishes_prepared_removal_after_restart_and_repeats_idempotently(tmp_path: Path):
    control, target, head = _linked_worktree_fixture(tmp_path)
    _config, store, state, worker = setup_task(
        control,
        task_id="task-worktree-restart",
        repository=str(target),
    )
    path = Path(state["manifest_path"])
    state.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None)
    state["cleanup"] = {"state": "CLEARED", "verified_empty_at": utc_now()}
    evidence = target / ".plan" / state["team"] / "restart-proof.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("restart evidence\n", encoding="utf-8")
    state = store.save(path, state)
    state = store.request_control(path, "archive_worktree", confirmed=True)
    prepared_archive = worker._prepare_worktree_archive(state)
    assert prepared_archive["state"] == "PREPARED"
    preservation_ref = prepared_archive["preservation_ref"]
    assert _git(control, "rev-parse", preservation_ref) == head
    assert (control / ".plan" / state["team"] / "restart-proof.md").is_file()
    prepared = store.update(
        path,
        lambda current: {**current, "worktree_archive": prepared_archive},
    )
    assert prepared["controls"][-1]["status"] == "requested"
    _git(control, "worktree", "remove", str(target))
    assert not target.exists()

    reloaded = store.load(path)
    assert reloaded["controls"][-1]["status"] == "requested"
    assert asyncio.run(worker._apply_control(reloaded, FakeActions(), path)) is True
    completed = store.load(path)
    assert completed["worktree_archive"]["state"] == "ARCHIVED"
    assert completed["controls"][-1]["status"] == "applied"

    completed = store.request_control(
        path,
        "archive_worktree",
        confirmed=True,
        external_command_id="cmd-archive-repeat",
    )
    assert asyncio.run(worker._apply_control(completed, FakeActions(), path)) is True
    repeated = store.load(path)
    assert repeated["controls"][-1]["status"] == "applied"
    assert repeated["controls"][-1]["result"]["idempotent"] is True
    assert _git(control, "rev-parse", preservation_ref) == head


def test_identical_cleanup_failure_is_gated_between_actual_attempts(tmp_path: Path):
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-cleanup-retry-gate",
    )
    state = _persist_removed_endpoint_cleanup(store, state)
    worker._endpoint_pool = SimpleNamespace(contexts={})
    actions = FakeActions()

    assert asyncio.run(
        worker._continue_cleanup(state, actions, Path(state["manifest_path"]))
    ) is False
    first = store.load(state["manifest_path"])
    for _ in range(1000):
        current = store.load(state["manifest_path"])
        assert asyncio.run(
            worker._continue_cleanup(current, actions, Path(current["manifest_path"]))
        ) is False

    saved = store.load(state["manifest_path"])
    assert saved["cleanup"]["retry_count"] == 1
    assert saved["cleanup"]["same_failure_count"] == 1
    assert saved["cleanup"]["failure_fingerprint"]
    assert saved["cleanup"]["next_retry_at"]
    assert saved["cleanup"]["retry_suppressed"] is False
    assert saved["cleanup"]["last_error"] == (
        "RoleOwnershipError: cleanup cannot verify unavailable browser legacy"
    )
    assert saved["cleanup"]["phase"] == "close_pending"
    assert saved["roles"]["PLAN"]["page_id"] == "legacy-page"
    assert first["cleanup"]["retry_count"] == saved["cleanup"]["retry_count"]
    assert actions.closed_teams == 0


def test_cleanup_identical_failure_caps_and_changed_evidence_resets_streak(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-cleanup-retry-cap",
    )
    state = _persist_removed_endpoint_cleanup(store, state)
    worker._endpoint_pool = SimpleNamespace(contexts={})
    actions = FakeActions()
    clock = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(worker_module, "utc_now", lambda: clock.isoformat())
    for attempt in range(worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT):
        current = store.load(state["manifest_path"])
        if attempt:
            current["cleanup"]["next_retry_at"] = clock.isoformat()
            current = store.save(current["manifest_path"], current)
        asyncio.run(worker._continue_cleanup(current, actions, Path(current["manifest_path"])))
        clock += timedelta(hours=1)

    capped = store.load(state["manifest_path"])
    assert capped["cleanup"]["state"] == "CLEARING"
    assert capped["cleanup"]["phase"] == "close_pending"
    assert capped["cleanup"]["same_failure_count"] == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT
    assert capped["cleanup"]["retry_suppressed"] is True
    assert capped["cleanup"]["next_retry_at"] is None
    assert capped["roles"]["PLAN"]["page_id"] == "legacy-page"
    assert actions.closed_teams == 0

    capped["roles"]["PLAN"]["browser_endpoint_id"] = "legacy-2"
    capped["cleanup"]["retry_suppressed"] = False
    capped["cleanup"]["next_retry_at"] = clock.isoformat()
    capped = store.save(capped["manifest_path"], capped)
    asyncio.run(worker._continue_cleanup(capped, actions, Path(capped["manifest_path"])))

    changed = store.load(state["manifest_path"])
    assert changed["cleanup"]["same_failure_count"] == 1
    assert changed["cleanup"]["retry_suppressed"] is False
    assert "legacy-2" in changed["cleanup"]["last_error"]


def test_zero_progress_close_verification_failure_reaches_retry_cap(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-cleanup-zero-progress-cap",
    )
    state = _persist_removed_endpoint_cleanup(store, state)
    path = Path(state["manifest_path"])
    clock = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)

    class ZeroProgressActions(FakeActions):
        async def close_team(self, _state, *, preflighted_pages=None):
            selected = list(preflighted_pages or [])
            assert selected == self.pages
            self.closed_teams += 1
            return 0

    actions = ZeroProgressActions()
    monkeypatch.setattr(worker_module, "utc_now", lambda: clock.isoformat())

    for _ in range(worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT):
        current = store.load(path)
        assert asyncio.run(worker._continue_cleanup(current, actions, path)) is False
        clock += timedelta(hours=1)

    capped = store.load(path)
    assert capped["cleanup"]["state"] == "CLEARING"
    assert capped["cleanup"]["phase"] == "close_pending"
    assert capped["cleanup"]["retry_count"] == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT
    assert capped["cleanup"]["same_failure_count"] == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT
    assert capped["cleanup"]["retry_suppressed"] is True
    assert capped["cleanup"]["next_retry_at"] is None
    assert capped["roles"]["PLAN"]["page_id"] == "legacy-page"
    assert actions.closed_teams == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT

    for _ in range(1000):
        current = store.load(path)
        assert asyncio.run(worker._continue_cleanup(current, actions, path)) is False

    quiescent = store.load(path)
    assert quiescent["cleanup"]["retry_count"] == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT
    assert actions.closed_teams == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT


def test_partial_close_progress_resets_failure_streak_until_progress_stops(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-cleanup-partial-progress",
    )
    state = _persist_removed_endpoint_cleanup(store, state)
    path = Path(state["manifest_path"])
    clock = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)

    class PartialProgressActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.pages = [
                f"assigned-page-{index}"
                for index in range(worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT + 2)
            ]
            self.make_progress = True

        async def close_team(self, _state, *, preflighted_pages=None):
            selected = list(preflighted_pages or [])
            assert selected == self.pages
            self.closed_teams += 1
            if self.make_progress:
                self.pages.pop(0)
                raise TeamCloseError(
                    "assigned team tab did not report closed after page.close()",
                    closed_tabs=1,
                )
            raise TeamCloseError(
                "assigned team tab did not report closed after page.close()",
                closed_tabs=0,
            )

    actions = PartialProgressActions()
    monkeypatch.setattr(worker_module, "utc_now", lambda: clock.isoformat())

    for attempt in range(worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT):
        current = store.load(path)
        assert asyncio.run(worker._continue_cleanup(current, actions, path)) is False
        saved = store.load(path)
        assert saved["cleanup"]["closed_tabs"] == attempt + 1
        assert saved["cleanup"]["same_failure_count"] == 1
        assert saved["cleanup"]["retry_suppressed"] is False
        clock += timedelta(hours=1)

    actions.make_progress = False
    for _ in range(worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT - 1):
        current = store.load(path)
        assert asyncio.run(worker._continue_cleanup(current, actions, path)) is False
        clock += timedelta(hours=1)

    capped = store.load(path)
    assert capped["cleanup"]["state"] == "CLEARING"
    assert capped["cleanup"]["phase"] == "close_pending"
    assert capped["cleanup"]["closed_tabs"] == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT
    assert capped["cleanup"]["same_failure_count"] == worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT
    assert capped["cleanup"]["retry_suppressed"] is True
    assert capped["cleanup"]["next_retry_at"] is None
    assert len(actions.pages) == 2
    attempts_at_cap = actions.closed_teams

    for _ in range(1000):
        current = store.load(path)
        assert asyncio.run(worker._continue_cleanup(current, actions, path)) is False

    quiescent = store.load(path)
    assert quiescent["cleanup"]["retry_count"] == attempts_at_cap
    assert actions.closed_teams == attempts_at_cap


def test_normal_cleanup_still_requires_close_then_zero_match_verification(tmp_path: Path):
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-cleanup-normal",
    )
    state = _persist_removed_endpoint_cleanup(store, state)
    path = Path(state["manifest_path"])
    actions = FakeActions()

    assert asyncio.run(worker._continue_cleanup(state, actions, path)) is True

    saved = store.load(path)
    assert actions.closed_teams == 1
    assert actions.preflight_calls == 2
    assert saved["cleanup"]["state"] == "CLEARED"
    assert saved["cleanup"]["phase"] == "cleared"
    assert saved["cleanup"]["verified_empty_at"]
    assert saved["cleanup"]["retry_suppressed"] is False
    assert saved["cleanup"]["failure_fingerprint"] is None


def test_explicit_clear_team_retries_suppressed_cleanup_once(tmp_path: Path):
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-cleanup-explicit-retry",
    )
    state = _persist_removed_endpoint_cleanup(store, state)
    path = Path(state["manifest_path"])
    state["cleanup"].update(
        failure_fingerprint="old",
        same_failure_count=worker_module.CLEANUP_IDENTICAL_FAILURE_LIMIT,
        retry_suppressed=True,
        next_retry_at=None,
        retry_count=7,
    )
    state = store.save(path, state)
    requested = store.request_control(path, "clear_team", reason="operator reverify")
    worker._endpoint_pool = SimpleNamespace(contexts={})

    assert asyncio.run(
        worker._apply_control(
            requested,
            FakeActions(),
            path,
            persisted_result=[],
        )
    ) is True

    saved = store.load(path)
    assert saved["cleanup"]["retry_count"] == 8
    assert saved["cleanup"]["same_failure_count"] == 1
    assert saved["cleanup"]["retry_suppressed"] is False
    assert saved["cleanup"]["last_error"] == (
        "RoleOwnershipError: cleanup cannot verify unavailable browser legacy"
    )


def test_ambient_page_automation_uses_workspace_timeout_config(tmp_path: Path, monkeypatch):
    config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-timeout-config"
    )
    seen_timeouts = []

    class Page:
        url = "https://chatgpt.com/c/ambient"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": "task-ambient-timeout-config",
                "pageId": "page-ambient",
                "role": "alpha-plan",
            })

    class Client:
        def __init__(self, _page, *, timeout_ms):
            seen_timeouts.append(timeout_ms)

        async def install_page_observer(self):
            return None

        async def read_wait_probe(self):
            return SimpleNamespace(stop_visible=False, last_assistant_message_id=None)

        def page_observation(self):
            return {}

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[Page()])))

    assert seen_timeouts == [min(15_000, round(config.workspace_timeout_seconds * 1000))]


def test_ambient_page_automation_never_dispatches_registered_task(tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-single-owner"
    )
    worker.registry = SimpleNamespace(
        tasks_by_id={
            "task-ambient-single-owner": {
                "status": "RUNNING",
                "active_hop_id": 1,
                "hops": [{"hop_id": 1, "target_role": "PLAN", "state": "waiting"}],
                "roles": {"PLAN": {"page_id": "page-active"}},
            }
        }
    )
    calls = []

    class Page:
        url = "https://chatgpt.com/c/ambient"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": "task-ambient-single-owner",
                "pageId": "page-active",
                "role": "cdpa-ambient-single-owner-plan",
            })

    class Client:
        def __init__(self, _page, *, timeout_ms):
            calls.append(("init", timeout_ms))

        async def install_page_observer(self):
            calls.append(("listen",))

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[Page()])))

    assert calls[0][0] == "init"
    assert calls[1] == ("listen",)


def test_worker_arms_passive_observer_with_exact_hop_generation_and_receipt(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-passive-observer-wiring"
    )
    hop = _active_hop(state)
    state["roles"]["PLAN"]["conversation_generation"] = 7
    receipt = SendReceipt(
        prompt="prompt",
        prompt_sha256=prompt_digest("prompt"),
        binding=PageBinding("page-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u-passive",
        user_turn_id="t-passive",
        conversation_id="conversation-passive",
    )

    class Client:
        def __init__(self):
            self.calls = []

        def arm_passive_observer(self, **kwargs):
            self.calls.append(kwargs)

    client = Client()
    worker._arm_passive_request_observer(state, hop, client, receipt)

    assert client.calls == [
        {
            "request_id": hop["request_id"],
            "generation": 7,
            "conversation_id": "conversation-passive",
            "accepted_user_message_id": "u-passive",
            "task_id": "task-passive-observer-wiring",
            "team": "alpha",
        }
    ]


def test_retry_ui_queues_format_repair_without_retry_click(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-retry-format-repair"
    )
    ledger = RequestLedger(hop["ledger_path"])
    assert ledger.get(hop["request_id"]).status is RequestStatus.SENT
    worker._queue_format_repair(
        state,
        hop,
        None,
        RouteContractError("ChatGPT Retry UI is visible; continue from the existing state"),
    )
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == hop["target_role"]
    assert repair["turn"] == hop["turn"]
    original = ledger.get(hop["request_id"])
    assert original.status is RequestStatus.SENT
    assert original.response is None


def test_worker_passive_listener_remains_attached_across_rearms(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-passive-mode-switch"
    )
    hop = _active_hop(state)
    worker.runtime_db.ensure_schema()

    class Client:
        def __init__(self):
            self.armed = 0
            self.detached = 0

        def arm_passive_observer(self, **_kwargs):
            self.armed += 1

        def detach_passive_observer(self):
            self.detached += 1

    client = Client()
    worker._arm_passive_request_observer(state, hop, client)
    assert (client.armed, client.detached) == (1, 0)

    worker._arm_passive_request_observer(state, hop, client)
    assert (client.armed, client.detached) == (2, 0)


def bootstrap_record(*, bootstrap_id="general-team-bootstrap", enabled=True):
    return {
        "bootstrap_id": bootstrap_id,
        "name": "General Team Bootstrap",
        "description": "Reusable task-neutral context",
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_assistant_message_id": "22222222-2222-4222-8222-222222222222",
        "enabled": enabled,
        "tags": ["general"],
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:00+00:00",
        "expires_at": None,
        "last_verified_at": None,
        "source_fingerprints": {},
    }


def send_snapshot(
    *,
    text="",
    messages=(),
    state=ChatGPTState.NEW_CHAT,
    task_id="task-a",
    team="alpha",
):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/cdpa-send",
        session_id="cdpa-send",
        page_id="page-PLAN",
        page_role="PLAN",
        page_task_id=task_id,
        page_team=team,
        state=state,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text=text,
        send_visible=bool(text),
        send_enabled=bool(text),
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=(),
        messages=tuple(messages),
    )


class RecordingCDPASendClient:
    def __init__(self, *, task_id="task-a", team="alpha"):
        self.binding = PageBinding("page-PLAN", "PLAN")
        self.current = send_snapshot(task_id=task_id, team=team)
        self.set_calls = []
        self.send_calls = []
        self.send_timeout_ms = []
        self.send_max_attempts = []
        self.send_recovery_reload = []

    async def assert_ownership(self):
        return self.current

    async def set_text(self, text):
        self.set_calls.append(text)
        self.current = send_snapshot(
            text=text,
            messages=self.current.messages,
            state=ChatGPTState.DRAFT,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )

    async def send(
        self,
        text,
        *,
        wait_for_stop=True,
        max_attempts=2,
        recovery_reload=True,
        expected_task_id=None,
        expected_team=None,
        expected_attachment_ownership_token=None,
        expected_attachment_count=0,
        expected_attachment_names=None,
        timeout_ms=None,
    ):
        self.send_calls.append(text)
        self.send_timeout_ms.append(timeout_ms)
        self.send_max_attempts.append(max_attempts)
        self.send_recovery_reload.append(recovery_reload)
        baseline = capture_message_baseline(self.current.messages)
        user = MessageSnapshot("user", "u1", "t1", text, ())
        self.current = send_snapshot(
            messages=(*self.current.messages, user),
            state=ChatGPTState.SUBMITTING,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )
        return SendReceipt(
            prompt=text,
            prompt_sha256=prompt_digest(text),
            binding=self.binding,
            baseline=baseline,
            attempts=1,
            accepted_via="user_message_identity",
            session_id_before="cdpa-send",
            user_message_id="u1",
            user_turn_id="t1",
        )


class RecordingCDPASendActions:
    def __init__(self, client):
        self.client = client

    async def locate_owned(self, _state, _role):
        return AcquiredRole(
            client=self.client,
            page_id="page-PLAN",
            url="https://chatgpt.com/c/cdpa-send",
            created=False,
            new_chat=False,
        )


def test_waiting_stop_control_applies_before_waiting_short_circuit(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    dependency = store.create_task(
        "dependency",
        requested_team="dependency",
        task_id="task-waiting-stop-dependency",
        governing_objective="dependency",
        governing_directives=(),
    )
    state = store.create_task(
        "waiting child",
        requested_team="waiting-stop",
        task_id="task-waiting-stop",
        depends_on_task_ids=(dependency["task_id"],),
        governing_objective="waiting child",
        governing_directives=(),
    )
    path = Path(state["manifest_path"])
    assert state["status"] == "WAITING"

    requested = store.request_control(path, "stop", reason="operator force stop")
    assert requested["controls"][-1]["status"] == "requested"

    actions = FakeActions()
    worker = CDPAWorker(config, store=store)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    stopped = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=store.discover(),
        )
    )

    assert stopped == store.load(path)
    assert stopped["status"] == "STOPPED"
    assert stopped["terminal_state"] == "STOPPED"
    assert stopped["controls"][-1]["status"] == "applied"


def test_stop_terminalizes_even_when_owned_tab_lookup_fails(tmp_path: Path):
    config, store, state, worker = setup_task(tmp_path, task_id="task-force-stop")
    path = Path(state["manifest_path"])
    requested = store.request_control(path, "stop", reason="operator force stop")

    class MissingOwnedTabActions(FakeActions):
        async def locate_owned(self, _state, _role):
            raise RoleOwnershipError("owned tab is unavailable")

    stopped = asyncio.run(worker._apply_control(requested, MissingOwnedTabActions(), path))

    assert stopped is True
    assert requested["status"] == "STOPPED"
    assert requested["terminal_state"] == "STOPPED"
    assert requested["controls"][-1]["status"] == "applied"


def test_pause_requested_during_role_acquisition_survives_and_applies_before_send(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "owner",
        requested_team="alpha",
        task_id="task-presend-control-race",
        governing_objective="owner",
        governing_directives=(),
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)

    class ConcurrentAcquireActions(FakeActions):
        async def acquire(self, current, role, **_kwargs):
            store.request_control(
                path,
                "pause",
                reason="pause requested during role acquisition",
            )
            return await super().acquire(current, role)

    actions = ConcurrentAcquireActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    first = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert first == store.load(path)
    assert _active_hop(first)["state"] == "sending"
    assert len(first["controls"]) == 1
    assert first["controls"][0]["action"] == "pause"
    assert first["controls"][0]["role"] is None
    assert first["controls"][0]["reason"] == "pause requested during role acquisition"
    assert first["controls"][0]["status"] == "requested"

    second = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert second == store.load(path)
    assert second["status"] == "PAUSED"
    assert second["controls"][0]["status"] == "applied"
    assert _active_hop(second)["state"] == "sending"


def test_pause_requested_after_accepted_send_survives_without_duplicate_send(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "owner",
        requested_team="alpha",
        task_id="task-send-control-race",
        governing_objective="owner",
        governing_directives=(),
    )
    path = Path(state["manifest_path"])
    worker = CDPAWorker(config, store=store)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: FakeActions())
    prepared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    assert _active_hop(prepared)["state"] == "sending"

    class ConcurrentSendClient(RecordingCDPASendClient):
        async def send(self, text, **kwargs):
            receipt = await super().send(text, **kwargs)
            store.request_control(
                path,
                "pause",
                reason="pause requested after accepted send",
            )
            return receipt

    client = ConcurrentSendClient(
        task_id=prepared["task_id"],
        team=prepared["team"],
    )
    actions = RecordingCDPASendActions(client)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    sent = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert sent == store.load(path)
    assert _active_hop(sent)["state"] == "sent"
    assert len(client.send_calls) == 1
    assert sent["controls"][0]["action"] == "pause"
    assert sent["controls"][0]["status"] == "requested"

    paused = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert paused == store.load(path)
    assert paused["status"] == "PAUSED"
    assert paused["controls"][0]["status"] == "applied"
    assert _active_hop(paused)["state"] == "sent"
    assert len(client.send_calls) == 1


def test_pre_send_lazily_acquires_only_plan_with_compact_prompt(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert hop["state"] == "sending"
    assert hop["expected_report_path"] == ".plan/alpha/alpha-plan_turn1_task-a.md"
    assert hop["prompt"].count("Constructor for PLAN") == 1
    assert "Before replying, write a non-empty report to exactly: .plan/alpha/alpha-plan_turn1_task-a.md" in hop["prompt"]
    assert '"handoff":".plan/alpha/alpha-plan_turn1_task-a.md"' in hop["prompt"]
    assert ".plan/<team>/<physical-role>_turn<N>_<task-id>.md" not in hop["prompt"]
    assert hop["prompt"].startswith("You are PLAN for the team alpha | task-a:")
    assert f"Turn: 1\nWorkspace: {tmp_path}" in hop["prompt"]
    assert "Goal: Implement exact production behavior" in hop["prompt"]
    assert "Handoff: Initial task intake; no prior role handoff." in hop["prompt"]
    route_contract = next(
        json.loads(line)["route"]
        for line in hop["prompt"].splitlines()
        if line.startswith('{"route":')
    )
    assert route_contract.split("|") == ["PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"]
    assert "PAUSE requires an external/manual prerequisite" in hop["prompt"]
    assert state["roles"]["PLAN"]["page_id"] == "page-alpha-plan"
    assert state["roles"]["DEV"]["status"] == "unallocated"
    assert state["roles"]["PLAN"]["constructor_sent_generation"] == 0


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_pre_send_first_role_uses_bootstrap_once_and_keeps_full_prompt(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id="task-bootstrap-runtime",
        roles=("PLAN", "DEV", "REVIEW"),
        bootstrap=anchor,
        governing_objective="Implement exact production behavior",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id, **_kwargs
        ):
            self.branch_calls.append((role, source_conversation_id, assistant_message_id))
            return AcquiredRole(
                client=ready_client(_state, role),
                page_id=f"branch-{role.lower()}",
                url=f"https://chatgpt.com/c/{role.lower()}-branch",
                created=True,
                new_chat=True,
            )

    actions = Actions()
    first = _active_hop(state)
    asyncio.run(worker._pre_send(state, first, actions))

    donor = anchor["donors"][0]
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"
    assert state["roles"]["PLAN"]["conversation_generation"] == 1
    assert "Constructor for PLAN" in first["prompt"]
    assert "Return exactly one valid JSON object" in first["prompt"]

    plan_turn2 = worker._append_hop(
        state, source_role="REVIEW", target_role="PLAN", handoff="return to plan"
    )
    asyncio.run(worker._pre_send(state, plan_turn2, actions))
    assert len(actions.branch_calls) == 1
    assert "PLAN_CLOSURE: Treat the handoff and accepted role reports as closure evidence." in plan_turn2["prompt"]
    assert "Constructor for PLAN" not in plan_turn2["prompt"]
    assert plan_turn2["prompt"].index("Work first.") < plan_turn2["prompt"].index("PLAN_CLOSURE")
    assert plan_turn2["prompt"].count('{"route":"') == 1
    assert state["roles"]["PLAN"]["page_id"] == "page-alpha-plan"

    for role in ("DEV", "REVIEW"):
        hop = worker._append_hop(
            state, source_role="PLAN", target_role=role, handoff=f"first {role}"
        )
        asyncio.run(worker._pre_send(state, hop, actions))
    assert [call[0] for call in actions.branch_calls] == ["PLAN", "DEV", "REVIEW"]
    assert all(call[1:] == actions.branch_calls[0][1:] for call in actions.branch_calls)


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_bootstrap_branch_rejects_foreign_durable_writable_conversation(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    state = store.create_task(
        "owner collision",
        requested_team="owner-collision",
        task_id="task-owner-collision",
        bootstrap=anchor,
        governing_objective="owner collision",
        governing_directives=(),
    )
    foreign = store.create_task(
        "foreign owner",
        requested_team="foreign-owner",
        task_id="task-foreign-owner",
        governing_objective="foreign owner",
        governing_directives=(),
    )
    candidate_id = "88888888-8888-4888-8888-888888888888"
    foreign_role = foreign["roles"]["PLAN"]
    foreign_role["page_id"] = "foreign-owned-page"
    foreign_role["page_url"] = f"https://chatgpt.com/c/{candidate_id}"
    store.save(Path(foreign["manifest_path"]), foreign)
    worker = CDPAWorker(config, store=store)

    class BranchPage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    branch_page = BranchPage()

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            return AcquiredRole(
                client=SimpleNamespace(page=branch_page),
                page_id="new-branch-page",
                url=f"https://chatgpt.com/c/{candidate_id}",
                created=True,
                new_chat=True,
            )

    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", Actions()))

    assert acquired is None
    assert branch_page.closed is True
    assert state["roles"]["PLAN"].get("page_id") is None
    assert state["active_action"] == "bootstrap_retry"


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_pre_send_unresolved_native_branch_blocks_without_ui_fallback(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    state = store.create_task(
        "unresolved provisional branch",
        requested_team="unresolved-branch",
        task_id="task-unresolved-branch",
        bootstrap=anchor,
        governing_objective="unresolved provisional branch",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)
    unresolved_type = getattr(
        worker_module, "BranchTargetUnresolvedError", BranchBootstrapError
    )

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise unresolved_type("branch target remained provisional WEB identity")

    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, Actions()))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "branch_target_unresolved"
    assert state["active_action"] == "blocked"
    assert hop["state"] == "pre_send"
    assert RequestLedger(hop["ledger_path"]).peek(hop["request_id"]) is None


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_bootstrap_alias_rejection_keeps_durable_attempts_at_zero(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    anchor = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    state = store.create_task(
        "alias stays pre-send",
        requested_team="alias-zero-send",
        task_id="task-alias-zero-send",
        bootstrap=anchor,
        governing_objective="alias stays pre-send",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == anchor["conversation_id"]
            return _bootstrap_graph(anchor["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise BranchBootstrapError("branch canonicalized back to donor")

    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, Actions()))

    assert hop["state"] != "sending"
    assert state["active_action"] == "bootstrap_retry"
    assert RequestLedger(hop["ledger_path"]).peek(hop["request_id"]) is None


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_pre_send_bootstrap_transient_failure_stays_pre_send_without_fresh_fallback(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    selected = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    state = store.create_task(
        "fallback",
        requested_team="fallback",
        task_id="task-bootstrap-fallback",
        bootstrap=selected,
        governing_objective="fallback",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branch_calls = 0
            self.acquire_calls = 0

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == bootstrap_record()["conversation_id"]
            return _bootstrap_graph(bootstrap_record()["terminal_assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise BranchBootstrapError("native unavailable")

        async def acquire(self, state, role, **_kwargs):
            self.acquire_calls += 1
            return await super().acquire(state, role)

    actions = Actions()
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, actions))
    assert actions.branch_calls == 1
    assert actions.acquire_calls == 0
    assert state["active_action"] == "bootstrap_retry"
    assert state["roles"]["PLAN"].get("context_source") is None
    assert hop["state"] == "pre_send"
    assert RequestLedger(hop["ledger_path"]).peek(hop["request_id"]) is None

    protected = store.create_task(
        "protected send",
        requested_team="protected-send",
        task_id="task-bootstrap-protected-send",
        bootstrap=selected,
        governing_objective="protected send",
        governing_directives=(),
    )
    protected_hop = _active_hop(protected)
    protected_hop["receipt"] = {"accepted": True}
    no_actions = Actions()
    asyncio.run(worker._pre_send(protected, protected_hop, no_actions))
    assert no_actions.branch_calls == 0
    assert no_actions.acquire_calls == 0
    assert protected["block_code"] == "pre_send_request_recovery_required"


def test_pre_send_limits_allowed_routes_to_selected_workflow_roles(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, roles=("PLAN", "REVIEW"))
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    report_path = ".plan/alpha/alpha-plan_turn1_task-a.md"
    route_contract = next(
        json.loads(line)["route"]
        for line in hop["prompt"].splitlines()
        if line.startswith('{"route":')
    )

    assert route_contract.split("|") == ["PLAN", "REVIEW", "DONE"]
    assert "PAUSE requires an external/manual prerequisite" in hop["prompt"]
    assert hop["prompt"].count("Constructor for PLAN") == 1
    assert f"Before replying, write a non-empty report to exactly: {report_path}" in hop["prompt"]
    assert f'"handoff":"{report_path}"' in hop["prompt"]
    assert "Handoff: Initial task intake; no prior role handoff." in hop["prompt"]
    assert ".plan/<team>/<physical-role>_turn<N>_<task-id>.md" not in hop["prompt"]
    assert list(state["roles"]) == ["PLAN", "REVIEW"]


def test_normal_cdpa_send_keeps_transport_identity_out_of_actual_payload(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    client = RecordingCDPASendClient()

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert client.send_calls == [hop["prompt"]]
    assert client.send_timeout_ms == [None]
    assert client.send_calls[0].startswith("You are PLAN for the team alpha | task-a:")
    assert hop["receipt"]["prompt"] == hop["prompt"]
    assert record is not None
    assert record.rendered_prompt == hop["prompt"]
    for forbidden in (
        "ROLE_REQUEST_ID",
        hop["request_id"],
        hop["ledger_path"],
        state["manifest_path"],
        "controller_id",
        "run_id",
        "prompt_sha256",
        "created_at",
        "updated_at",
        "page_id",
    ):
        assert forbidden not in client.send_calls[0]
        assert forbidden not in hop["receipt"]["prompt"]




def test_reused_cdpa_role_waits_for_hydrated_history_before_send(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-reused-history")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    state["hops"].insert(
        0,
        {
            "hop_id": 0,
            "target_role": "PLAN",
            "receipt": {"accepted_via": "user_message_identity"},
            "response_record": {
                "role": "assistant",
                "message_id": "a-old",
                "turn_id": "t-old",
                "text": "older answer",
                "actions": [],
                "image_count": 0,
            },
        },
    )
    history = (
        MessageSnapshot("user", "u-old", "t-old", "older prompt", ()),
        MessageSnapshot("assistant", "a-old", "t-old", "older answer", ()),
    )

    class HydratingClient(RecordingCDPASendClient):
        def __init__(self):
            super().__init__(task_id=state["task_id"], team=state["team"])
            self.prompt_reads = 0
            self.require_history = []

        async def assert_ownership(self):
            if self.current.composer_text and not self.current.messages:
                self.prompt_reads += 1
                if self.prompt_reads >= 2:
                    self.current = send_snapshot(
                        text=self.current.composer_text,
                        messages=history,
                        state=ChatGPTState.DRAFT,
                        task_id=state["task_id"],
                        team=state["team"],
                    )
            return self.current

        async def send(
            self,
            text,
            *,
            require_existing_conversation_baseline=False,
            **kwargs,
        ):
            self.require_history.append(require_existing_conversation_baseline)
            return await super().send(text, **kwargs)

    client = HydratingClient()

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert record is not None and record.baseline is not None
    assert record.baseline.message_ids == frozenset({"u-old", "a-old"})
    assert client.require_history == [True]
    assert len(client.send_calls) == 1


def test_reused_cdpa_role_persistent_empty_history_blocks_retryably_preboundary(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(tmp_path, task_id="task-reused-history-empty")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    state["hops"].insert(
        0,
        {
            "hop_id": 0,
            "target_role": "PLAN",
            "receipt": {"accepted_via": "user_message_identity"},
            "response_record": {
                "role": "assistant",
                "message_id": "a-old",
                "turn_id": "t-old",
                "text": "older answer",
                "actions": [],
                "image_count": 0,
            },
        },
    )
    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "conversation_transcript_not_ready"
    assert state["block_retryable"] is True
    assert record is not None and record.status is RequestStatus.PROMPT_SET
    assert record.attempts == 0
    assert record.baseline is None
    assert client.send_calls == []


def test_repair_after_accepted_send_without_response_does_not_require_old_transcript(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(tmp_path, task_id="task-repair-no-response-history")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    state["hops"].insert(
        0,
        {
            "hop_id": 0,
            "target_role": "PLAN",
            "receipt": {"accepted_via": "assistant_activity"},
            "response": None,
            "response_record": None,
        },
    )

    class Client(RecordingCDPASendClient):
        def __init__(self):
            super().__init__(task_id=state["task_id"], team=state["team"])
            self.require_history = []

        async def send(
            self,
            text,
            *,
            require_existing_conversation_baseline=False,
            **kwargs,
        ):
            self.require_history.append(require_existing_conversation_baseline)
            return await super().send(text, **kwargs)

    client = Client()
    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    assert client.require_history == [False]
    assert len(client.send_calls) == 1
    assert state["block_code"] is None


def test_missing_local_file_report_preserves_route_and_queues_report_repair(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-missing-local-report"
    )
    handoff = str(hop["expected_report_path"])
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert hop["route"] == "DEV"
    assert repair["kind"] == "report_repair"
    assert repair["target_role"] == "PLAN"
    assert repair["locked_route"] == "DEV"
    assert repair["locked_handoff"] == handoff
    assert "report" in repair["validation_error"].lower()
    assert "missing" in repair["validation_error"].lower()
    assert state["reports"] == []
    assert not any(
        item.get("target_role") == "DEV" and item.get("kind") == "handoff"
        for item in state["hops"]
    )

    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert "CDPA_REPORT_REPAIR" in repair["prompt"]
    assert "prior route was DEV" in repair["prompt"]
    assert "historical context, not a lock" in repair["prompt"]
    assert handoff in repair["prompt"]
    assert '"route":"DEV"' in repair["prompt"]
    assert len(repair["prompt"]) <= 2500
    assert "do not invent evidence" in repair["prompt"]
    assert "CDPA_FORMAT_REPAIR" not in repair["prompt"]

    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "# PLAN report\n\n## Evidence\n- Inspected `path=.plan/alpha/alpha-plan_turn1_task-missing-local-report.md`.\n- `send_attempts=1` -> recovered report artifact without replay.\n",
        encoding="utf-8",
    )
    repair["response"] = json.dumps({"route": "REVIEW", "handoff": handoff})
    repair["state"] = "responded"
    worker._responded(state, repair)

    assert repair["route"] == "REVIEW"
    assert _active_hop(state)["target_role"] == "REVIEW"
    assert _active_hop(state)["kind"] == "handoff"


def test_empty_local_file_report_enters_report_repair_without_advancing(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-empty-local-report"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_bytes(b"")
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "report_repair"
    assert repair["locked_route"] == "DEV"
    assert "report" in repair["validation_error"].lower()
    assert "empty" in repair["validation_error"].lower()
    assert state["reports"] == []


def test_local_file_report_routes_once_with_hash_and_size_evidence(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-valid-local-report"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    data = b"# DEV report\n\n## Evidence\n- Verified `path=.plan/alpha/alpha-plan_turn1_task-valid-local-report.md`.\n- `checks=1` -> local artifact validation passed.\n"
    report.write_bytes(data)
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["state"] == "routed"
    assert hop["route"] == "DEV"
    assert hop["report_path"] == handoff
    assert hop["report_sha256"] == worker_module.hashlib.sha256(data).hexdigest()
    assert hop["report_size"] == len(data)
    assert child["target_role"] == "DEV"
    assert child["handoff"] == handoff
    assert state["reports"][-1]["sha256"] == hop["report_sha256"]
    assert state["reports"][-1]["size"] == len(data)


def test_mismatched_local_file_report_path_enters_route_repair(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-mismatched-local-report"
    )
    handoff = (
        f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_"
        "wrong-task-id.md"
    )
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# wrong report\n", encoding="utf-8")
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert "expected role report" in repair["validation_error"].lower()
    assert state["reports"] == []


def test_symlink_local_file_report_enters_report_repair(tmp_path: Path):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-symlink-local-report"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-report.md"
    outside.write_text("# outside\n", encoding="utf-8")
    try:
        report.symlink_to(outside)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    hop["response"] = json.dumps({"route": "DEV", "handoff": handoff})
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "report_repair"
    assert repair["locked_route"] == "DEV"
    assert "symlink" in repair["validation_error"].lower()
    assert state["reports"] == []



def test_unselected_route_enters_safe_repair_without_recording_report(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-unselected-route",
        roles=("PLAN", "REVIEW"),
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = (
        '{"route":"DEV","handoff":"' + str(hop["expected_report_path"]) + '"}'
    )
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert "not selected" in repair["validation_error"]
    assert state["reports"] == []
    assert not (tmp_path / hop["expected_report_path"]).exists()


def test_malformed_response_creates_same_turn_guide_only_repair(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = "not json"
    hop["state"] = "responded"

    worker._responded(state, hop)
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert repair["turn"] == 1
    assert repair["repair_attempt"] == 1

    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert "Implement exact production behavior" not in repair["prompt"]
    assert "Fix the current response issue:" not in repair["prompt"]
    assert "repairing the route response for alpha | task-a" in repair["prompt"]
    assert "Validation error:" in repair["prompt"]
    assert f"Expected report path: {repair['expected_report_path']}" in repair["prompt"]
    assert "Continue from the existing workspace/report state." in repair["prompt"]
    assert "Return exactly one valid JSON object:" in repair["prompt"]
    assert "Output exactly one JSON object and nothing else." in repair["prompt"]
    assert "Constructor for PLAN" not in repair["prompt"]
    assert repair["handoff"] == "route repair"
















def test_only_plan_done_can_make_task_terminal(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "# PLAN report\n\n## Evidence\n- Inspected `path=.plan/alpha/alpha-plan_turn1_task-a.md` before final routing.\n- `checks=1` -> final PLAN verification passed.\n",
        encoding="utf-8",
    )
    hop["response"] = '{"route":"DONE","handoff":".plan/alpha/alpha-plan_turn1_task-a.md"}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert state["active_hop_id"] is None
    assert state["kanban_column"] == "DONE_STOPPED"


def test_resume_and_retry_controls_have_distinct_state_contracts(tmp_path: Path):
    _, _, paused, worker = setup_task(tmp_path, task_id="task-paused")
    paused["status"] = "PAUSED"
    paused["kanban_column"] = "PAUSED"
    paused["resume_column"] = "PLANNING"
    paused["pause_reason"] = "manual"
    paused_request_id = _active_hop(paused)["request_id"]
    paused["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(paused, FakeActions())) is True
    assert paused["status"] == "RUNNING"
    assert paused["kanban_column"] == "PLANNING"
    assert paused["controls"][0]["status"] == "recovering"
    assert paused["controls"][0]["command_state"] == "RUNNING"
    assert paused["controls"][0]["applied_at"] is None
    assert paused["controls"][0]["result"]["outcome"] == "recovering"
    assert paused["controls"][0]["result"]["before"]["active_request_id"] == paused_request_id
    assert _active_hop(paused)["request_id"] == paused_request_id

    _, _, blocked, worker = setup_task(tmp_path, task_id="task-blocked")
    blocked["status"] = "BLOCKED"
    blocked["kanban_column"] = "BLOCKED"
    blocked["block_reason"] = "validation failed"
    blocked_hop = _active_hop(blocked)
    blocked_hop["repair_attempt"] = 2
    blocked_request_id = blocked_hop["request_id"]
    blocked["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(blocked, FakeActions())) is True
    assert blocked["status"] == "RUNNING"
    assert blocked["kanban_column"] == "PLANNING"
    assert blocked["block_reason"] is None
    assert blocked["controls"][0]["status"] == "recovering"
    assert blocked["controls"][0]["command_state"] == "RUNNING"
    assert blocked["controls"][0]["applied_at"] is None
    assert blocked["controls"][0]["result"]["outcome"] == "recovering"
    assert blocked["controls"][0]["result"]["before"]["active_request_id"] == blocked_request_id
    assert _active_hop(blocked)["repair_attempt"] == 2
    assert _active_hop(blocked)["request_id"] == blocked_request_id

    _, _, retried, worker = setup_task(tmp_path, task_id="task-retry")
    retried["status"] = "BLOCKED"
    retried["kanban_column"] = "BLOCKED"
    retried["block_code"] = "route_validation_exhausted"
    retried["block_retryable"] = True
    retried["block_reason"] = "route repair exhausted"
    retry_hop = _active_hop(retried)
    retry_hop["validation_error"] = "bad route"
    retry_hop["repair_attempt"] = worker.config.route_repair_attempts
    retried["controls"] = [
        {
            "control_id": 1,
            "action": "retry",
            "role": "PLAN",
            "status": "requested",
        }
    ]
    assert asyncio.run(worker._apply_control(retried, FakeActions())) is True
    assert retried["status"] == "RUNNING"
    assert retry_hop["repair_attempt"] == worker.config.route_repair_attempts - 1








































def test_open_tab_recovers_presend_role_offline_and_sends_original_once(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-presend-recovery"
    )
    path = Path(state["manifest_path"])
    worker.runtime_db.ensure_schema()
    hop = _active_hop(state)
    original_hop_id = hop["hop_id"]
    original_request_id = hop["request_id"]
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
        active_action="blocked",
    )
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/owned-plan",
        online=False,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact owned PLAN tab",
            "confirmed": False,
            "status": "requested",
            "requested_at": "2026-07-23T00:00:00+00:00",
            "applied_at": None,
            "result": None,
        }
    ]
    store.save(path, state)
    sent_prompts: list[str] = []

    current = ChatGPTSnapshot(
        url="https://chatgpt.com/c/owned-plan",
        session_id="owned-plan",
        page_id="recovered-page",
        page_role=state["roles"]["PLAN"]["physical_role"],
        page_task_id=state["task_id"],
        page_team=state["team"],
        state=ChatGPTState.WAITING_PROMPT,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="",
        send_visible=True,
        send_enabled=True,
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=(),
        messages=(),
    )

    class RecoveryClient:
        async def assert_ownership(self):
            return current

        async def wait_snapshot(self, _receipt, **_kwargs):
            return current

        def page_observation(self):
            return {}

    client = RecoveryClient()
    acquired = AcquiredRole(
        client=client,
        page_id="recovered-page",
        url="https://chatgpt.com/c/owned-plan",
        created=False,
        new_chat=False,
    )

    class RecoveryActions:
        def __init__(self):
            self.recovered = False

        async def locate_owned(self, _state, _role):
            return acquired if self.recovered else None

        async def reopen(
            self,
            _state,
            _role,
            *,
            require_clean_ready=True,
            foreground=True,
        ):
            assert require_clean_ready is True
            assert foreground is True
            self.recovered = True
            return acquired

        async def open_tab(self, _acquired):
            self.recovered = True

        async def acquire(self, _state, _role, **_kwargs):
            assert self.recovered is True
            return acquired

    actions = RecoveryActions()
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: actions,
    )

    class OneSendBlock:
        def __init__(self, prompt, **_kwargs):
            self.prompt = prompt

        async def run(self, _context):
            sent_prompts.append(self.prompt)
            receipt = SendReceipt(
                prompt=self.prompt,
                prompt_sha256=prompt_digest(self.prompt),
                binding=PageBinding("recovered-page", "alpha-plan"),
                baseline=MessageBaseline(
                    frozenset(), frozenset(), frozenset(), frozenset()
                ),
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before="owned-plan",
                conversation_id="owned-plan",
                user_message_id="user-1",
                user_turn_id="turn-1",
            )
            return {
                "receipt": receipt.to_dict(),
                "record": {
                    "accepted_at": datetime.now(timezone.utc).timestamp(),
                },
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", OneSendBlock)
    context = SimpleNamespace(pages=[])

    recovered = asyncio.run(worker.advance(path, context))
    recovered_hop = _active_hop(recovered)
    assert recovered["status"] == "RUNNING"
    assert recovered["kanban_column"] == "PLANNING"
    assert recovered["block_code"] is None
    assert recovered["block_reason"] is None
    assert recovered["roles"]["PLAN"]["online"] is True
    assert recovered["roles"]["PLAN"]["page_id"] == "recovered-page"
    assert recovered_hop["hop_id"] == original_hop_id
    assert recovered_hop["request_id"] == original_request_id
    assert recovered_hop["state"] == "pre_send"
    assert recovered["controls"][0]["status"] == "applied"
    assert recovered["controls"][0]["result"]["resumed"] is True
    assert sent_prompts == []

    prepared = asyncio.run(worker.advance(path, context))
    assert _active_hop(prepared)["state"] == "sending"
    assert _active_hop(prepared)["request_id"] == original_request_id
    assert sent_prompts == []

    sent = asyncio.run(worker.advance(path, context))
    assert _active_hop(sent)["state"] == "sent"
    assert _active_hop(sent)["request_id"] == original_request_id
    assert len(sent["hops"]) == 1
    assert len(sent_prompts) == 1

    waiting = asyncio.run(worker.advance(path, context))
    waiting_hop = _active_hop(waiting)
    assert waiting_hop["state"] == "waiting"
    assert waiting_hop["request_id"] == original_request_id
    assert len(sent_prompts) == 1

    asyncio.run(worker._waiting(waiting, waiting_hop, actions, path))
    assert _active_hop(waiting)["state"] == "waiting"
    assert len(sent_prompts) == 1


def test_open_tab_reconciles_preboundary_acquisition_timeout_without_requiring_clean_ready(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-preboundary-acquisition"
    )
    hop = _active_hop(state)
    role = state["roles"]["PLAN"]
    conversation_url = "https://chatgpt.com/c/owned-plan-preboundary"
    original_request_id = hop["request_id"]
    role.update(
        page_id="owned-plan-page",
        page_url=conversation_url,
        online=False,
        status="pending",
    )
    hop["browser_endpoint_id"] = role.get("browser_endpoint_id")
    hop["preboundary_recovery"] = {
        "phase": "recovery_required",
        "stage": "acquisition",
        "request_id": original_request_id,
        "attempts": 0,
        "last_failure": "TimeoutError: clean-ready timeout",
    }
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        active_action="blocked",
        block_code="preboundary_acquisition_recovery_required",
        block_retryable=False,
        block_reason=(
            "Pre-Send clean-ready acquisition timed out without a returned binding; "
            "exact owned-page reconciliation is required before another acquisition or Send."
        ),
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "reconcile exact pre-Send page",
            "status": "requested",
        }
    ]
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="owned-plan-page",
        url=conversation_url,
        created=True,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.reopen_calls = []

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, **kwargs):
            self.reopen_calls.append(kwargs)
            return acquired

    actions = Actions()
    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert actions.reopen_calls == [{"require_clean_ready": False, "foreground": True}]
    assert state["controls"][0]["status"] == "applied"
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["active_action"] == "resuming"
    assert hop["state"] == "pre_send"
    assert hop["request_id"] == original_request_id
    assert hop.get("receipt") is None
    assert "preboundary_recovery" not in hop
    assert state["roles"]["PLAN"]["page_id"] == "owned-plan-page"
    assert state["roles"]["PLAN"]["page_url"] == conversation_url


@pytest.mark.parametrize("via", ["user_message_identity", "stop_button"])
def test_open_tab_recovers_waiting_accepted_send_on_exact_conversation_without_resend(
    tmp_path: Path, via: str,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-waiting-accepted"
    )
    hop = _active_hop(state)
    conversation_url = "https://chatgpt.com/c/owned-plan"
    original_request_id = hop["request_id"]
    receipt = {
        "prompt": "accepted prompt",
        "prompt_sha256": "digest",
        "binding": {"page_id": "closed-page", "role": "alpha-plan"},
        "accepted_via": via,
        "user_message_id": "accepted-user-message" if via == "user_message_identity" else None,
        "user_turn_id": "accepted-user-turn" if via == "user_message_identity" else None,
        "attempts": 1,
    }
    hop.update(
        state="waiting",
        conversation_url=conversation_url,
        receipt=receipt,
    )
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url=conversation_url,
        online=False,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact accepted waiting conversation",
            "status": "requested",
        }
    ]

    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="closed-page",
        url=conversation_url,
        created=True,
        new_chat=False,
    )

    class ExactWaitingRecovery:
        def __init__(self):
            self.reopen_clean = []

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, require_clean_ready=True, **_kwargs):
            self.reopen_clean.append(require_clean_ready)
            if require_clean_ready:
                raise RuntimeError("accepted waiting has no clean editable composer")
            return acquired

    actions = ExactWaitingRecovery()

    assert asyncio.run(worker._apply_control(state, actions)) is True
    assert actions.reopen_clean == [False]
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["roles"]["PLAN"]["page_id"] == "closed-page"
    assert state["roles"]["PLAN"]["page_url"] == conversation_url
    assert hop["state"] == "waiting"
    assert hop["request_id"] == original_request_id
    assert hop["receipt"] == receipt
    assert state["controls"][0]["status"] == "applied"
    assert state["controls"][0]["result"] == {
        "page_id": "closed-page",
        "recovered": True,
        "resumed": True,
        "hop_id": hop["hop_id"],
        "request_id": original_request_id,
    }
























def test_open_tab_reconciles_stale_web_command_snapshot_from_exact_ledger(tmp_path: Path):
    from dataclasses import replace
    from playwright_auto.cdpa_commands import WorkerCommand

    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-open-tab-stale-web"
    )
    provisional_url = "https://chatgpt.com/c/WEB:stale-open-tab"
    canonical_id = "11111111-1111-4111-8111-111111111111"
    canonical_url = f"https://chatgpt.com/c/{canonical_id}"
    hop["conversation_url"] = provisional_url
    state["roles"]["PLAN"].update(page_url=provisional_url, online=False)
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    command = WorkerCommand.create(
        origin="operator",
        action="open_tab",
        reason="recover stale provisional conversation",
        state=state,
        role="PLAN",
    )
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id=canonical_id).to_dict(),
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover stale provisional conversation",
            "status": "requested",
            "command": command.to_dict(),
        }
    ]
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id=receipt.binding.page_id,
        url=canonical_url,
        created=True,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.reopen_calls = []

        async def reopen(self, _state, _role, **kwargs):
            self.reopen_calls.append(kwargs)
            return acquired

    actions = Actions()
    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert actions.reopen_calls == [{"require_clean_ready": False, "foreground": True}]
    assert state["controls"][0]["status"] == "applied"
    assert state["status"] == "RUNNING"
    assert hop["receipt"]["conversation_id"] == canonical_id
    assert hop["conversation_url"] == canonical_url
    assert state["roles"]["PLAN"]["page_url"] == canonical_url
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_open_tab_rejects_conflicting_canonical_snapshot_after_ledger_reconcile(tmp_path: Path):
    from dataclasses import replace
    from playwright_auto.cdpa_commands import WorkerCommand

    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-open-tab-canonical-conflict"
    )
    provisional_url = "https://chatgpt.com/c/WEB:stale-conflict"
    hop["conversation_url"] = provisional_url
    state["roles"]["PLAN"].update(page_url=provisional_url, online=False)
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    command_state = json.loads(json.dumps(state))
    command_hop = _active_hop(command_state)
    command_hop["conversation_url"] = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
    command_state["roles"]["PLAN"]["page_url"] = command_hop["conversation_url"]
    command = WorkerCommand.create(
        origin="operator",
        action="open_tab",
        reason="conflicting canonical snapshot",
        state=command_state,
        role="PLAN",
    )
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id="11111111-1111-4111-8111-111111111111").to_dict(),
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "conflicting canonical snapshot",
            "status": "requested",
            "command": command.to_dict(),
        }
    ]

    class Actions:
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("canonical command conflict must fail before reopening")

    assert asyncio.run(worker._apply_control(state, Actions())) is True
    assert state["controls"][0]["status"] == "ineffective"
    assert "canonical" in str(state["controls"][0]["result"]).lower()
    assert state["status"] == "BLOCKED"
    assert _active_hop(state)["conversation_url"] == provisional_url


def test_advance_persists_ineffective_open_tab_before_later_requested_control(
    tmp_path: Path,
    monkeypatch,
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-btm-like-control-order")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="recorded PLAN tab is offline",
        active_action="blocked",
    )
    state["roles"]["PLAN"].update(
        page_id="old-plan-page",
        page_url="https://chatgpt.com/",
        online=False,
        status="pending",
    )
    hop["conversation_url"] = None
    store.save(path, state)
    worker.hydrate_runtime()

    worker.runtime_db.enqueue_command(
        command_id="cmd-btm-like-open",
        idempotency_key="btm-like-open",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={"action": "open_tab", "role": "PLAN", "reason": None},
    )
    assert worker.dispatch_command_once()["status"] == "running"
    worker.runtime_db.enqueue_command(
        command_id="cmd-btm-like-restart",
        idempotency_key="btm-like-restart",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={
            "action": "restart_role",
            "role": "PLAN",
            "reason": "bookkeeping conversation only",
        },
    )
    assert worker.dispatch_command_once()["status"] == "running"

    requested = store.load(path)
    assert [(item["action"], item["status"]) for item in requested["controls"][-2:]] == [
        ("open_tab", "requested"),
        ("restart_role", "requested"),
    ]

    class ControlActions:
        async def restart(self, *_args, **_kwargs):
            raise RoleOwnershipError("no exact conversation can be safely restarted")

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: ControlActions(),
    )

    result = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=list(worker.registry.tasks_by_id.values()),
        )
    )

    assert result is not None
    saved = store.load(path)
    open_control, restart_control = saved["controls"][-2:]
    assert open_control["status"] == "ineffective"
    assert open_control["command_state"] == "INEFFECTIVE"
    assert "exact saved ChatGPT conversation URL" in str(open_control["result"])
    assert restart_control["status"] == "requested"
    assert restart_control["command_state"] == "PENDING"
    assert worker.runtime_db.get_command("cmd-btm-like-open")["status"] == "failed"
    assert worker.runtime_db.get_command("cmd-btm-like-restart")["status"] == "running"

    second = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=list(worker.registry.tasks_by_id.values()),
        )
    )

    assert second is not None
    final = store.load(path)
    assert final["controls"][-1]["status"] == "rejected"
    assert final["controls"][-1]["command_state"] == "REJECTED"
    assert worker.runtime_db.get_command("cmd-btm-like-restart")["status"] == "failed"


def test_restart_role_from_report_repair_preserves_substantive_handoff(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-restart-report-repair-handoff")
    path = Path(state["manifest_path"])
    original = _active_hop(state)
    original["source_role"] = "REVIEW"
    original["target_role"] = "PLAN"
    original["handoff"] = ".plan/alpha/alpha-review_turn2_task-restart-report-repair-handoff.md"
    original["state"] = "routed"

    first_repair = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="PLAN",
        handoff="report repair",
        kind="report_repair",
        turn=int(original["turn"]),
        repair_attempt=1,
        validation_error="report file is missing",
    )
    first_repair["locked_route"] = "PAUSE"
    first_repair["locked_handoff"] = ".plan/alpha/alpha-plan_turn1_task-restart-report-repair-handoff.md"
    first_repair["state"] = "routed"
    second_repair = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="PLAN",
        handoff="report repair",
        kind="report_repair",
        turn=int(original["turn"]),
        repair_attempt=2,
        validation_error="report file is missing",
    )
    second_repair["locked_route"] = "PAUSE"
    second_repair["locked_handoff"] = first_repair["locked_handoff"]
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        active_role="PLAN",
        active_action="blocked",
        block_code="report_validation_exhausted",
        block_retryable=True,
        block_reason="report repair exhausted: report file is missing",
    )
    store.save(path, state)
    queued = store.request_control(
        path,
        "restart_role",
        role="PLAN",
        reason="replace inaccessible repair context",
        external_command_id="restart-report-repair-handoff-once",
    )

    assert asyncio.run(worker._apply_control(queued, FakeActions(), path)) is True

    child = _active_hop(queued)
    assert child["kind"] == "role_restart"
    assert child["parent_hop_id"] == second_repair["hop_id"]
    assert child["handoff"] == original["handoff"]


def test_blocked_task_does_not_advance_without_explicit_control(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "manual inspection required"
    store.save(path, state)

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "BLOCKED"
    assert result["active_hop_id"] == 1
    assert _active_hop(result)["state"] == "pre_send"
    assert result["block_reason"] == "manual inspection required"














def _prepare_sent_waiting_task(
    tmp_path: Path,
    *,
    task_id: str,
    report_mode: str = "file",
    repository=None,
):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id=task_id,
        report_mode=report_mode,
        repository=repository,
    )
    from dataclasses import replace
    worker.config = replace(
        worker.config, response_stream_status_terminal_settle_seconds=0.0
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    prompt = hop["prompt"]
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=prompt,
        prompt_sha256=prompt_digest(prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="t1",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=prompt,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    sent_at = datetime.now(timezone.utc)
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = sent_at.isoformat()
    worker._start_wait_budget_from_sent(hop)
    state["roles"]["PLAN"]["page_id"] = "page-alpha-plan"
    state = store.save(path, state)
    hop = _active_hop(state)
    return store, state, worker, path, hop, receipt, sent_at


def _enable_backend_wait_identity(store, state, path, hop, receipt, *, conversation_id="conversation-1"):
    from dataclasses import replace

    enriched = replace(receipt, conversation_id=conversation_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{conversation_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    hop["timestamps"]["sent_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    state = store.save(path, state)
    return state, _active_hop(state), enriched


def _backend_graph(user_id: str, assistant_id: str, text: str):
    return {
        "current_node": assistant_id,
        "mapping": {
            user_id: {
                "id": user_id,
                "parent": None,
                "message": {
                    "id": user_id,
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["prompt"]},
                },
            },
            assistant_id: {
                "id": assistant_id,
                "parent": user_id,
                "message": {
                    "id": assistant_id,
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": [text]},
                },
            },
        },
    }


def test_unresolved_complete_accepts_terminal_continuation_before_final_block(
    tmp_path: Path,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-terminal-continuation-arrives"
    )
    state, hop, receipt = _enable_backend_wait_identity(
        store, state, path, hop, receipt
    )
    response = MessageSnapshot(
        "assistant",
        "assistant-terminal",
        "assistant-terminal-turn",
        json.dumps(
            {
                "route": "REVIEW",
                "handoff": ".plan/alpha/alpha-plan_turn1_task-terminal-continuation-arrives.md",
            }
        ),
        ("copy-turn-action-button",),
    )
    snapshot = send_snapshot(
        messages=(
            MessageSnapshot("user", receipt.user_message_id, receipt.user_turn_id, receipt.prompt, ()),
            response,
        ),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    snapshot = SimpleNamespace(
        **{
            **snapshot.__dict__,
            "composer_empty": True,
            "manual_input_pending": False,
            "response_activity_text": response.text,
            "response_activity_structure": "full",
            "response_activity_turn_id": None,
            "response_activity_length": len(response.text),
        }
    )

    hop["wait"].update(
        result_seen_key=worker_module.hashlib.sha256(response.text.encode()).hexdigest(),
        result_seen_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        result_samples=1,
    )
    state = store.save(path, state)
    hop = _active_hop(state)

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def refresh(self, *_args, **_kwargs):
            raise AssertionError("a persisted refresh must not be repeated")

    hop["wait"].update(
        completion_mode="dom_fallback",
        backend_fallback_category="graph_not_ready",
        dom_fallback_ready_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
        refresh_count=1,
        terminal_continuation_unresolved={
            "request_id": hop["request_id"],
            "started_at": utc_now(),
            "refresh_baseline": 0,
            "block_ready_at": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        },
    )

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert state["status"] == "RUNNING"
    assert hop["state"] == "routed"
    assert hop["response"] == response.text
    assert _active_hop(state)["hop_id"] != hop["hop_id"]
    assert hop["route"] == "REVIEW"
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert record.attempts == 1
    assert record.status is RequestStatus.COMPLETED
    completed_receipt = SendReceipt.from_dict(record.receipt)
    assert completed_receipt.accepted_via == receipt.accepted_via
    assert completed_receipt.user_message_id == receipt.user_message_id
    assert record.response == response.to_dict()


def test_completed_semantic_assistant_preserves_recovery_send_provenance(tmp_path: Path):
    _store, _state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-semantic-assistant-provenance"
    )
    response = MessageSnapshot(
        "assistant",
        "DOM:assistant:0",
        None,
        "completed semantic response",
        (),
    )
    hop["response"] = response.text
    hop["response_record"] = response.to_dict()

    worker._complete_request_response(hop)

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert record is not None and record.status is RequestStatus.COMPLETED
    completed_receipt = SendReceipt.from_dict(record.receipt)
    assert completed_receipt.accepted_via == receipt.accepted_via
    assert completed_receipt.user_message_id == receipt.user_message_id
    assert record.response == response.to_dict()


def _mark_completed_refresh(hop: dict, *, seconds_ago: int) -> None:
    finished = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    hop["wait"]["refresh_count"] = int(hop["wait"].get("refresh_count") or 0) + 1
    hop["wait"]["last_refresh_at"] = finished.isoformat()
    hop["wait"]["last_refresh_result"] = {
        "status": "completed",
        "finished_at": finished.isoformat(),
    }


def test_normal_wait_routes_valid_local_file_response_without_repair(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-normal-wait-path-only"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = b"# TEST report\n\n## Evidence\n- Inspected `report_path=.plan/alpha/alpha-plan_turn1_task-normal-wait-path-only.md`.\n- `samples=2` -> stable route/report evidence verified.\n"
    report.write_bytes(report_bytes)
    response = MessageSnapshot(
        "assistant",
        "a-normal-path-only",
        "ta-normal-path-only",
        json.dumps({"route": "TEST", "handoff": handoff}),
        ("copy-turn-action-button",),
    )
    snapshot = send_snapshot(
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), response),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    hop["wait"].update(
        result_seen_key=worker_module.hashlib.sha256(response.text.encode()).hexdigest(),
        result_seen_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        result_samples=1,
    )

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/normal-path-only", False, False
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert hop["state"] == "routed"
    assert hop["response"] == response.text
    assert hop.get("validation_error") is None

    child = _active_hop(state)
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert hop["state"] == "routed"
    assert hop["report_path"] == handoff
    assert hop["report_sha256"] == worker_module.hashlib.sha256(report_bytes).hexdigest()
    assert hop["report_size"] == len(report_bytes)
    assert child["kind"] == "handoff"
    assert child["target_role"] == "TEST"
    assert child["handoff"] == handoff
    assert all(item.get("kind") != "route_repair" for item in state["hops"])
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


def test_waiting_requires_valid_route_report_and_two_samples_before_hop_response(tmp_path: Path):
    from dataclasses import replace

    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-valid-gate"
    )
    worker.config = replace(worker.config, response_poll_ms=5000)
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-valid-gate.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "# PLAN report\n\n## Evidence\n- Inspected `report_path=.plan/alpha/alpha-plan_turn1_task-valid-gate.md`.\n- `samples=2`; `controller_reason=stable_current_result` -> route/report candidate verified.\n",
        encoding="utf-8",
    )
    response = MessageSnapshot(
        "assistant",
        "a-valid",
        "ta-valid",
        '{"route":"TEST","handoff":".plan/alpha/alpha-plan_turn1_task-valid-gate.md"}',
        ("copy-turn-action-button",),
    )
    snapshot = send_snapshot(
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), response),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert hop["state"] == "waiting"
    assert hop["wait"]["result_samples"] == 1
    assert hop["wait"]["controller_reason"] == "result_stability"
    hop["wait"]["result_seen_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).isoformat()
    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert hop["wait"]["result_samples"] == 2
    assert hop["wait"]["controller_reason"] == "valid_bound_result"
    assert hop["state"] == "routed"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED
    assert _active_hop(state)["target_role"] == "TEST"


























def test_cdp_disconnect_does_not_convert_inflight_task_to_blocked(tmp_path: Path, monkeypatch):
    store, state, worker, path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-disconnect-preserve"
    )
    before = path.read_bytes()
    TargetClosedError = type("TargetClosedError", (RuntimeError,), {})

    async def disconnected_wait(*_args, **_kwargs):
        raise TargetClosedError("Target page, context or browser has been closed")

    monkeypatch.setattr(worker, "_waiting", disconnected_wait)

    with pytest.raises(TargetClosedError):
        asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert path.read_bytes() == before
    current = store.load(path)
    current_hop = _active_hop(current)
    assert current["status"] == "RUNNING"
    assert current_hop["state"] == "waiting"
    assert current_hop["request_id"] == hop["request_id"]
    assert current.get("block_code") is None









def _inline_response(route="DONE", body="# Inline report\n\nEvidence."):
    return f'{body}\n\n```json\n{{"route":"{route}","handoff":"INLINE"}}\n```'




def test_legacy_inline_response_materializes_once_then_switches_to_file(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-inline-materialize")
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = ".plan/alpha/alpha-plan_turn1_task-inline-materialize.md"
    hop["response"] = _inline_response(route="DEV")
    hop["state"] = "responded"

    worker._responded(state, hop)

    expected = (tmp_path / hop["expected_report_path"]).resolve()
    assert expected.read_text(encoding="utf-8") == "# Inline report\n\nEvidence."
    assert hop["report_path"] == str(expected)
    assert hop["report_size"] == len(expected.read_bytes())
    assert hop["report_sha256"] == worker_module.hashlib.sha256(expected.read_bytes()).hexdigest()
    assert len(state["reports"]) == 1
    assert state["reports"][0]["path"] == str(expected)
    assert state["options"]["report_mode"] == "file"
    next_hop = _active_hop(state)
    assert next_hop["target_role"] == "DEV"
    assert next_hop["handoff"] == hop["expected_report_path"]





















def test_legacy_inline_invalid_route_repairs_in_file_mode(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-inline-repair",
        roles=("PLAN", "REVIEW"),
    )
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = (
        ".plan/alpha/alpha-plan_turn1_task-legacy-inline-repair.md"
    )
    hop["response"] = _inline_response(route="DEV")
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["options"]["report_mode"] == "file"
    assert state["reports"] == []
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert '"handoff":"INLINE"' not in repair["prompt"]
    assert repair["expected_report_path"] == hop["expected_report_path"]


def test_pre_send_with_legacy_receipt_fails_closed_without_rebuilding_prompt(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-receipt"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-pre-send-legacy-receipt",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    original_prompt = "legacy accepted file prompt"
    receipt = SendReceipt(
        prompt=original_prompt,
        prompt_sha256=prompt_digest(original_prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="legacy_receipt",
        session_id_before=None,
        user_message_id=None,
        user_turn_id=None,
    )
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    hop["receipt"] = receipt.to_dict()
    state = store.save(path, state)
    hop = _active_hop(state)

    class NoActions:
        async def acquire(self, _state, _role):
            raise AssertionError("pre_send receipt conflict must not acquire a tab")

    asyncio.run(worker._pre_send(state, hop, NoActions()))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "pre_send_request_recovery_required"
    assert state["options"]["report_mode"] == "file"
    assert hop["state"] == "pre_send"
    assert hop["prompt"] == original_prompt
    assert hop["receipt"] == receipt.to_dict()
    assert hop["receipt"]["attempts"] == 1
    assert not Path(hop["ledger_path"]).exists()


def test_pre_send_exact_pristine_durable_request_resumes_same_identity(
    tmp_path: Path,
):
    config, _store, state, worker = setup_task(
        tmp_path,
        task_id="task-pre-send-pristine-durable",
    )
    hop = _active_hop(state)
    role = str(hop["target_role"])
    prompt = "same durable request before send"
    hop["prompt"] = prompt
    hop["prompt_sha256"] = worker_module._sha(prompt)
    source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": hop["hop_id"],
        "manifest": state["manifest_path"],
    }
    role_prompt_hash = worker_module._sha(
        str(worker_module.task_workflow_definitions(state, config)[role]["system_prompt"])
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=str(hop["physical_role"]),
        prompt=prompt,
        source_context=source_context,
        role_prompt_hash=role_prompt_hash,
        request_id=hop["request_id"],
        render_request_marker=False,
    )

    assert record.status is RequestStatus.NEW
    assert record.attempts == 0
    assert worker._pre_send_request_identity_is_safe(state, hop) is True
    assert state["status"] != "BLOCKED"

    ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    assert worker._pre_send_request_identity_is_safe(state, hop) is True
    assert state["status"] != "BLOCKED"


@pytest.mark.parametrize("live_kind", ["manual_text", "manual_attachment"])
def test_pristine_durable_pre_send_existing_owned_page_preserves_live_composer(
    tmp_path: Path, live_kind: str,
):
    config, _store, state, worker = setup_task(
        tmp_path,
        task_id=f"task-pre-send-preserve-{live_kind}",
    )
    hop = _active_hop(state)
    role = str(hop["target_role"])
    prompt = "same durable request before send"
    hop["prompt"] = prompt
    hop["prompt_sha256"] = worker_module._sha(prompt)
    source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": hop["hop_id"],
        "manifest": state["manifest_path"],
    }
    role_prompt_hash = worker_module._sha(
        str(worker_module.task_workflow_definitions(state, config)[role]["system_prompt"])
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=str(hop["physical_role"]),
        prompt=prompt,
        source_context=source_context,
        role_prompt_hash=role_prompt_hash,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    live = {
        "text": "OPERATOR MANUAL DRAFT" if live_kind == "manual_text" else "",
        "attachments": ("operator-manual.pdf",) if live_kind == "manual_attachment" else (),
    }
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="owned-page",
        url="https://chatgpt.com/",
        created=False,
        new_chat=False,
    )

    class PreserveActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.clean_acquire_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def acquire(self, _state, _role, *, clean_for_send=False):
            self.clean_acquire_calls += 1
            if clean_for_send:
                live["text"] = ""
                live["attachments"] = ()
            return acquired

    actions = PreserveActions()
    worker.role_controller.run = AsyncMock(
        return_value=SimpleNamespace(reason="composer_pending")
    )

    asyncio.run(worker._pre_send(state, hop, actions))

    current = ledger.get(record.request_id)
    assert actions.clean_acquire_calls == 0
    assert live["text"] == (
        "OPERATOR MANUAL DRAFT" if live_kind == "manual_text" else ""
    )
    assert live["attachments"] == (
        ("operator-manual.pdf",) if live_kind == "manual_attachment" else ()
    )
    assert current is not None
    assert current.request_id == record.request_id
    assert current.attempts == 0
    assert current.status is RequestStatus.NEW
    assert hop["state"] == "pre_send"


def test_pre_send_with_durable_record_fails_closed_without_rebuilding_prompt(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-durable-record"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-pre-send-durable-record",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    original_prompt = "durable accepted file prompt"
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    binding = PageBinding("page-alpha-plan", "alpha-plan")
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=original_prompt,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=binding,
        baseline=baseline,
    )
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    state = store.save(path, state)
    hop = _active_hop(state)

    class NoActions:
        async def acquire(self, _state, _role):
            raise AssertionError("pre_send ledger conflict must not acquire a tab")

    asyncio.run(worker._pre_send(state, hop, NoActions()))

    current = ledger.get(record.request_id)
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "pre_send_request_recovery_required"
    assert state["options"]["report_mode"] == "file"
    assert hop["state"] == "pre_send"
    assert hop["prompt"] == original_prompt
    assert hop.get("receipt") is None
    assert current is not None
    assert current.rendered_prompt == original_prompt
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1


def test_relocated_prompt_rebind_crash_reconciles_persisted_intent_and_new_ledger(tmp_path: Path):
    config, store, state, worker = setup_task(tmp_path, task_id="task-rebind-crash")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    role = str(hop["target_role"])
    source_context = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": hop["hop_id"],
        "manifest": state["manifest_path"],
    }
    role_prompt_hash = worker_module._sha(
        str(worker_module.task_workflow_definitions(state, config)[role]["system_prompt"])
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.begin(
        role=str(hop["physical_role"]), prompt="old prompt", request_id=hop["request_id"],
        source_context=source_context, role_prompt_hash=role_prompt_hash, render_request_marker=False,
    )
    intended_prompt = "rebuilt prompt after relocation"
    ledger.rebind_pristine(
        hop["request_id"], role=str(hop["physical_role"]), prompt=intended_prompt,
        source_context=source_context, role_prompt_hash=role_prompt_hash,
    )
    hop["prompt"] = "old prompt"
    hop["prompt_sha256"] = worker_module._sha("old prompt")
    hop["rebuild_prompt_after_relocation"] = True
    hop["prompt_rebind_intent"] = {
        "request_id": hop["request_id"],
        "role": str(hop["physical_role"]),
        "prompt": intended_prompt,
        "prompt_sha256": worker_module._sha(intended_prompt),
        "source_context": source_context,
        "role_prompt_hash": role_prompt_hash,
    }
    state = store.save(path, state)
    hop = _active_hop(state)

    assert worker._pre_send_request_identity_is_safe(state, hop) is True
    assert state["status"] != "BLOCKED"


def test_legacy_inline_pre_send_switches_to_file_before_prompt(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-pre-send"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-pre-send",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    state["options"]["report_mode"] = "inline"
    state = store.save(path, state)
    baseline = json.loads(json.dumps(state))
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert state["options"]["report_mode"] == "file"
    assert "Before replying, write a non-empty report to exactly: .plan/alpha/alpha-plan_turn1_task-legacy-pre-send.md" in hop["prompt"]
    assert '"handoff":".plan/alpha/alpha-plan_turn1_task-legacy-pre-send.md"' in hop["prompt"]
    assert '"handoff":"INLINE"' not in hop["prompt"]
    assert hop["expected_report_path"] == ".plan/alpha/alpha-plan_turn1_task-legacy-pre-send.md"
    persisted = worker._persist_transport_result(path, baseline, state)
    assert persisted["options"]["report_mode"] == "file"
    assert _active_hop(persisted)["prompt"] == hop["prompt"]


def test_accepted_legacy_inline_hop_drains_once_to_execution_repo_then_file(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-accepted"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-accepted-inline",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = (
        ".plan/alpha/alpha-plan_turn1_task-legacy-accepted-inline.md"
    )
    original_request_id = hop["request_id"]
    original_prompt = "legacy accepted inline prompt with handoff INLINE"
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=original_prompt,
        prompt_sha256=prompt_digest(original_prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="t1",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=original_prompt,
        request_id=original_request_id,
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = datetime.now(timezone.utc).isoformat()
    worker._start_wait_budget_from_sent(hop)
    state["roles"]["PLAN"]["page_id"] = "page-alpha-plan"
    state = store.save(path, state)
    hop = _active_hop(state)

    body = "# Legacy accepted inline\n\nMaterialize exactly once."
    response = MessageSnapshot(
        "assistant",
        "a-legacy-inline",
        "ta-legacy-inline",
        _inline_response(route="DEV", body=body),
        ("copy-turn-action-button",),
    )
    snapshot = send_snapshot(
        messages=(MessageSnapshot("user", "u1", "t1", original_prompt, ()), response),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    hop["wait"].update(
        result_seen_key=worker_module.hashlib.sha256(response.text.encode()).hexdigest(),
        result_seen_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        result_samples=1,
    )
    state = store.save(path, state)
    hop = _active_hop(state)

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/legacy-inline",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    waiting_baseline = json.loads(json.dumps(state))
    asyncio.run(worker._waiting(state, hop, Actions(), path))
    state = worker._persist_transport_result(path, waiting_baseline, state)
    completed_hop = state["hops"][0]
    assert completed_hop["state"] == "routed"
    assert state["options"]["report_mode"] == "file"
    report_path = Path(completed_hop["report_path"])

    assert report_path == (
        execution_repository
        / ".plan"
        / "alpha"
        / "alpha-plan_turn1_task-legacy-accepted-inline.md"
    ).resolve()
    assert report_path.read_text(encoding="utf-8") == body
    assert not (
        tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-legacy-accepted-inline.md"
    ).exists()
    assert completed_hop["prompt"] == original_prompt
    assert completed_hop["request_id"] == original_request_id
    assert completed_hop["receipt"] == receipt.to_dict()
    original_record = RequestLedger(completed_hop["ledger_path"]).get(original_request_id)
    assert original_record is not None
    assert original_record.attempts == 1
    assert original_record.status is RequestStatus.COMPLETED
    assert state["options"]["report_mode"] == "file"

    next_hop = _active_hop(state)
    assert next_hop["target_role"] == "DEV"
    asyncio.run(worker._pre_send(state, next_hop, FakeActions()))
    assert state["options"]["report_mode"] == "file"
    assert '"handoff":"INLINE"' not in next_hop["prompt"]
    assert next_hop["expected_report_path"] == (
        ".plan/alpha/alpha-dev_turn1_task-legacy-accepted-inline.md"
    )
    final_record = RequestLedger(completed_hop["ledger_path"]).get(original_request_id)
    assert final_record is not None
    assert final_record.attempts == 1
    assert final_record.status is RequestStatus.COMPLETED


def test_accepted_legacy_inline_conflict_recovers_same_response_without_replay(
    tmp_path: Path,
):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-legacy-conflict"
    execution_repository.mkdir()
    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-legacy-inline-conflict",
        repository=execution_repository,
    )
    path = Path(state["manifest_path"])
    state["options"]["report_mode"] = "inline"
    hop = _active_hop(state)
    hop["expected_report_path"] = (
        ".plan/alpha/alpha-plan_turn1_task-legacy-inline-conflict.md"
    )
    original_request_id = hop["request_id"]
    original_prompt = "legacy accepted inline conflict prompt with handoff INLINE"
    hop["prompt"] = original_prompt
    hop["prompt_sha256"] = prompt_digest(original_prompt)
    receipt = SendReceipt(
        prompt=original_prompt,
        prompt_sha256=prompt_digest(original_prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u-conflict",
        user_turn_id="t-conflict",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=original_prompt,
        request_id=original_request_id,
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=receipt.baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    body = "# Legacy accepted inline conflict\n\nAccepted report body."
    response_text = _inline_response(route="DEV", body=body)
    response = MessageSnapshot(
        "assistant",
        "a-conflict",
        "ta-conflict",
        response_text,
        (),
    )
    hop["receipt"] = receipt.to_dict()
    hop["response"] = response_text
    hop["response_sha256"] = worker_module.hashlib.sha256(
        response_text.encode("utf-8")
    ).hexdigest()
    hop["response_record"] = response.to_dict()
    hop["message_identity"] = {
        "message_id": response.message_id,
        "turn_id": response.turn_id,
    }
    hop["state"] = "responded"
    state = store.save(path, state)
    hop = _active_hop(state)

    report_path = (execution_repository / hop["expected_report_path"]).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_bytes = b"# Orphan steering artifact\n\nDifferent unowned bytes.\n"
    report_path.write_bytes(orphan_bytes)

    worker._responded(state, hop)
    state = store.save(path, state)
    blocked_hop = _active_hop(state)
    original_record = RequestLedger(blocked_hop["ledger_path"]).get(original_request_id)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert report_path.read_bytes() == orphan_bytes
    assert len(state["hops"]) == 1
    assert blocked_hop["state"] == "responded"
    assert blocked_hop["request_id"] == original_request_id
    assert blocked_hop["prompt"] == original_prompt
    assert blocked_hop["receipt"] == receipt.to_dict()
    assert original_record is not None
    assert original_record.attempts == 1
    assert original_record.status is RequestStatus.SENT
    mode_after_conflict = state["options"]["report_mode"]

    archive_path = execution_repository / ".recovery-orphans" / "legacy-inline-conflict.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.replace(archive_path)
    report_path.write_text(body, encoding="utf-8")

    recovered = store.load(path)
    recovered_hop = _active_hop(recovered)
    worker._responded(recovered, recovered_hop)
    recovered = store.save(path, recovered)
    completed_hop = recovered["hops"][0]
    completed_record = RequestLedger(completed_hop["ledger_path"]).get(original_request_id)

    assert mode_after_conflict == "inline"
    assert archive_path.read_bytes() == orphan_bytes
    assert Path(completed_hop["report_path"]) == report_path
    assert report_path.read_text(encoding="utf-8") == body
    assert completed_hop["state"] == "routed"
    assert completed_hop["route"] == "DEV"
    assert completed_hop["request_id"] == original_request_id
    assert completed_hop["prompt"] == original_prompt
    assert completed_hop["receipt"] == receipt.to_dict()
    assert completed_record is not None
    assert completed_record.attempts == 1
    assert completed_record.status is RequestStatus.COMPLETED
    assert recovered["options"]["report_mode"] == "file"
    assert len(recovered["hops"]) == 2
    next_hop = _active_hop(recovered)
    assert next_hop["kind"] == "handoff"
    assert next_hop["target_role"] == "DEV"


def test_cross_workspace_local_file_report_requires_execution_repository_artifact(tmp_path: Path):
    execution_repository = tmp_path.parent / f"{tmp_path.name}-worker-execution"
    execution_repository.mkdir()
    store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-cross-workspace-file",
        repository=execution_repository,
    )
    original_request_id = hop["request_id"]
    handoff = str(hop["expected_report_path"])
    report = execution_repository / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = b"# PLAN report\n\n## Evidence\n- Inspected `report_path=.plan/alpha/alpha-plan_turn1_task-cross-workspace-file.md` in the execution repository.\n- `checks=1` -> cross-workspace local artifact validation passed.\n"
    report.write_bytes(report_bytes)
    response = MessageSnapshot(
        "assistant",
        "a-cross-workspace",
        "ta-cross-workspace",
        json.dumps({"route": "DEV", "handoff": handoff}),
        ("copy-turn-action-button",),
    )
    snapshot = send_snapshot(
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), response),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    hop["wait"].update(
        result_seen_key=worker_module.hashlib.sha256(response.text.encode()).hexdigest(),
        result_seen_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        result_samples=1,
    )

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/cross-workspace",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    assert state["options"]["report_mode"] == "file"
    assert '"handoff":"INLINE"' not in hop["prompt"]
    assert report.exists()
    assert not (tmp_path / handoff).exists()
    assert RequestLedger(hop["ledger_path"]).get(original_request_id).attempts == 1

    asyncio.run(worker._waiting(state, hop, Actions(), Path(state["manifest_path"])))
    assert hop["state"] == "routed"

    child = _active_hop(state)
    assert hop["report_path"] == handoff
    assert hop["report_size"] == len(report_bytes)
    assert hop["report_sha256"] == worker_module.hashlib.sha256(report_bytes).hexdigest()
    assert child["target_role"] == "DEV"
    assert child["handoff"] == handoff
    assert hop["request_id"] == original_request_id
    record = RequestLedger(hop["ledger_path"]).get(original_request_id)
    assert record.attempts == 1
    assert record.status is RequestStatus.COMPLETED
    assert all(item.get("kind") != "route_repair" for item in state["hops"])

    persisted = store.save(state["manifest_path"], state)
    assert persisted["active_role"] == "DEV"
    assert len(persisted["hops"]) == 2
    assert persisted["hops"][0]["request_id"] == original_request_id
    assert persisted["hops"][0]["report_path"] == handoff
    assert persisted["hops"][1]["handoff"] == handoff


class ExplodingBrowserContext:
    @property
    def pages(self):
        raise AssertionError("dependency WAITING/release must not inspect browser pages")


def test_dependency_waiting_does_not_construct_browser_actions_or_rewrite(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    store.create_task("Parent", requested_team="parent", task_id="task-parent", governing_objective="Parent", governing_directives=())
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
        governing_objective="Child",
        governing_directives=(),
    )
    path = Path(child["manifest_path"])
    before = path.read_bytes()
    before_events = list(child["dependency_events"])

    result = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))

    assert result["status"] == "WAITING"
    assert result["waiting"]["waiting_on"] == ["task-parent"]
    assert result["hops"][0]["state"] == "pre_send"
    assert path.read_bytes() == before
    assert result["dependency_events"] == before_events


def test_dependency_release_is_one_durable_transition_without_browser_action(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent", governing_objective="Parent", governing_directives=())
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
        governing_objective="Child",
        governing_directives=(),
    )
    store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )

    path = Path(child["manifest_path"])
    result = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))
    assert result["status"] == "INBOX"
    assert result["kanban_column"] == "INBOX"
    assert result["active_action"] == "queued"
    assert result["waiting"] == {
        "reason": None,
        "waiting_on": [],
        "stopped": [],
        "missing": [],
        "since": None,
    }
    assert result["hops"][0]["state"] == "pre_send"
    assert result["active_hop_id"] == 1
    assert result["dependency_events"][-1]["status"] == "RELEASED"

    persisted = path.read_bytes()
    # The next poll would be allowed to acquire PLAN, so only verify the release
    # transition itself is stable through a worker restart/load.
    restarted = CDPAWorker(config, store=store)
    loaded = store.load(path)
    assert loaded["status"] == "INBOX"
    assert path.read_bytes() == persisted
    assert restarted.store is store




def test_same_team_queue_releases_oldest_ready_task_before_browser_access(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner", governing_objective="owner", governing_directives=())
    queued_b = store.create_task("queued b", reuse_team="alpha", task_id="task-b", governing_objective="queued b", governing_directives=())
    queued_a = store.create_task("queued a", reuse_team="alpha", task_id="task-a", governing_objective="queued a", governing_directives=())
    same_time = "2026-07-23T00:00:00+00:00"
    for queued in (queued_a, queued_b):
        store.update(
            queued["manifest_path"],
            lambda state: {
                **state,
                "created_at": same_time,
                "queue": {**state["queue"], "enqueued_at": same_time},
            },
        )
    store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )
    worker = CDPAWorker(config, store=store)

    later = asyncio.run(worker.advance(queued_b["manifest_path"], ExplodingBrowserContext()))
    first = asyncio.run(worker.advance(queued_a["manifest_path"], ExplodingBrowserContext()))

    assert later["status"] == "WAITING"
    assert later["waiting"]["blocked_by_task_id"] == "task-a"
    assert first["status"] == "INBOX"
    assert first["queue"]["released_at"]
    assert first["reusable_teams"] == ["alpha"]

    still_waiting = asyncio.run(
        worker.advance(queued_b["manifest_path"], ExplodingBrowserContext())
    )
    assert still_waiting["status"] == "WAITING"
    assert still_waiting["waiting"]["blocked_by_task_id"] == "task-a"

    store.update(
        queued_a["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )
    released = asyncio.run(
        worker.advance(queued_b["manifest_path"], ExplodingBrowserContext())
    )
    assert released["status"] == "INBOX"
    assert released["queue_events"][-1]["status"] == "RELEASED"





















































def test_cdpa_uploads_once_per_role_conversation_generation(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "context.md"
    attachment.write_text("stable context", encoding="utf-8")
    state = store.create_task(
        "Analyze context",
        requested_team="alpha",
        task_id="task-upload-generation",
        upload_paths=[attachment],
        governing_objective="Analyze context",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)
    captured_files: list[tuple[str, ...]] = []

    class Client:
        binding = PageBinding("page-alpha-plan", "alpha-plan")

        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/upload",
                url="https://chatgpt.com/c/upload",
            )

    client = Client()

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return AcquiredRole(
                client=client,
                page_id="page-alpha-plan",
                url="https://chatgpt.com/c/upload",
                created=False,
                new_chat=False,
            )

    class FakeDurableSendBlock:
        def __init__(self, _prompt, *, files=(), **_kwargs):
            captured_files.append(tuple(str(path) for path in files))

        async def run(self, _context):
            return {
                "receipt": {"prompt_sha256": "a" * 64},
                "record": {"accepted_at": datetime.now(timezone.utc).timestamp()},
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeDurableSendBlock)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    assert str(attachment.resolve()) not in str(hop["prompt"])
    assert "stable context" not in str(hop["prompt"])
    asyncio.run(worker._sending(state, hop, Actions()))

    assert captured_files == [(str(attachment.resolve()),)]
    assert state["roles"]["PLAN"]["attachments_uploaded_generation"] == 0

    next_hop = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="PLAN",
        handoff="next turn",
    )
    asyncio.run(worker._pre_send(state, next_hop, FakeActions()))
    asyncio.run(worker._sending(state, next_hop, Actions()))

    assert captured_files[-1] == ()
    assert state["roles"]["PLAN"]["attachments_uploaded_generation"] == 0

    state["roles"]["PLAN"]["conversation_generation"] = 1
    fresh_hop = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="PLAN",
        handoff="fresh generation",
    )
    asyncio.run(worker._pre_send(state, fresh_hop, FakeActions()))
    asyncio.run(worker._sending(state, fresh_hop, Actions()))

    assert captured_files[-1] == (str(attachment.resolve()),)
    assert state["roles"]["PLAN"]["attachments_uploaded_generation"] == 1

    dev_hop = worker._append_hop(
        state,
        source_role="PLAN",
        target_role="DEV",
        handoff="independent DEV context",
    )
    asyncio.run(worker._pre_send(state, dev_hop, FakeActions()))
    asyncio.run(worker._sending(state, dev_hop, Actions()))

    assert captured_files[-1] == (str(attachment.resolve()),)
    assert state["roles"]["DEV"]["attachments_uploaded_generation"] == 0














def test_worker_operational_failures_are_sanitized_before_manifest_and_dashboard(
    tmp_path: Path,
):
    from playwright_auto.cdpa_projection import build_task_projection

    _config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-worker-credential-boundary",
    )
    path = Path(state["manifest_path"])
    secret_path = "worker-path-secret-token"
    secret_query = "worker-query-secret"
    secret_bearer = "worker-bearer-secret"
    sensitive_url = (
        f"https://api.example.invalid/webhooks/{secret_path}/status"
        f"?access_token={secret_query}#worker-fragment-secret"
    )
    error = TimeoutError(
        f"GET {sensitive_url} timed out; Authorization: Bearer {secret_bearer}"
    )

    requested = store.request_control(path, "pause", reason="credential boundary fixture")
    control_id = requested["controls"][-1]["control_id"]

    saved = store.update(
        path,
        lambda current: worker._block(
            current,
            error,
            code="role_offline",
            retryable=False,
        ),
    )
    saved = store.reject_control(
        path,
        control_id,
        f"{type(error).__name__}: {error}",
        action="pause",
    )
    payload = build_task_projection(saved, tasks=[saved]).detail
    manifest_text = path.read_text(encoding="utf-8")
    dashboard_text = json.dumps(payload, ensure_ascii=False)

    for secret in (
        secret_path,
        secret_query,
        secret_bearer,
        "worker-fragment-secret",
    ):
        assert secret not in manifest_text
        assert secret not in dashboard_text
    assert "[REDACTED]" in manifest_text
    assert "[REDACTED]" in dashboard_text
    assert secret_path not in str(saved["block_reason"])
    assert secret_path not in str(saved["roles"][saved["active_role"]]["last_error"])
    assert all(secret_path not in str(item) for item in saved["errors"])
    assert all(secret_path not in str(item) for item in _active_hop(saved)["errors"])
    assert secret_path not in str(saved["controls"][-1]["result"])
    assert all(secret_path not in item["message"] for item in payload["timeline"])












def test_runtime_worker_discovers_once_and_idle_cycles_do_not_scan(tmp_path: Path, monkeypatch):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-runtime-once")
    discover_calls = 0
    recover_calls = 0
    original_discover = store.discover_with_errors
    original_recover = store.recover_phase4_replacement

    def counted_discover():
        nonlocal discover_calls
        discover_calls += 1
        return original_discover()

    def counted_recover():
        nonlocal recover_calls
        recover_calls += 1
        return original_recover()

    async def inert_advance(path, _browser_context, *, scheduling_tasks=None):
        assert scheduling_tasks is not None
        return store.load(path)

    class InertMaintainers:
        async def advance(self, tasks, _browser_context):
            assert len(tasks) == 1
            return False

    monkeypatch.setattr(store, "discover_with_errors", counted_discover)
    monkeypatch.setattr(store, "recover_phase4_replacement", counted_recover)
    monkeypatch.setattr(worker, "advance", inert_advance)
    worker.maintainers = InertMaintainers()

    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert discover_calls == 1
    assert recover_calls == 1


def test_runtime_worker_hung_dom_probe_revisits_expired_second_task_past_bound(
    tmp_path: Path,
    monkeypatch,
):
    config, store, first, worker = setup_task(tmp_path, task_id="task-runtime-hung-dom-a")
    second = store.create_task(
        "second task",
        requested_team="beta",
        task_id="task-runtime-hung-dom-b",
        governing_objective="second task",
        governing_directives=(),
    )
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(first, now=0.0)
    worker.registry.update_task(second, now=0.0)

    class HangingPage:
        url = "https://chatgpt.com/c/hung"

        async def evaluate(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    timeout_seconds = 0.03
    hung_client = ChatGPTPage(HangingPage(), timeout_ms=int(timeout_seconds * 1000))
    receipt = SendReceipt(
        prompt="probe",
        prompt_sha256="probe-sha",
        binding=PageBinding("page-plan", "PLAN"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="test",
        session_id_before=None,
    )
    calls = {first["task_id"]: 0, second["task_id"]: 0}
    reconciliations = 0
    second_deadline = time.monotonic() + timeout_seconds / 2

    async def bounded_advance(path, _browser_context, *, scheduling_tasks=None):
        nonlocal reconciliations
        assert scheduling_tasks is not None
        state = store.load(path)
        task_id = state["task_id"]
        calls[task_id] += 1
        if task_id == first["task_id"]:
            with pytest.raises(TimeoutError):
                await hung_client.wait_snapshot(receipt)
        elif calls[task_id] >= 2 and time.monotonic() >= second_deadline:
            reconciliations += 1
        return state

    class InertMaintainers:
        async def advance(self, _tasks, _browser_context):
            return False

    monkeypatch.setattr(worker, "advance", bounded_advance)
    worker.maintainers = InertMaintainers()
    started = time.monotonic()

    async def scenario():
        await worker.run_once(SimpleNamespace(pages=[]))
        await worker.run_once(SimpleNamespace(pages=[]))

    asyncio.run(scenario())
    elapsed = time.monotonic() - started
    bound = 2 * timeout_seconds + 2 * worker_module.MINIMUM_DEADLINE_SECONDS

    assert calls[second["task_id"]] >= 2
    assert reconciliations == 1
    assert elapsed < bound


def test_runtime_worker_stale_result_preserves_newer_requested_control_until_command_finishes(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-runtime-stale-control"
    )
    path = Path(state["manifest_path"])
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(state, now=0.0)
    advance_started = asyncio.Event()
    release_advance = asyncio.Event()
    original_advance = worker.advance
    stale_result: dict[str, object] = {}

    async def stale_advance(path_arg, _browser_context, *, scheduling_tasks=None):
        assert scheduling_tasks is not None
        stale = json.loads(json.dumps(store.load(path_arg)))
        stale.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="stale browser-cycle result",
        )
        stale_result.clear()
        stale_result.update(stale)
        advance_started.set()
        await release_advance.wait()
        return stale

    monkeypatch.setattr(worker, "advance", stale_advance)
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: FakeActions(),
    )
    worker.runtime_db.enqueue_command(
        command_id="cmd-runtime-stale-control",
        idempotency_key="runtime-stale-control",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={"action": "stop", "reason": "prove stale result cannot erase control"},
    )

    async def scenario():
        cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
        await asyncio.wait_for(advance_started.wait(), timeout=1.0)
        delivered = worker.dispatch_command_once()
        assert delivered is not None and delivered["status"] == "running"
        requested = store.load(path)
        assert requested["controls"][-1]["status"] == "requested"
        assert requested["updated_at"] != stale_result["updated_at"]

        release_advance.set()
        await cycle

        current = worker.registry.tasks_by_id[state["task_id"]]
        assert current["updated_at"] == requested["updated_at"]
        assert current["controls"][-1]["status"] == "requested"
        assert state["task_id"] in worker.registry.due_task_ids(time.time() + 1.0)

        terminal = await original_advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=list(worker.registry.tasks_by_id.values()),
        )
        assert terminal is not None
        assert terminal["controls"][-1]["status"] == "applied"
        command = worker.runtime_db.get_command("cmd-runtime-stale-control")
        assert command is not None and command["status"] == "applied"

    asyncio.run(scenario())


def test_runtime_worker_consumes_new_requested_control_during_unrelated_slow_cycle(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, slow, worker = setup_task(
        tmp_path, task_id="task-runtime-slow-cycle"
    )
    slow_path = Path(slow["manifest_path"])
    target = store.create_task(
        "blocked target",
        requested_team="target-team",
        task_id="task-runtime-live-control",
        governing_objective="blocked target",
        governing_directives=(),
    )
    target_path = Path(target["manifest_path"])
    target["status"] = "BLOCKED"
    target["kanban_column"] = "BLOCKED"
    target["block_code"] = "role_offline"
    target["block_retryable"] = False
    target["block_reason"] = "owned target PLAN tab is offline"
    target["roles"]["PLAN"].update(
        page_id="target-plan-page",
        page_url="https://chatgpt.com/c/target-plan",
        online=False,
        status="offline",
        last_error="page_missing",
    )
    target = store.save(target_path, target)

    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry._due_at[slow["task_id"]] = time.time() - 1.0
    assert target["task_id"] not in worker.registry.due_task_ids(time.time())

    slow_started = asyncio.Event()
    target_started = asyncio.Event()
    release_slow = asyncio.Event()
    original_advance = worker.advance
    reopen_calls = 0

    async def mixed_advance(path, browser_context, *, scheduling_tasks=None):
        resolved = Path(path).resolve()
        if resolved == slow_path.resolve():
            slow_started.set()
            await release_slow.wait()
            return store.load(path)
        if resolved == target_path.resolve():
            assert target["task_id"] in worker.registry.due_task_ids(time.time())
            target_started.set()
        return await original_advance(
            path,
            browser_context,
            scheduling_tasks=scheduling_tasks,
        )

    class ControlActions(FakeActions):
        async def reopen(
            self,
            _state,
            _role,
            *,
            require_clean_ready=True,
            foreground=True,
        ):
            nonlocal reopen_calls
            reopen_calls += 1
            assert foreground is True
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id="target-plan-page",
                url="https://chatgpt.com/c/target-plan",
                created=False,
                new_chat=False,
            )

    monkeypatch.setattr(worker, "advance", mixed_advance)
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: ControlActions(),
    )
    worker.runtime_db.enqueue_command(
        command_id="cmd-runtime-live-control",
        idempotency_key="runtime-live-control",
        kind="task_control",
        task_id=target["task_id"],
        expected_task_version=None,
        payload={
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact blocked role",
        },
    )

    async def scenario():
        cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
        await asyncio.wait_for(slow_started.wait(), timeout=1.0)

        delivered = worker.dispatch_command_once()
        assert delivered is not None and delivered["status"] == "running"
        requested = store.load(target_path)
        assert requested["controls"][-1]["status"] == "requested"

        await asyncio.wait_for(target_started.wait(), timeout=1.5)
        for _ in range(20):
            command = worker.runtime_db.get_command("cmd-runtime-live-control")
            if command is not None and command["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert command is not None and command["status"] == "applied"
        applied = store.load(target_path)
        assert applied["controls"][-1]["status"] == "applied"
        assert reopen_calls == 1
        assert cycle.done() is False

        release_slow.set()
        await asyncio.wait_for(cycle, timeout=1.0)

    asyncio.run(scenario())


def test_runtime_worker_inventory_timeout_does_not_starve_requested_control(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-runtime-inventory-stall"
    )
    path = Path(state["manifest_path"])
    store.update(
        path,
        lambda current: worker._block(
            current,
            "owned PLAN tab is offline",
            code="role_offline",
            retryable=False,
        ),
    )
    worker.hydrate_runtime()
    previous_projection = {
        "connected": True,
        "page_count": 1,
        "pages": [{"page_id": "previous-page", "url": "https://chatgpt.com/"}],
    }
    worker._browser_projection = previous_projection
    worker._last_browser_inventory_at = 0.0
    worker._last_browser_page_count = 1
    inventory_started = asyncio.Event()
    inventory_cancelled = asyncio.Event()
    release_inventory = asyncio.Event()
    inventory_calls = 0

    async def hung_inventory(*_args, **_kwargs):
        nonlocal inventory_calls
        inventory_calls += 1
        inventory_started.set()
        try:
            await release_inventory.wait()
        except asyncio.CancelledError:
            inventory_cancelled.set()
            await release_inventory.wait()
            raise

    monkeypatch.setattr(worker_module, "build_browser_projection", hung_inventory)
    worker.runtime_db.enqueue_command(
        command_id="cmd-runtime-inventory-stall",
        idempotency_key="runtime-inventory-stall",
        kind="task_control",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={"action": "stop", "reason": "prove inventory cannot starve controls"},
    )
    delivered = worker.dispatch_command_once()
    assert delivered is not None and delivered["status"] == "running"
    requested = store.load(path)
    assert requested["controls"][-1]["status"] == "requested"

    async def scenario():
        cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
        await asyncio.wait_for(inventory_started.wait(), timeout=1.0)
        done, _pending = await asyncio.wait({cycle}, timeout=2.2)
        first_cycle_bounded = cycle in done

        second_cycle_bounded = False
        second_cycle = None
        if first_cycle_bounded:
            second_cycle = asyncio.create_task(worker.run_once(SimpleNamespace(pages=[])))
            done, _pending = await asyncio.wait({second_cycle}, timeout=0.8)
            second_cycle_bounded = second_cycle in done

        release_inventory.set()
        await asyncio.wait_for(cycle, timeout=1.0)
        if second_cycle is not None:
            await asyncio.wait_for(second_cycle, timeout=1.0)
        await asyncio.sleep(0)
        return first_cycle_bounded, second_cycle_bounded

    first_cycle_bounded, second_cycle_bounded = asyncio.run(scenario())

    assert first_cycle_bounded is True
    assert second_cycle_bounded is True
    assert inventory_calls == 1
    assert inventory_cancelled.is_set() is False
    assert getattr(worker, "_browser_inventory_task", None) is None
    command = worker.runtime_db.get_command("cmd-runtime-inventory-stall")
    assert command is not None and command["status"] == "applied"
    applied = store.load(path)
    assert applied["controls"][-1]["status"] == "applied"
    assert worker._browser_projection == previous_projection


def test_refresh_scheduling_fast_path_skips_global_reconciliation_for_unconstrained_state(
    tmp_path: Path, monkeypatch
):
    _config, store, state, _worker = setup_task(
        tmp_path, task_id="task-scheduling-fast-path"
    )

    def unexpected_global_reconcile():
        raise AssertionError("unconstrained scheduling state entered global reconciliation")

    monkeypatch.setattr(
        store, "_recover_phase4_replacement_unlocked", unexpected_global_reconcile
    )

    refreshed, changed = store.refresh_scheduling(
        state["manifest_path"],
        tasks=[state],
        state=dict(state),
    )

    assert changed is False
    assert refreshed == state


def test_run_heartbeat_ticks_while_startup_hydration_is_blocking(
    tmp_path: Path, monkeypatch
):
    hydrate_started = threading.Event()
    release_hydrate = threading.Event()
    heartbeat_ticks: list[float] = []
    observed_during_hydrate: list[int] = []

    class _RuntimeDB:
        def ensure_schema(self) -> None:
            pass

        def requeue_running_commands(self) -> None:
            pass

        def close(self) -> None:
            pass

    class _Worker:
        def __init__(self, _config) -> None:
            self.store = SimpleNamespace(
                endpoints={}, recover_phase4_replacement=lambda: None
            )
            self.runtime_db = _RuntimeDB()
            self._browser_command_tasks = set()
            self._worker_lease_acquired_at = None

        def _restore_rate_limit_cooldown(self) -> None:
            pass

        def _publish_heartbeat(self, **_kwargs) -> None:
            pass

        def hydrate_runtime(self, *, startup=False) -> None:
            hydrate_started.set()
            assert release_hydrate.wait(timeout=1.0)

        async def _heartbeat_loop(self) -> None:
            while True:
                heartbeat_ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        async def run_command_loop(self) -> None:
            await asyncio.Event().wait()

        async def run_pool_forever(self, _pool) -> None:
            return None

    class _EndpointPool:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def close(self) -> None:
            pass

    def observe_and_release() -> None:
        assert hydrate_started.wait(timeout=1.0)
        time.sleep(0.06)
        observed_during_hydrate.append(len(heartbeat_ticks))
        release_hydrate.set()

    monkeypatch.setattr(worker_module, "CDPAWorker", _Worker)
    monkeypatch.setattr(worker_module, "EndpointPool", _EndpointPool)
    timer = threading.Thread(target=observe_and_release)
    timer.start()
    config = SimpleNamespace(
        repository_root=tmp_path,
        worker_poll_seconds=0.1,
    )

    asyncio.run(worker_module._run(config))
    timer.join(timeout=1.0)

    assert observed_during_hydrate
    assert observed_during_hydrate[0] >= 2


def test_heartbeat_stays_current_while_refresh_scheduling_blocks(
    tmp_path: Path, monkeypatch
):
    from playwright_auto.dashboard_api import DashboardAPI

    config, store, state, worker = setup_task(
        tmp_path, task_id="task-heartbeat-scheduling-block"
    )
    worker.config = replace(config, heartbeat_seconds=0.01, worker_stale_seconds=0.04)
    worker.hydrate_runtime()
    worker._publish_heartbeat(force=True)
    manifest_path = Path(state["manifest_path"])
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    observed_health: list[dict] = []
    waiting_state = dict(state)
    waiting_state.update(
        status="WAITING",
        kanban_column="WAITING",
        active_action="waiting_browser_capacity",
        waiting_code="browser_pool",
        waiting_reason="Waiting for browser capacity.",
        waiting={
            "reason": "browser_pool",
            "since": utc_now(),
            "waiting_on": [],
            "stopped": [],
            "missing": [],
            "resume_status": state["status"],
            "resume_column": state["kanban_column"],
            "resume_action": state["active_action"],
        },
    )
    monkeypatch.setattr(worker, "_load_manifest_cached", lambda *_args, **_kwargs: dict(waiting_state))

    def slow_refresh(_path, *, tasks=None, state=None):
        assert tasks is not None
        assert state is not None
        refresh_started.set()
        assert release_refresh.wait(timeout=1.0)
        return dict(state), False

    monkeypatch.setattr(store, "refresh_scheduling", slow_refresh)
    api = DashboardAPI(worker.config)

    def observe_and_release() -> None:
        assert refresh_started.wait(timeout=1.0)
        time.sleep(0.06)
        observed_health.append(api.worker_health())
        release_refresh.set()

    async def scenario():
        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        observer = threading.Thread(target=observe_and_release)
        observer.start()
        try:
            await worker.advance(
                manifest_path,
                SimpleNamespace(pages=[]),
                scheduling_tasks=[waiting_state],
            )
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            observer.join(timeout=1.0)

    try:
        asyncio.run(scenario())
        assert observed_health
        assert observed_health[0]["worker_online"] is True
        assert observed_health[0]["worker_stale"] is False
    finally:
        api.db.close()
        worker.runtime_db.close()


def test_worker_health_is_starting_until_runtime_and_command_loop_are_ready(tmp_path: Path):
    from playwright_auto.dashboard_api import DashboardAPI

    _config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-worker-starting-health"
    )
    worker.runtime_db.ensure_schema()
    worker._runtime_ready = False
    worker._command_loop_ready = False
    worker._publish_heartbeat(force=True)
    api = DashboardAPI(worker.config)
    try:
        starting = api.worker_health()
        assert starting["worker_status"] == "STARTING"
        assert starting["worker_ready"] is False
        assert starting["worker_runtime_ready"] is False
        assert starting["worker_command_loop_ready"] is False

        worker._runtime_ready = True
        worker._command_loop_ready = True
        worker._publish_heartbeat(force=True)
        healthy = api.worker_health()
        assert healthy["worker_status"] == "HEALTHY"
        assert healthy["worker_ready"] is True
    finally:
        api.db.close()
        worker.runtime_db.close()


def test_blocked_event_loop_still_becomes_stale(tmp_path: Path):
    from playwright_auto.dashboard_api import DashboardAPI

    config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-heartbeat-event-loop-block"
    )
    worker.config = replace(config, heartbeat_seconds=0.01, worker_stale_seconds=0.04)
    worker.hydrate_runtime()
    worker._publish_heartbeat(force=True)
    api = DashboardAPI(worker.config)
    observed_health: list[dict] = []

    def observe() -> None:
        time.sleep(0.06)
        observed_health.append(api.worker_health())

    async def scenario():
        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        observer = threading.Thread(target=observe)
        observer.start()
        try:
            time.sleep(0.09)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            observer.join(timeout=1.0)

    try:
        asyncio.run(scenario())
        assert observed_health
        assert observed_health[0]["worker_stale"] is True
        assert observed_health[0]["worker_online"] is False
    finally:
        api.db.close()
        worker.runtime_db.close()


def test_heartbeat_ticker_keeps_long_advance_online_without_task_mutation(
    tmp_path: Path, monkeypatch
):
    from dataclasses import replace

    from playwright_auto.dashboard_api import DashboardAPI

    config, store, state, worker = setup_task(tmp_path, task_id="task-heartbeat-live")
    worker.config = replace(config, heartbeat_seconds=0.01, worker_stale_seconds=0.04)
    worker.hydrate_runtime()
    worker._command_loop_ready = True
    worker._publish_heartbeat(force=True)
    manifest_path = Path(state["manifest_path"])
    manifest_before = manifest_path.read_bytes()
    advance_started = asyncio.Event()

    async def slow_advance(path, _browser_context, *, scheduling_tasks=None):
        assert scheduling_tasks is not None
        advance_started.set()
        await asyncio.sleep(0.10)
        return store.load(path)

    monkeypatch.setattr(worker, "advance", slow_advance)
    api = DashboardAPI(worker.config)

    async def scenario():
        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        advance_task = asyncio.create_task(
            worker.advance(
                manifest_path,
                SimpleNamespace(pages=[]),
                scheduling_tasks=[store.load(manifest_path)],
            )
        )
        try:
            await asyncio.wait_for(advance_started.wait(), timeout=1.0)
            await asyncio.sleep(0.07)
            health = api.worker_health()
            assert health["worker_online"] is True
            assert health["worker_stale"] is False
            await advance_task
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    try:
        asyncio.run(scenario())
        assert manifest_path.read_bytes() == manifest_before
    finally:
        api.db.close()
        worker.runtime_db.close()


def test_stopped_heartbeat_ticker_becomes_stale(tmp_path: Path, monkeypatch):
    from dataclasses import replace

    import playwright_auto.dashboard_api as dashboard_api_module
    from playwright_auto.dashboard_api import DashboardAPI

    config, _store, _state, worker = setup_task(tmp_path, task_id="task-heartbeat-stale")
    worker.config = replace(config, heartbeat_seconds=0.01, worker_stale_seconds=0.04)
    worker.hydrate_runtime()
    api = DashboardAPI(worker.config)

    async def scenario():
        heartbeat_published = asyncio.Event()
        original_publish_heartbeat = worker._publish_heartbeat

        def publish_heartbeat(*args, **kwargs):
            original_publish_heartbeat(*args, **kwargs)
            heartbeat_published.set()

        monkeypatch.setattr(worker, "_publish_heartbeat", publish_heartbeat)
        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        try:
            await asyncio.wait_for(heartbeat_published.wait(), timeout=1.0)
            live = api.worker_health()
            assert live["worker_online"] is True
            assert live["worker_stale"] is False
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        heartbeat_at = api.worker_health()["worker_heartbeat_at"]
        stale_at = (
            datetime.fromisoformat(heartbeat_at).timestamp()
            + worker.config.worker_stale_seconds
            + 0.001
        )
        monkeypatch.setattr(
            dashboard_api_module,
            "time",
            SimpleNamespace(time=lambda: stale_at),
        )
        assert api.worker_health()["worker_stale"] is True

    try:
        asyncio.run(scenario())
    finally:
        api.db.close()
        worker.runtime_db.close()


def test_dashboard_actions_snapshot_tracks_browser_and_command_changes(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-dashboard-actions"
    )
    worker.hydrate_runtime()

    initial = worker.runtime_db.get_snapshot("dashboard_actions")
    assert initial is not None
    assert initial["payload"]["resume_teams"] == []
    assert initial["payload"]["dependency_teams"][0]["team"] == state["team"]

    asyncio.run(worker._publish_browser_inventory(SimpleNamespace(pages=[])))
    connected = worker.runtime_db.get_snapshot("dashboard_actions")["payload"]
    assert connected["resume_teams"] == [
        {
            "team": state["team"],
            "task_id": state["task_id"],
            "title": state["task_title"],
            "status": "INBOX",
            "reason": "offline",
        }
    ]

    worker._publish_browser_disconnected()
    disconnected = worker.runtime_db.get_snapshot("dashboard_actions")["payload"]
    assert disconnected["resume_teams"] == []

    worker.runtime_db.enqueue_command(
        command_id="cmd-dashboard-actions-create",
        idempotency_key="dashboard-actions-create",
        kind="create_task",
        task_id="task-dashboard-actions-created",
        expected_task_version=None,
        payload={
            "task": "Created dashboard action dependency",
            "requested_team": "mailbox",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["PLAN", "REVIEW"],
            "governing_objective": "Created dashboard action dependency",
            "governing_directives": [],
        },
    )
    assert worker._apply_next_command()["status"] == "applied"
    after_create = worker.runtime_db.get_snapshot("dashboard_actions")["payload"]
    assert {item["team"] for item in after_create["dependency_teams"]} == {
        state["team"],
        "mailbox",
    }


def test_route_repair_send_race_with_active_response_waits_instead_of_blocking(
    tmp_path: Path, monkeypatch
):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-route-repair-send-race"
    )
    hop = _active_hop(state)
    hop["kind"] = "route_repair"
    hop["prompt"] = "repair route only"
    hop["prompt_sha256"] = worker_module._sha(hop["prompt"])
    hop["state"] = "sending"
    hop["timestamps"]["sending_at"] = utc_now()
    state["status"] = "RUNNING"
    state["active_action"] = "send"

    ledger = RequestLedger(hop["ledger_path"])
    constructor = worker_module.task_workflow_definitions(
        state, worker.config
    )[hop["target_role"]]["system_prompt"]
    record = ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": state["manifest_path"],
        },
        role_prompt_hash=worker_module._sha(constructor),
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0

    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="page-plan",
        url="https://chatgpt.com/c/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        created=False,
        new_chat=False,
    )

    async def owned(*_args, **_kwargs):
        return acquired

    async def no_rate_limit(_acquired):
        return None

    async def no_observer(*_args, **_kwargs):
        return None

    async def active_before_click(_self, _context):
        raise UnsafePageStateError("page is already responding")

    monkeypatch.setattr(worker, "_owned_or_block", owned)
    monkeypatch.setattr(worker, "_dismiss_known_rate_limit_on_existing", no_rate_limit)
    monkeypatch.setattr(worker, "_arm_passive_request_observer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_module.DurableSendBlock, "run", active_before_click)

    asyncio.run(worker._sending(state, hop, FakeActions()))

    durable = ledger.get(hop["request_id"])
    assert durable.status is RequestStatus.NEW
    assert durable.attempts == 0
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_response"
    assert hop["state"] == "waiting"
    assert hop["observed_existing_response"] is True
    assert hop.get("receipt") is None


def test_resume_endpointless_only_for_durable_state_only_transitions(tmp_path: Path):
    _config, _store, state, _worker = setup_task(
        tmp_path, task_id="task-resume-endpointless"
    )
    control = {"control_id": 1, "action": "resume", "status": "requested"}
    hop = state["hops"][0]

    hop["state"] = "responded"
    assert worker_module._control_can_run_endpointless(state, control) is True

    hop["state"] = "routed"
    hop["route"] = "PAUSE"
    assert worker_module._control_can_run_endpointless(state, control) is True

    hop["state"] = "waiting"
    hop["route"] = None
    assert worker_module._control_can_run_endpointless(state, control) is False

    hop["state"] = "sending"
    assert worker_module._control_can_run_endpointless(state, control) is False


def test_starting_worker_admits_exact_team_resume_without_full_hydrate(
    tmp_path: Path, monkeypatch
):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-startup-resume"
    )

    def pause(current: dict) -> dict:
        current["status"] = "PAUSED"
        current["kanban_column"] = "PAUSED"
        current["active_action"] = "paused"
        current["pause_reason"] = "operational pause"
        return current

    paused = store.update(state["manifest_path"], pause)
    worker.runtime_db.ensure_schema()
    assert worker.registry is None

    def forbidden_hydrate(*_args, **_kwargs):
        raise AssertionError("exact-team Resume must not require full runtime hydrate")

    monkeypatch.setattr(worker, "hydrate_runtime", forbidden_hydrate)
    worker.runtime_db.enqueue_command(
        command_id="cmd-startup-resume",
        idempotency_key="startup-resume",
        kind="resume_team",
        task_id=paused["task_id"],
        expected_task_version=None,
        payload={"team": paused["team"], "reason": "resume during startup"},
    )

    result = worker.dispatch_command_once()

    assert result is not None
    assert result["status"] == "running"
    saved = store.load(paused["manifest_path"])
    control = saved["controls"][-1]
    assert control["action"] == "resume"
    assert control["status"] == "requested"
    assert control["external_command_id"] == "cmd-startup-resume"
    assert worker.registry is None
    assert worker.runtime_db.get_task_detail(paused["task_id"])["task_id"] == paused["task_id"]


def test_startup_hydrate_merges_concurrent_exact_task_control_into_registry(
    tmp_path: Path, monkeypatch
):
    import threading

    config, store, state, worker = setup_task(
        tmp_path, task_id="task-startup-hydrate-race"
    )
    worker.config = replace(config, independent_seed_builtins=False)

    def pause(current: dict) -> dict:
        current["status"] = "PAUSED"
        current["kanban_column"] = "PAUSED"
        current["active_action"] = "paused"
        current["pause_reason"] = "operational pause"
        return current

    paused = store.update(state["manifest_path"], pause)
    worker.runtime_db.ensure_schema()
    discovered = threading.Event()
    release = threading.Event()
    original_discover = store.discover_with_errors

    def paused_discover():
        result = original_discover()
        discovered.set()
        assert release.wait(timeout=5.0)
        return result

    monkeypatch.setattr(store, "discover_with_errors", paused_discover)
    errors: list[BaseException] = []

    def hydrate():
        try:
            worker.hydrate_runtime(startup=False)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=hydrate)
    thread.start()
    assert discovered.wait(timeout=5.0)
    assert worker.registry is None

    worker.runtime_db.enqueue_command(
        command_id="cmd-startup-hydrate-race",
        idempotency_key="startup-hydrate-race",
        kind="resume_team",
        task_id=paused["task_id"],
        expected_task_version=None,
        payload={"team": paused["team"], "reason": "resume during hydrate"},
    )
    command = worker.dispatch_command_once()
    assert command is not None
    assert command["status"] == "running"
    release.set()
    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert errors == []

    registered = worker.registry.tasks_by_id[paused["task_id"]]
    control = registered["controls"][-1]
    assert control["action"] == "resume"
    assert control["status"] == "requested"
    assert control["external_command_id"] == "cmd-startup-hydrate-race"
    assert paused["task_id"] in worker.registry.due_task_ids(time.time() + 1.0)


def test_startup_projection_replace_cannot_overwrite_newer_command_projection(
    tmp_path: Path, monkeypatch
):
    config, store, state, worker = setup_task(
        tmp_path, task_id="task-startup-projection-race"
    )
    worker.config = replace(config, independent_seed_builtins=False)

    def pause(current: dict) -> dict:
        current["status"] = "PAUSED"
        current["kanban_column"] = "PAUSED"
        current["active_action"] = "paused"
        current["pause_reason"] = "startup projection race"
        return current

    paused = store.update(state["manifest_path"], pause)
    worker.runtime_db.ensure_schema()
    replacing = threading.Event()
    release_replace = threading.Event()
    original_replace = worker.runtime_db.replace_task_projections

    def delayed_replace(projections, *, catalog):
        replacing.set()
        assert release_replace.wait(timeout=5.0)
        return original_replace(projections, catalog=catalog)

    monkeypatch.setattr(
        worker.runtime_db, "replace_task_projections", delayed_replace
    )
    hydrate_errors: list[BaseException] = []

    def hydrate():
        try:
            worker.hydrate_runtime(startup=False)
        except BaseException as exc:
            hydrate_errors.append(exc)

    hydrate_thread = threading.Thread(target=hydrate)
    hydrate_thread.start()
    assert replacing.wait(timeout=5.0)

    worker.runtime_db.enqueue_command(
        command_id="cmd-startup-projection-race",
        idempotency_key="startup-projection-race",
        kind="resume_team",
        task_id=paused["task_id"],
        expected_task_version=None,
        payload={"team": paused["team"], "reason": "newer command projection"},
    )
    command_result: list[dict | None] = []
    command_thread = threading.Thread(
        target=lambda: command_result.append(worker.dispatch_command_once())
    )
    command_thread.start()
    time.sleep(0.1)
    release_replace.set()
    hydrate_thread.join(timeout=10.0)
    command_thread.join(timeout=10.0)

    assert not hydrate_thread.is_alive()
    assert not command_thread.is_alive()
    assert hydrate_errors == []
    assert command_result and command_result[0] is not None
    detail = worker.runtime_db.get_task_detail(paused["task_id"])
    controls = detail["controls"]
    assert controls[-1]["external_command_id"] == "cmd-startup-projection-race"
    assert controls[-1]["status"] == "requested"


def test_runtime_create_command_applies_once_and_publishes_projection(tmp_path: Path):
    config, store, _state, worker = setup_task(tmp_path, task_id="task-existing-command")
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-once",
        idempotency_key="create-once",
        kind="create_task",
        task_id="task-created-command",
        expected_task_version=None,
        payload={
            "task": "Created through mailbox",
            "requested_team": "mailbox",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["REVIEW", "PLAN"],
            "governing_objective": "Created through mailbox",
            "governing_directives": [],
        },
    )

    result = worker._apply_next_command()

    assert result["status"] == "applied"
    created = store.load_task_id("task-created-command")
    assert created is not None
    assert created["applied_command_ids"] == ["cmd-create-once"]
    assert list(created["roles"]) == ["PLAN", "REVIEW"]
    assert worker.runtime_db.get_task_detail("task-created-command")["task_id"] == "task-created-command"

    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL "
            "WHERE command_id = ?",
            ("cmd-create-once",),
        )
    reconciled = worker._apply_next_command()
    assert reconciled["status"] == "applied"
    assert reconciled["result"]["reconciled"] is True
    assert store.load_task_id("task-created-command")["applied_command_ids"] == [
        "cmd-create-once"
    ]









def test_missing_snapshot_bootstrap_does_not_rebase_to_current_default(tmp_path: Path):
    config, store, state, worker = setup_task(tmp_path, task_id="task-bootstrap-account-switch")
    old = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    state["bootstrap"] = old
    BootstrapCatalog(tmp_path).delete(old["bootstrap_id"])
    BootstrapCatalog(tmp_path).upsert(
        bootstrap_record(bootstrap_id="g8-bootstrap")
    )

    assert worker._bootstrap_for_state(state) is None
    assert state["bootstrap"] == old


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_runtime_create_snapshots_bootstrap_and_replay_ignores_catalog_drift(tmp_path: Path):
    config, store, _state, worker = setup_task(
        tmp_path, task_id="task-existing-bootstrap-command"
    )
    worker.hydrate_runtime()
    anchor = BootstrapCatalog(tmp_path).upsert(bootstrap_record())
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-bootstrap",
        idempotency_key="create-bootstrap",
        kind="create_task",
        task_id="task-created-bootstrap",
        expected_task_version=None,
        payload={
            "task": "Created with bootstrap",
            "requested_team": "bootstrap-mailbox",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["PLAN", "DEV", "REVIEW"],
            "bootstrap_id": anchor["bootstrap_id"],
        },
    )

    result = worker._apply_next_command()
    assert result["status"] == "applied"
    created = store.load_task_id("task-created-bootstrap")
    assert created is not None
    assert created["bootstrap"] == anchor

    BootstrapCatalog(tmp_path).delete(anchor["bootstrap_id"])
    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            ("cmd-create-bootstrap",),
        )
    replay = worker._apply_next_command()
    assert replay["status"] == "applied"
    assert replay["result"]["reconciled"] is True
    assert store.load_task_id("task-created-bootstrap")["bootstrap"] == anchor

    command = worker.runtime_db.get_command("cmd-create-bootstrap")
    changed = {**command, "payload": {**command["payload"], "bootstrap_id": "other-bootstrap"}}
    with pytest.raises(RuntimeError, match="bootstrap"):
        worker._command_replay_state(changed)

    disabled = BootstrapCatalog(tmp_path).upsert(
        bootstrap_record(bootstrap_id="disabled-bootstrap", enabled=False)
    )
    worker.runtime_db.enqueue_command(
        command_id="cmd-create-disabled-bootstrap",
        idempotency_key="create-disabled-bootstrap",
        kind="create_task",
        task_id="task-disabled-bootstrap",
        expected_task_version=None,
        payload={
            "task": "Must fail",
            "requested_team": "disabled-bootstrap",
            "repository": str(tmp_path),
            "report_mode": "file",
            "bootstrap_id": disabled["bootstrap_id"],
        },
    )
    failed = worker._apply_next_command()
    assert failed["status"] == "failed"
    assert store.load_task_id("task-disabled-bootstrap") is None

    worker.runtime_db.enqueue_command(
        command_id="cmd-create-explicit-fresh",
        idempotency_key="create-explicit-fresh",
        kind="create_task",
        task_id="task-explicit-fresh",
        expected_task_version=None,
        payload={
            "task": "Explicit Fresh remains supported",
            "requested_team": "explicit-fresh",
            "repository": str(tmp_path),
            "report_mode": "file",
            "bootstrap_id": None,
        },
    )
    fresh_result = worker._apply_next_command()
    assert fresh_result["status"] == "applied"
    fresh = store.load_task_id("task-explicit-fresh")
    assert fresh is not None
    assert fresh.get("bootstrap") is None

    worker.runtime_db.enqueue_command(
        command_id="cmd-create-inline-bootstrap",
        idempotency_key="create-inline-bootstrap",
        kind="create_task",
        task_id="task-inline-bootstrap",
        expected_task_version=None,
        payload={
            "task": "Create bootstrap inline",
            "requested_team": "inline-bootstrap-team",
            "repository": str(tmp_path),
            "report_mode": "file",
            "bootstrap_id": "inline-bootstrap",
            "bootstrap_definition": {
                "bootstrap_id": "inline-bootstrap",
                "name": "Inline Bootstrap",
                "source_conversation_id": None,
                "prewarm_prompt": "Reusable inline prewarm.",
                "max_backups": 5,
            },
        },
    )
    inline_result = worker._apply_next_command()
    assert inline_result["status"] == "applied"
    inline = store.load_task_id("task-inline-bootstrap")
    assert inline is not None
    assert inline["bootstrap"]["bootstrap_id"] == "inline-bootstrap"
    assert inline["bootstrap"]["prewarm_prompt"] == "Reusable inline prewarm."
    assert inline["bootstrap"]["max_backups"] == 5
    assert BootstrapCatalog(tmp_path).get("inline-bootstrap") == inline["bootstrap"]


def test_bootstrap_projection_is_sanitized_and_distinguishes_fallbacks(tmp_path: Path):
    from playwright_auto.cdpa_projection import build_task_projection

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "projection bootstrap",
        requested_team="projection-bootstrap",
        task_id="task-bootstrap-projection",
        roles=("PLAN", "DEV", "REVIEW"),
        bootstrap=bootstrap_record(),
        governing_objective="projection bootstrap",
        governing_directives=(),
    )
    state["roles"]["PLAN"]["context_source"] = "bootstrap_donor"
    state["roles"]["DEV"]["context_source"] = "bootstrap_native"
    state["roles"]["REVIEW"]["context_source"] = "bootstrap_ui"
    state["status"] = "BLOCKED"
    state["block_reason"] = "locator timeout for bootstrap donor"

    detail = build_task_projection(state, tasks=[state]).detail
    context = detail["bootstrap_context"]
    assert context["bootstrap_id"] == "general-team-bootstrap"
    assert context["name"] == "General Team Bootstrap"
    assert context["roles"]["PLAN"] != context["roles"]["DEV"]
    assert context["roles"]["DEV"] != context["roles"]["REVIEW"]
    serialized = json.dumps(detail, sort_keys=True)
    assert bootstrap_record()["conversation_id"] not in serialized
    assert bootstrap_record()["terminal_assistant_message_id"] not in serialized

    fresh = store.create_task(
        "projection fresh", requested_team="projection-fresh", task_id="task-fresh-projection",
        governing_objective="projection fresh",
        governing_directives=(),
    )
    fresh_detail = build_task_projection(fresh, tasks=[fresh]).detail
    assert "bootstrap_context" not in fresh_detail


def test_independent_activation_refreshes_registry_and_projection(tmp_path: Path):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-maintainers-refresh"
    )
    store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "role_offline",
            "block_reason": "blocked",
            "updated_at": "2026-07-26T00:00:00+00:00",
        },
    )
    store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "blocked_at": "2026-07-26T00:00:00+00:00",
        },
    )
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    standby = store.update(
        standby["manifest_path"],
        lambda current: {
            **current,
            "independent": {
                **current["independent"],
                "watermarks": {
                    **current["independent"]["watermarks"],
                    "recovery_enabled_at": "2026-07-26T00:00:00+00:00",
                },
            },
        },
    )
    worker.hydrate_runtime()

    worker._activate_independent_agents()

    assert worker.registry.tasks_by_id[standby["task_id"]]["status"] == "RUNNING"
    projected = worker.runtime_db.get_task_detail(standby["task_id"])
    assert projected["status"] == "RUNNING"
    assert projected["column"] == "INDEPENDENT_AGENTS"
    assert projected["agent"]["target_task_id"] == state["task_id"]















def test_active_accepted_waiting_offline_reopens_without_resend_or_clean_composer(
    tmp_path: Path,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-auto-recover-waiting"
    )
    hop = _active_hop(state)
    exact_url = "https://chatgpt.com/c/waiting-exact"
    original_request_id = hop["request_id"]
    receipt = {
        "binding": {"page_id": "closed-page", "role": "alpha-plan"},
        "accepted_via": "user_message_identity",
        "user_message_id": "accepted-user",
        "user_turn_id": "accepted-turn",
    }
    hop.update(state="waiting", conversation_url=exact_url, receipt=receipt)
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url=exact_url,
        online=False,
    )
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="closed-page",
        url=exact_url,
        created=True,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.reopen_calls = []
            self.wake_calls = []

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(
            self,
            *_args,
            require_clean_ready=True,
            foreground=True,
            **_kwargs,
        ):
            self.reopen_calls.append((require_clean_ready, foreground))
            return acquired

        async def wake(self, exact):
            self.wake_calls.append(exact)

    actions = Actions()
    result = asyncio.run(worker._owned_or_block(state, "PLAN", actions))

    assert result == acquired
    assert actions.reopen_calls == [(False, False)]
    assert actions.wake_calls == [acquired]
    assert hop["state"] == "waiting"
    assert hop["request_id"] == original_request_id
    assert hop["receipt"] == receipt
    assert state["status"] != "BLOCKED"
    assert state.get("maintenance") in (None, {})


def test_change_goal_command_applies_once_and_reconciles_without_browser(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-change-goal")
    state = store.update(state["manifest_path"], lambda current: {**current, "status": "RUNNING"})
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-change-goal", idempotency_key="change-goal", kind="change_goal",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={
            "goal": "Replacement from command",
            "governing_objective": "Replacement from command",
            "governing_directives": [],
        },
    )
    applied = worker.dispatch_command_once()
    assert applied["status"] == "applied"
    changed = store.load_task_id(state["task_id"])
    assert changed["effective_goal"] == "Replacement from command"
    assert len(changed["goal_revisions"]) == 1
    assert changed["hops"][0]["prompt"] is None
    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            ("cmd-change-goal",),
        )
    reconciled = worker.dispatch_command_once()
    assert reconciled["status"] == "applied"
    assert reconciled["result"]["reconciled"] is True
    assert len(store.load_task_id(state["task_id"])["goal_revisions"]) == 1


def test_change_goal_keeps_current_prompt_and_updates_first_later_hop(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-goal-prompt")
    state = store.update(state["manifest_path"], lambda current: {**current, "status": "RUNNING"})
    state = store.change_goal(
        state["manifest_path"],
        "Replacement for later roles",
        governing_objective="Replacement for later roles",
        governing_directives=(),
        external_command_id="cmd-goal-prompt",
    )
    current = _active_hop(state)
    asyncio.run(worker._pre_send(state, current, FakeActions()))
    assert "Goal: Implement exact production behavior" in current["prompt"]

    later = worker._append_hop(
        state, source_role="PLAN", target_role="DEV",
        handoff=".plan/alpha/alpha-plan_turn1_task-goal-prompt.md",
    )
    asyncio.run(worker._pre_send(state, later, FakeActions()))
    assert "Goal: Replacement for later roles" in later["prompt"]


def test_resume_reconciles_canonical_ledger_before_exact_page_validation(tmp_path: Path):
    from dataclasses import replace

    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-stale-web"
    )
    worker.runtime_db.ensure_schema()
    provisional_url = "https://chatgpt.com/c/WEB:stale-resume"
    canonical_id = "canonical-resume"
    canonical_url = f"https://chatgpt.com/c/{canonical_id}"
    hop["conversation_url"] = provisional_url
    state["roles"]["PLAN"]["page_url"] = provisional_url
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id=canonical_id).to_dict(),
    )
    control = {
        "control_id": 1,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=20)
    ).isoformat()
    current_receipt = replace(receipt, conversation_id=canonical_id)
    accepted_user = MessageSnapshot(
        "user",
        current_receipt.user_message_id or "accepted-user",
        current_receipt.user_turn_id or "accepted-turn",
        current_receipt.prompt,
        (),
    )
    snapshot = replace(
        send_snapshot(
            messages=(accepted_user,),
            state=ChatGPTState.RESPONDING,
            task_id=state["task_id"],
            team=state["team"],
        ),
        stop_visible=True,
    )
    client = SimpleNamespace(
        wait_snapshot=AsyncMock(return_value=snapshot),
    )
    acquired = AcquiredRole(
        client, current_receipt.binding.page_id, canonical_url, False, False
    )
    seen = []

    class Actions:
        async def backend_stream_status(self, conversation_id):
            seen.append(conversation_id)
            return {"status": "IS_STREAMING"}

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("Resume must not fetch the conversation graph")

        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("exact owned Resume should not reopen source")

    asyncio.run(worker._recover_resume_waiting(state, hop, control, Actions()))

    assert seen == []
    assert control["status"] == "applied"
    assert control["result"]["action"] == "resume_role_controller"
    assert control["result"]["postcondition"] == "observation_rearmed"
    assert hop["receipt"]["conversation_id"] == canonical_id
    assert hop["conversation_url"] == canonical_url
    assert state["roles"]["PLAN"]["page_url"] == canonical_url
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1



def test_waiting_reconciles_conversation_id_from_exact_ledger_without_status_poll(tmp_path: Path):
    from dataclasses import replace
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-conversation-reconcile"
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id="conversation-1").to_dict(),
    )

    worker._reconcile_hop_conversation_identity(state, hop)

    assert hop["receipt"]["conversation_id"] == "conversation-1"
    assert ledger.get(hop["request_id"]).attempts == 1



def test_one_shot_stream_status_is_reserved_for_recovery_boundary(tmp_path: Path):
    _store, _state, worker, _path, _hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-expired-backend-first"
    )
    canonical_id = "expired-backend"
    receipt = replace(receipt, conversation_id=canonical_id)
    calls = []

    class Actions:
        async def backend_stream_status(self, conversation_id):
            calls.append(("status", conversation_id))
            return {"status": "COMPLETE"}

        async def backend_conversation(self, conversation_id):
            calls.append(("graph", conversation_id))
            raise AssertionError("one-shot status must not fetch the full graph")

    status = asyncio.run(worker._one_shot_stream_status(Actions(), receipt))

    assert status == "COMPLETE"
    assert calls == [("status", canonical_id)]


def test_record_response_reconciles_late_conversation_id_and_conflict_fails_closed(tmp_path: Path):
    from dataclasses import replace
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-conversation-late"
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(
        hop["request_id"],
        receipt=replace(receipt, conversation_id="conversation-late").to_dict(),
    )
    response = MessageSnapshot("assistant", "a1", "ta1", "answer", ())
    worker._record_response(state, hop, response)
    assert hop["state"] == "responded"
    assert hop["receipt"]["conversation_id"] == "conversation-late"

    hop["state"] = "waiting"
    hop["receipt"] = replace(receipt, conversation_id="conversation-old").to_dict()
    with pytest.raises(RuntimeError, match="conversation identity"):
        worker._record_response(state, hop, response)
    assert hop["state"] == "waiting"


def donor_pool_record(*, donors=None, prewarm_prompt=None, max_backups=7):
    return {
        "bootstrap_id": "general-team-bootstrap",
        "name": "General Team Bootstrap",
        "description": "Reusable task-neutral context",
        "source_conversation_id": "11111111-1111-4111-8111-111111111111",
        "prewarm_prompt": prewarm_prompt,
        "max_backups": max_backups,
        "donors": list(
            donors
            if donors is not None
            else [
                {
                    "conversation_id": "11111111-1111-4111-8111-111111111111",
                    "assistant_message_id": "22222222-2222-4222-8222-222222222222",
                }
            ]
        ),
        "enabled": True,
        "tags": ["general"],
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:00+00:00",
    }


def _bootstrap_graph(assistant_id: str):
    return {
        "current_node": assistant_id,
        "mapping": {
            "bootstrap-user": {
                "id": "bootstrap-user",
                "message": {
                    "id": "bootstrap-user",
                    "author": {"role": "user"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["seed"]},
                },
                "parent": None,
                "children": [assistant_id],
            },
            assistant_id: {
                "id": assistant_id,
                "message": {
                    "id": assistant_id,
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["bootstrap"]},
                },
                "parent": "bootstrap-user",
                "children": [],
            },
        },
    }


def test_bootstrap_keeper_compatibility_is_retired_from_worker():
    assert not hasattr(CDPAWorker, "_wait_for_bootstrap_repair")
    assert not hasattr(CDPAWorker, "_release_bootstrap_repair_wait")


def test_shared_automated_send_gate_enforces_remaining_spacing(tmp_path: Path, monkeypatch):
    _, _, _, worker = setup_task(tmp_path, task_id="task-send-spacing")
    worker._last_automated_send_at = worker_module.time.monotonic() - 5.0
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(worker_module.asyncio, "sleep", fake_sleep)
    calls = []

    async def send_once():
        calls.append("send")
        return "accepted"

    before = worker_module.time.monotonic()
    result = asyncio.run(worker._run_automated_send(send_once))

    assert result == "accepted"
    assert calls == ["send"]
    assert len(sleeps) == 1 and 4.5 <= sleeps[0] <= 5.0
    assert worker._last_automated_send_at is not None
    assert worker._last_automated_send_at >= before


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_first_allocation_keeps_catalog_primary_before_task_backup(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    root_donor = donor_pool_record()["donors"][0]
    record = catalog.upsert(donor_pool_record())
    store = TaskStore(config)
    state = store.create_task(
        "prefer stable primary donor",
        requested_team="donor-preference",
        task_id="task-donor-preference",
        bootstrap=record,
        governing_objective="prefer stable primary donor",
        governing_directives=(),
    )
    child_donor = {
        "conversation_id": "33333333-3333-4333-8333-333333333333",
        "assistant_message_id": "44444444-4444-4444-8444-444444444444",
    }
    state["bootstrap_task_donors"] = [child_donor]
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branches = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            donor = child_donor if conversation_id == child_donor["conversation_id"] else root_donor
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id, **_kwargs
        ):
            self.branches.append((source_conversation_id, assistant_message_id))
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id=f"branch-{role.lower()}",
                url=f"https://chatgpt.com/c/{source_conversation_id}",
                created=True,
                new_chat=True,
            )

    actions = Actions()
    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", actions))

    assert acquired is not None
    assert actions.branches == [
        (root_donor["conversation_id"], root_donor["assistant_message_id"])
    ]
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_transient_donor_backend_failure_retries_without_removing_or_blocking(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record())
    store = TaskStore(config)
    state = store.create_task(
        "transient donor read",
        requested_team="donor-transient",
        task_id="task-donor-transient",
        bootstrap=record,
        governing_objective="transient donor read",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, _conversation_id):
            raise worker_module.BackendUnavailableError(502, "conversation")

    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", Actions()))

    assert acquired is None
    assert state.get("block_code") is None
    assert state["active_action"] == "bootstrap_retry"
    assert catalog.get(record["bootstrap_id"])["donors"] == record["donors"]


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_source_only_bootstrap_requires_local_materialization_without_graph_fetch(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(donors=[]))
    store = TaskStore(config)
    state = store.create_task(
        "materialize source donor",
        requested_team="donor-source",
        task_id="task-donor-source",
        bootstrap=record,
        governing_objective="materialize source donor",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)
    assistant_id = "22222222-2222-4222-8222-222222222222"

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.branches = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, _conversation_id):
            raise AssertionError("source-only bootstrap must not fetch conversation graph")

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id, **_kwargs
        ):
            self.branches.append((source_conversation_id, assistant_message_id))
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id=f"branch-{role.lower()}",
                url=f"https://chatgpt.com/c/{source_conversation_id}",
                created=True,
                new_chat=True,
            )

    actions = Actions()
    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", actions))

    assert acquired is None
    assert actions.branches == []
    assert catalog.get(record["bootstrap_id"])["donors"] == []
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "bootstrap_unavailable"
    assert state["bootstrap_source_requires_local_materialization"] == record["source_conversation_id"]


def test_active_rate_limit_gate_blocks_prewarm_before_global_role_acquisition(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(
        donor_pool_record(donors=[], prewarm_prompt="Load reusable bootstrap context only.")
    )
    store = TaskStore(config)
    state = store.create_task(
        "do not prewarm during cooldown",
        requested_team="donor-prewarm-gated",
        task_id="task-donor-prewarm-gated",
        bootstrap=record,
        governing_objective="do not prewarm during cooldown",
        governing_directives=(),
    )
    state["bootstrap_source_exhausted"] = True
    worker = CDPAWorker(config, store=store)
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-08-09T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }

    class Actions(FakeActions):
        async def acquire_global_role(self, physical_role, *, allow_create=True):
            assert physical_role == "BOOTSTRAP"
            assert allow_create is False
            raise RoleOwnershipError("global role has no open tab")

    updated = asyncio.run(worker._regenerate_bootstrap_donor(state, record, Actions()))

    assert updated is None
    assert state["active_action"] == "rate_limit_cooldown"


def test_prewarm_regenerates_donor_through_shared_send_gate(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(
        donor_pool_record(donors=[], prewarm_prompt="Load reusable bootstrap context only.")
    )
    store = TaskStore(config)
    state = store.create_task(
        "regenerate donor",
        requested_team="donor-prewarm",
        task_id="task-donor-prewarm",
        bootstrap=record,
        governing_objective="regenerate donor",
        governing_directives=(),
    )
    state["bootstrap_source_exhausted"] = True
    worker = CDPAWorker(config, store=store)
    calls = []

    class Client:
        binding = SimpleNamespace(role="BOOTSTRAP")
        page = SimpleNamespace(url="https://chatgpt.com/c/33333333-3333-4333-8333-333333333333")

        async def new_chat(self, **_kwargs):
            calls.append("new_chat")
            return "https://chatgpt.com/"

        async def assert_ownership(self):
            return SimpleNamespace(
                page_role="BOOTSTRAP",
                page_task_id=None,
                page_team=None,
                url="https://chatgpt.com/c/33333333-3333-4333-8333-333333333333",
            )

    class Actions(FakeActions):
        async def acquire_global_role(self, physical_role, *, allow_create=True):
            assert allow_create is True
            calls.append(("acquire_global_role", physical_role))
            return AcquiredRole(
                client=Client(),
                page_id="bootstrap-global",
                url="https://chatgpt.com/",
                created=True,
                new_chat=False,
            )

    class FakeBlock:
        def __init__(self, prompt, **kwargs):
            source_context = kwargs["source_context"]
            assert source_context["kind"] == "bootstrap_prewarm"
            assert source_context["origin_task_id"] == state["task_id"]
            assert "task_id" not in source_context
            assert "team" not in source_context
            calls.append(("block", prompt, kwargs))

        async def run(self, context):
            calls.append(("block.run", context.client.binding.role))
            return {
                "receipt": {
                    "conversation_id": "33333333-3333-4333-8333-333333333333"
                },
                "response": {
                    "message_id": "44444444-4444-4444-8444-444444444444",
                    "role": "assistant",
                    "turn_id": "55555555-5555-4555-8555-555555555555",
                    "text": "bootstrap ready",
                    "attachments": [],
                },
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeBlock)
    original_gate = worker._run_automated_send

    async def tracked_gate(operation):
        calls.append("send_gate")
        return await original_gate(operation)

    monkeypatch.setattr(worker, "_run_automated_send", tracked_gate)

    updated = asyncio.run(
        worker._regenerate_bootstrap_donor(state, record, Actions())
    )

    donor = {
        "conversation_id": "33333333-3333-4333-8333-333333333333",
        "assistant_message_id": "44444444-4444-4444-8444-444444444444",
    }
    assert updated is not None
    assert updated["donors"] == [donor]
    assert catalog.get(record["bootstrap_id"])["donors"] == [donor]
    assert state["bootstrap_prewarm_donor"] == donor
    assert ("acquire_global_role", "BOOTSTRAP") in calls
    assert "new_chat" in calls
    assert "send_gate" in calls


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_exhausted_source_without_prewarm_blocks_without_fresh_fallback(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(donors=[]))
    store = TaskStore(config)
    state = store.create_task(
        "lost donors",
        requested_team="donor-lost",
        task_id="task-donor-lost",
        bootstrap=record,
        governing_objective="lost donors",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)

    class Actions(FakeActions):
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, _conversation_id):
            raise worker_module.BackendUnavailableError(404, "conversation")

    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", Actions()))

    assert acquired is None
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "bootstrap_unavailable"
    assert state["bootstrap_source_requires_local_materialization"] == record["source_conversation_id"]
    assert "select another bootstrap" in state["block_reason"].lower()


def test_accepted_first_role_self_clones_child_local_bootstrap_donor(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(max_backups=2))
    store = TaskStore(config)
    state = store.create_task(
        "capture descendant donor",
        requested_team="donor-capture",
        task_id="task-donor-capture",
        bootstrap=record,
        governing_objective="capture descendant donor",
        governing_directives=(),
    )
    role = state["roles"]["PLAN"]
    role["context_source"] = "bootstrap_donor"
    role["conversation_generation"] = 1
    role["bootstrap_source_donor"] = dict(record["donors"][0])
    hop = _active_hop(state)
    child_conversation = "33333333-3333-4333-8333-333333333333"
    inherited_assistant = "44444444-4444-4444-8444-444444444444"
    accepted_user = "55555555-5555-4555-8555-555555555555"
    role_response = "66666666-6666-4666-8666-666666666666"
    hop["receipt"] = {
        "conversation_id": child_conversation,
        "user_message_id": accepted_user,
    }
    worker = CDPAWorker(config, store=store)
    snapshot = SimpleNamespace(
        messages=(
            MessageSnapshot("assistant", inherited_assistant, "turn-bootstrap", "bootstrap prefix", ()),
            MessageSnapshot("user", accepted_user, "turn-user", "PLAN prompt", ()),
            MessageSnapshot("assistant", role_response, "turn-response", "PLAN response", ()),
        )
    )
    client = SimpleNamespace(assert_ownership=AsyncMock(return_value=snapshot))
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url=f"https://chatgpt.com/c/{child_conversation}",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

        async def backend_conversation(self, _conversation_id):
            raise AssertionError("bootstrap donor capture must use local transcript state")

    captured = asyncio.run(worker._capture_bootstrap_role_donor(state, hop, Actions()))

    donor = {
        "conversation_id": child_conversation,
        "assistant_message_id": inherited_assistant,
    }
    assert captured is True
    assert state["bootstrap_task_donors"][0] == donor
    assert role["bootstrap_donor"] == donor
    assert catalog.get(record["bootstrap_id"])["donors"] == [record["donors"][0], donor]


def test_aliased_role_conversation_is_not_persisted_as_descendant_bootstrap_donor(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    record = catalog.upsert(donor_pool_record(max_backups=2))
    source_donor = dict(record["donors"][0])
    store = TaskStore(config)
    state = store.create_task(
        "reject aliased descendant donor",
        requested_team="donor-alias",
        task_id="task-donor-alias",
        bootstrap=record,
        governing_objective="reject aliased descendant donor",
        governing_directives=(),
    )
    role = state["roles"]["PLAN"]
    role["context_source"] = "bootstrap_donor"
    role["conversation_generation"] = 1
    role["bootstrap_source_donor"] = source_donor
    hop = _active_hop(state)
    hop["receipt"] = {
        "conversation_id": source_donor["conversation_id"],
        "user_message_id": "55555555-5555-4555-8555-555555555555",
    }
    worker = CDPAWorker(config, store=store)

    class Actions:
        async def backend_conversation(self, _conversation_id):
            raise AssertionError("aliased donor must be rejected before backend capture")

    captured = asyncio.run(worker._capture_bootstrap_role_donor(state, hop, Actions()))

    assert captured is False
    assert state.get("bootstrap_task_donors") in (None, [])
    assert role.get("bootstrap_donor") is None
    assert catalog.get(record["bootstrap_id"])["donors"] == record["donors"]


def _accept_self_route_guard_decision(worker, state, route: str):
    hop = _active_hop(state)
    state["roles"][str(hop["target_role"])]["turn"] = int(hop["turn"])
    handoff = (
        f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_"
        f"{state['task_id']}.md"
    )
    hop["expected_report_path"] = handoff
    report = Path(str(state["repository"])) / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"# {hop['physical_role']} turn {hop['turn']} report\n", encoding="utf-8")
    hop["response"] = json.dumps({"route": route, "handoff": handoff})
    hop["state"] = "responded"
    worker._responded(state, hop)
    return hop


def test_self_routes_use_shared_execution_budget_instead_of_local_guard(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-self-route-budget")

    first = _accept_self_route_guard_decision(worker, state, "PLAN")
    second = _accept_self_route_guard_decision(worker, state, "PLAN")
    third = _accept_self_route_guard_decision(worker, state, "PLAN")

    assert first["state"] == second["state"] == third["state"] == "routed"
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["active_hop_id"] == 4
    assert state["execution_budget"]["charged_hop_ids"] == [1, 2, 3, 4]


def test_role_changes_do_not_reset_shared_execution_budget(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-role-change-budget",
        roles=("PLAN", "DEV"),
    )

    for route in ("PLAN", "PLAN", "DEV", "DEV", "DEV", "DEV"):
        _accept_self_route_guard_decision(worker, state, route)

    assert state["status"] == "RUNNING"
    assert state["active_role"] == "DEV"
    assert state["execution_budget"]["charged_hop_ids"] == list(range(1, 8))


def test_custom_workflow_roles_share_the_same_execution_budget(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    custom = store.create_workflow_agent(
        display_name="Custom loop role",
        system_prompt="Perform the assigned custom workflow role.",
        continuation_contract="Continue the assigned custom workflow role on later turns.",
        external_command_id="create-custom-loop-role",
    )["route_key"]
    state = store.create_task(
        "Exercise custom workflow budget",
        requested_team="custom-loop",
        task_id="task-custom-budget",
        roles=("PLAN", custom),
        governing_objective="Exercise custom workflow budget",
        governing_directives=(),
    )
    worker = CDPAWorker(config, store=store)

    for _ in range(4):
        _accept_self_route_guard_decision(worker, state, custom)

    assert custom.startswith("WF_")
    assert state["status"] == "RUNNING"
    assert state["active_hop_id"] == 5
    assert state["execution_budget"]["charged_hop_ids"] == [1, 2, 3, 4, 5]


def test_route_repair_execution_is_charged_once_by_hop_identity(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-route-repair-budget")

    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    malformed = _active_hop(state)
    malformed["response"] = "not json"
    malformed["state"] = "responded"
    worker._responded(state, malformed)
    repair = _active_hop(state)

    assert repair["kind"] == "route_repair"
    assert state["execution_budget"]["charged_hop_ids"] == [1, 2, 3, 4]
    assert len(set(state["execution_budget"]["charged_hop_ids"])) == 4


def test_alternating_dev_review_loop_does_not_force_context_rotation(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-alternating-budget",
        roles=("PLAN", "DEV", "REVIEW"),
    )

    for route in ("DEV", "REVIEW", "DEV", "REVIEW", "DEV", "REVIEW", "DEV"):
        _accept_self_route_guard_decision(worker, state, route)

    assert state["status"] == "RUNNING"
    assert state["active_role"] == "DEV"
    assert state["roles"]["DEV"]["reset_requested"] is False
    assert state["execution_budget"]["charged_hop_ids"] == list(range(1, 9))
    assert not any(
        event.get("kind") == "alternating_route_context_rotation"
        for event in state["route_timeline"]
    )


def test_legacy_manifest_lazily_initializes_budget_from_existing_hop_ids(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-legacy-budget-migration")
    state.pop("execution_budget", None)
    state["hops"] = [
        {"hop_id": 1, "kind": "handoff"},
        {"hop_id": 2, "kind": "report_repair"},
    ]

    budget = worker._execution_budget(state)

    assert budget["epoch"] == 1
    assert budget["limit"] == 12
    assert budget["charged_hop_ids"] == [1, 2]


def test_goal_revision_does_not_reset_shared_execution_budget(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-goal-budget-stable")

    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    boundary = int(state["active_hop_id"])
    state["goal_revisions"] = [
        {
            "revision": 1,
            "changed_at": utc_now(),
            "applies_from_hop_id": boundary,
            "goal": "Revised goal",
            "external_command_id": "goal-reset-self-route",
        }
    ]
    state["effective_goal"] = "Revised goal"

    _accept_self_route_guard_decision(worker, state, "PLAN")

    assert state["status"] == "RUNNING"
    assert state["active_hop_id"] == boundary + 1
    assert state["execution_budget"]["epoch"] == 1
    assert state["execution_budget"]["charged_hop_ids"] == [1, 2, 3, 4]


@pytest.mark.parametrize("operational_kind", ["role_restart", "control"])
def test_allocated_operational_hops_count_once_in_shared_budget(
    tmp_path: Path, operational_kind: str
):
    _, _, state, worker = setup_task(
        tmp_path, task_id=f"task-budget-{operational_kind}"
    )
    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")

    operational = _active_hop(state)
    operational["kind"] = operational_kind
    _accept_self_route_guard_decision(worker, state, "PLAN")

    assert state["status"] == "RUNNING"
    assert state["execution_budget"]["charged_hop_ids"] == [1, 2, 3, 4]
    assert len(set(state["execution_budget"]["charged_hop_ids"])) == 4


@pytest.mark.parametrize("action", ["open_tab", "new_chat"])
def test_rate_limit_defers_only_missing_tab_control(tmp_path: Path, action: str):
    _, _, state, worker = setup_task(
        tmp_path, task_id=f"task-rate-limit-missing-{action}"
    )
    worker._rate_limit_cooldown = {
        "state": "active",
        "detected_at": "2026-08-17T00:00:00+00:00",
        "release_not_before": "2999-01-01T00:00:00+00:00",
    }
    state["controls"] = [{
        "control_id": 1, "action": action, "role": "PLAN",
        "reason": "new tab is paused", "confirmed": False,
        "status": "requested", "requested_at": "2026-08-17T00:00:00+00:00",
        "applied_at": None, "result": None,
    }]

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return None
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("cooldown must not reopen/create a missing tab")
        async def new_chat(self, *_args, **_kwargs):
            raise AssertionError("cooldown must not create a missing tab")

    assert asyncio.run(worker._apply_control(state, Actions())) is True
    control = state["controls"][0]
    assert control["status"] == "applied"
    assert control["result"] == {"deferred": True, "reason": "rate_limit_cooldown"}
    assert state["active_action"] == "rate_limit_cooldown"


@pytest.mark.parametrize("status", ["DONE", "STOPPED", "PAUSED", "BLOCKED"])
def test_ambient_old_registered_tabs_keep_auto_allow(status: str, tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id=f"task-ambient-history-{status.lower()}"
    )
    task_id = f"task-ambient-history-{status.lower()}"
    worker.registry = SimpleNamespace(tasks_by_id={task_id: {"status": status}})
    calls = []

    class Page:
        url = "https://chatgpt.com/c/ambient-history"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": task_id,
                "pageId": "page-history",
                "role": "PLAN",
            })

    page = Page()

    class Client:
        def __init__(self, _page, *, timeout_ms):
            self.page = _page

        async def install_page_observer(self):
            calls.append("listen")

        async def read_wait_probe(self):
            return SimpleNamespace(
                page_task_id=task_id,
                page_id="page-history",
                page_role="PLAN",
                stop_visible=False,
                last_assistant_message_id=None,
                mcp_permission_allow_count=0,
                mcp_permission_node_count=0,
            )

        def page_observation(self):
            return {"permission_action": {
                "type": "allow",
                "target_message_id": "history-call",
                "remember_answer": True,
                "label": "Allow mcp-g8 for this conversation",
            }}

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            calls.append(("allow", passive_action["target_message_id"]))
            return {"method": "react_handler", "target_message_id": passive_action["target_message_id"]}

        def clear_permission_action(self):
            calls.append("clear")

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    worker._ambient_controller_states[id(page)] = {
        "mcp_allow_seen_at": (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat(),
        "mcp_allow_seen_target": "history-call",
    }
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[page])))

    if status in {"DONE", "STOPPED"}:
        assert calls == []
    else:
        assert ("allow", "history-call") in calls
        assert "clear" in calls


def test_worker_source_identity_detects_runtime_config_drift(tmp_path: Path):
    config, _store, _state, worker = setup_task(tmp_path, task_id="task-source-drift")
    config_path = config.repository_root / "cdpa.yaml"
    assert config_path.exists()
    baseline = worker._source_identity["fingerprint"]
    assert worker._refresh_source_drift(force=True)["drifted"] is False

    config_path.write_text(config_path.read_text(encoding="utf-8") + "\n# source-drift-test\n", encoding="utf-8")

    drift = worker._refresh_source_drift(force=True)
    assert drift["drifted"] is True
    assert "cdpa.yaml" in drift["changed"]
    assert worker._source_identity["fingerprint"] == baseline

    restarted = CDPAWorker(config, store=_store)
    assert restarted._source_identity["fingerprint"] != baseline
    assert restarted._refresh_source_drift(force=True)["drifted"] is False


def test_worker_source_identity_covers_dashboard_runtime_but_ignores_task_artifacts(tmp_path: Path):
    config, _store, _state, worker = setup_task(tmp_path, task_id="task-source-runtime-surface")
    package = Path(worker_module.__file__).resolve().parent
    dashboard_api = package / "dashboard_api.py"
    assert dashboard_api in worker._source_identity_paths()

    dashboard_asset = package / "dashboard_assets" / "app.js"
    if dashboard_asset.exists():
        assert dashboard_asset in worker._source_identity_paths()

    task_artifact = config.repository_root / ".plan" / "noise.md"
    task_artifact.parent.mkdir(parents=True, exist_ok=True)
    task_artifact.write_text("runtime-irrelevant", encoding="utf-8")
    assert worker._refresh_source_drift(force=True)["drifted"] is False


def test_worker_heartbeat_exposes_lease_and_source_identity(tmp_path: Path):
    _config, _store, _state, worker = setup_task(tmp_path, task_id="task-heartbeat-identity")
    worker.runtime_db.ensure_schema()
    worker._worker_lease_acquired_at = "2026-09-15T12:00:00+00:00"
    worker._publish_heartbeat(force=True)

    snapshot = worker.runtime_db.get_snapshot("worker")
    payload = snapshot["payload"]
    assert payload["lease"]["acquired"] is True
    assert payload["lease"]["owner_pid"]
    assert payload["source_identity"]["fingerprint"] == worker._source_identity["fingerprint"]
    assert payload["reload_required"] is False


def test_ambient_old_tab_visible_allow_is_clicked_without_passive_event(tmp_path: Path, monkeypatch):
    _config, _store, _state, worker = setup_task(
        tmp_path, task_id="task-ambient-history-dom-allow"
    )
    task_id = "task-ambient-history-dom-allow"
    worker.registry = SimpleNamespace(tasks_by_id={task_id: {"status": "DONE"}})
    calls = []

    class Page:
        url = "https://chatgpt.com/c/ambient-history-dom"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": task_id,
                "pageId": "page-history-dom",
                "role": "cdpa-history-dom-plan",
            })

    page = Page()

    class Client:
        def __init__(self, _page, *, timeout_ms):
            self.binding = None

        async def install_page_observer(self):
            pass

        async def read_wait_probe(self):
            return SimpleNamespace(
                page_task_id=task_id,
                page_id="page-history-dom",
                page_role="cdpa-history-dom-plan",
                stop_visible=False,
                last_assistant_message_id=None,
                mcp_permission_allow_count=1,
                mcp_permission_node_count=0,
            )

        def page_observation(self):
            return {}

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            assert self.binding == PageBinding("page-history-dom", "cdpa-history-dom-plan")
            calls.append(passive_action)
            return {"method": "dom_click", "target_message_id": "", "remember_answer": "false"}

        def clear_permission_action(self):
            pass

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    worker.runtime_db.ensure_schema()
    worker._ambient_controller_states[id(page)] = {
        "mcp_allow_seen_at": (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat(),
        "mcp_allow_seen_target": "",
    }
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[page])))

    assert calls == []


def test_open_tab_recovers_receiptless_observed_response_tab_loss_to_presend_fresh(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-receiptless-observed"
    )
    worker.runtime_db.ensure_schema()
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    original_request_id = hop["request_id"]
    hop["state"] = "waiting"
    hop["observed_existing_response"] = True
    hop["receipt"] = None
    hop["message_identity"] = None
    hop["wait"]["started_at"] = "2026-09-23T00:00:00+00:00"
    hop["wait"]["deadline_at"] = "2026-09-23T00:05:00+00:00"
    hop["timestamps"]["waiting_at"] = "2026-09-23T00:00:00+00:00"
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
        active_action="blocked",
    )
    state["roles"]["PLAN"].update(
        page_id="lost-observed-page",
        page_url="https://chatgpt.com/",
        online=False,
        status="pending",
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover lost receiptless observed response",
            "confirmed": False,
            "status": "requested",
            "requested_at": "2026-09-23T00:01:00+00:00",
            "applied_at": None,
            "result": None,
        }
    ]
    store.save(path, state)

    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="fresh-recovered-page",
        url="https://chatgpt.com/",
        created=True,
        new_chat=True,
    )

    class RecoveryActions:
        def __init__(self):
            self.fresh_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def fresh_chat(self, _state, role, *, url, clean_for_send=False):
            self.fresh_calls.append((role, url, clean_for_send))
            return acquired

    actions = RecoveryActions()
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: actions,
    )

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    recovered_hop = _active_hop(recovered)
    assert recovered["status"] == "RUNNING"
    assert recovered["block_code"] is None
    assert recovered["active_action"] == "resuming"
    assert recovered_hop["state"] == "pre_send"
    assert recovered_hop["request_id"] == original_request_id
    assert "observed_existing_response" not in recovered_hop
    assert recovered_hop["receipt"] is None
    assert recovered["roles"]["PLAN"]["online"] is True
    assert recovered["roles"]["PLAN"]["page_id"] == "fresh-recovered-page"
    assert actions.fresh_calls == [("PLAN", "https://chatgpt.com/", True)]
    assert recovered["controls"][0]["status"] == "applied"
    assert recovered["controls"][0]["result"]["resumed"] is True


def test_independent_reset_is_endpointless_only_without_owned_page():
    state = {
        "task_mode": "independent",
        "active_role": "AGENT",
        "active_hop_id": 1,
        "roles": {"AGENT": {"page_id": None, "page_url": None}},
        "hops": [{"hop_id": 1, "state": "pre_send"}],
    }
    control = {"control_id": 1, "action": "reset", "status": "requested"}
    assert worker_module._control_can_run_endpointless(state, control) is True
    state["roles"]["AGENT"]["page_id"] = "owned-page"
    assert worker_module._control_can_run_endpointless(state, control) is False


def test_independent_reset_supersedes_older_requested_resume():
    state = {
        "task_mode": "independent",
        "controls": [
            {"control_id": 1, "action": "resume", "status": "requested"},
            {"control_id": 2, "action": "reset", "status": "requested"},
        ],
    }
    selected = worker_module._next_requested_control(state)
    assert selected is not None
    assert selected["control_id"] == 2
    assert selected["action"] == "reset"


def test_normal_cdpa_send_uses_single_attempt_no_recovery_reload(tmp_path: Path):
    """Fix A: Normal _pre_send DurableSendBlock must use max_attempts=1, recovery_reload=False."""
    _, _, state, worker = setup_task(tmp_path, task_id="task-send-config")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    assert len(client.send_calls) == 1
    assert client.send_max_attempts == [1], (
        f"Normal CDPA send must use max_attempts=1, got {client.send_max_attempts}"
    )
    assert client.send_recovery_reload == [False], (
        f"Normal CDPA send must use recovery_reload=False, got {client.send_recovery_reload}"
    )
