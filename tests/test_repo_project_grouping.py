from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.durable import RequestLedger
from playwright_auto.chatgpt import (
    backend_create_project,
    backend_projects,
    backend_set_conversation_project,
)
from playwright_auto.chatgpt_graph import (
    BackendAuthError,
    BackendNotReadyError,
    BackendSchemaError,
    BackendUnavailableError,
)
from test_cdpa_core import write_config
from test_cdpa_worker import _prepare_sent_waiting_task


def make_worker(tmp_path: Path) -> tuple[TaskStore, CDPAWorker]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    return store, CDPAWorker(config, store=store)


def test_project_backend_contract_and_non_get_401_refresh():
    class Response:
        def __init__(self, status, payload):
            self.status, self.payload = status, payload
        async def json(self):
            if isinstance(self.payload, BaseException):
                raise self.payload
            return self.payload

    class Requests:
        def __init__(self):
            self.responses = [
                Response(200, {"accessToken": "one"}),
                Response(200, {"items": [{"gizmo": {"gizmo": {"id": "g-p-a", "display": {"name": "repo"}}}}]}),
                Response(401, {}),
                Response(200, {"accessToken": "two"}),
                Response(200, {"resource": {"gizmo": {"id": "g-p-b"}}}),
                Response(200, {}),
            ]
            self.calls = []
        async def _call(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)
        async def get(self, url, **kwargs): return await self._call("GET", url, **kwargs)
        async def post(self, url, **kwargs): return await self._call("POST", url, **kwargs)
        async def patch(self, url, **kwargs): return await self._call("PATCH", url, **kwargs)

    async def scenario():
        context = SimpleNamespace(request=Requests())
        assert await backend_projects(context) == [{"id": "g-p-a", "name": "repo"}]
        assert await backend_create_project(context, "repo") == "g-p-b"
        await backend_set_conversation_project(context, "conversation-1", "g-p-b")
        calls = context.request.calls
        assert calls[1][1].endswith("/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0")
        assert calls[2][0] == "POST" and calls[2][2]["data"] == {"name": "repo", "instructions": ""}
        assert calls[4][2]["headers"]["Authorization"] == "Bearer two"
        assert calls[5][0] == "PATCH" and calls[5][2]["data"] == {"gizmo_id": "g-p-b"}
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,error",
    [
        (403, BackendUnavailableError),
        (404, BackendNotReadyError),
        (429, BackendUnavailableError),
        (500, BackendUnavailableError),
    ],
)
def test_project_backend_write_failures_are_typed(status, error):
    class Response:
        def __init__(self, status, payload): self.status, self.payload = status, payload
        async def json(self): return self.payload
    class Requests:
        def __init__(self): self.calls = 0
        async def get(self, _url, **_kwargs): return Response(200, {"accessToken": "token"})
        async def post(self, _url, **_kwargs): return Response(status, {})
    async def scenario():
        with pytest.raises(error):
            await backend_create_project(SimpleNamespace(request=Requests()), "repo")
    asyncio.run(scenario())


def test_project_backend_timeout_is_unavailable():
    class Response:
        status = 200
        async def json(self): return {"accessToken": "token"}
    class Requests:
        async def get(self, _url, **_kwargs): return Response()
        async def post(self, _url, **_kwargs): raise TimeoutError("bounded timeout")
    async def scenario():
        with pytest.raises(BackendUnavailableError):
            await backend_create_project(SimpleNamespace(request=Requests()), "repo")
    asyncio.run(scenario())


def test_project_backend_schema_drift_fails_closed():
    class Response:
        def __init__(self, payload): self.status, self.payload = 200, payload
        async def json(self): return self.payload
    class Requests:
        async def get(self, url, **_kwargs):
            return Response({"accessToken": "token"}) if url.endswith("/api/auth/session") else Response({"items": [{}]})
    async def scenario():
        with pytest.raises(BackendSchemaError):
            await backend_projects(SimpleNamespace(request=Requests()))
    asyncio.run(scenario())


def test_repository_project_catalog_round_trip_preserves_unrelated_keys(tmp_path: Path):
    store, _worker = make_worker(tmp_path)
    state = store.create_task("x", requested_team="alpha", task_id="task-a")
    catalog = json.loads(store.catalog_path.read_text())
    catalog["operator_key"] = {"keep": True}
    store.catalog_path.write_text(json.dumps(catalog))
    repo = tmp_path / "repos" / "alpha"
    store.set_repository_project(repo, "g-p-one")
    saved = json.loads(store.catalog_path.read_text())
    assert saved["operator_key"] == {"keep": True}
    assert saved["entries"]
    assert store.repository_projects() == {str(repo.resolve()): "g-p-one"}
    assert state["task_id"] == "task-a"


def test_malformed_repository_project_catalog_fails_only_accessor(tmp_path: Path):
    store, _worker = make_worker(tmp_path)
    store.create_task("x", requested_team="alpha", task_id="task-a")
    catalog = json.loads(store.catalog_path.read_text())
    catalog["repository_projects"] = []
    store.catalog_path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError):
        store.repository_projects()
    assert store.discover()[0]["task_id"] == "task-a"


def test_repository_project_catalog_rejects_shared_project_id(tmp_path: Path):
    store, _worker = make_worker(tmp_path)
    store.create_task("x", requested_team="alpha", task_id="task-a")
    repo_a = str((tmp_path / "a" / "repo").resolve())
    repo_b = str((tmp_path / "b" / "repo").resolve())
    store.set_repository_project(repo_a, "g-p-shared")

    with pytest.raises(ValueError):
        store.set_repository_project(repo_b, "g-p-shared")
    assert store.repository_projects() == {repo_a: "g-p-shared"}

    catalog = json.loads(store.catalog_path.read_text())
    catalog["repository_projects"] = {repo_a: "g-p-shared", repo_b: "g-p-shared"}
    store.catalog_path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError):
        store.repository_projects()
    assert store.discover()[0]["task_id"] == "task-a"


def test_grouping_mapping_hit_skips_resolve_and_concurrent_miss_creates_once(tmp_path: Path, monkeypatch):
    store, worker = make_worker(tmp_path)
    repo = str((tmp_path / "repo").resolve())
    calls = {"list": 0, "create": 0, "patch": []}

    async def listed(_ctx):
        calls["list"] += 1
        await asyncio.sleep(0.01)
        return []
    async def created(_ctx, _name):
        calls["create"] += 1
        return "g-p-created"
    async def patched(_ctx, conversation_id, project_id):
        calls["patch"].append((conversation_id, project_id))
    monkeypatch.setattr(worker_module, "backend_projects", listed)
    monkeypatch.setattr(worker_module, "backend_create_project", created)
    monkeypatch.setattr(worker_module, "backend_set_conversation_project", patched)

    async def scenario():
        await asyncio.gather(
            worker._group_repository_project(repo, "c1", object()),
            worker._group_repository_project(repo, "c2", object()),
        )
        assert calls == {"list": 1, "create": 1, "patch": [("c1", "g-p-created"), ("c2", "g-p-created")]}
        calls["list"] = calls["create"] = 0
        calls["patch"].clear()
        fresh = CDPAWorker(worker.config, store=store)
        await fresh._group_repository_project(repo, "c3", object())
        assert calls == {"list": 0, "create": 0, "patch": [("c3", "g-p-created")]}
    asyncio.run(scenario())


def test_grouping_patch_failure_is_fail_soft_and_repeat_is_tolerated(tmp_path: Path, monkeypatch):
    store, worker = make_worker(tmp_path)
    repository = str((tmp_path / "repo").resolve())
    store.set_repository_project(repository, "g-p-existing")
    task = store.create_task("x", requested_team="alpha", task_id="task-a", repository=repository)
    before = store.load(task["manifest_path"])
    calls = []

    async def patched(_ctx, conversation_id, project_id):
        calls.append((conversation_id, project_id))
        if len(calls) == 1:
            raise TimeoutError("membership timeout")

    monkeypatch.setattr(worker_module, "backend_set_conversation_project", patched)

    async def scenario():
        await worker._group_repository_project(repository, "same-conversation", object())
        await worker._group_repository_project(repository, "same-conversation", object())

    asyncio.run(scenario())
    assert calls == [
        ("same-conversation", "g-p-existing"),
        ("same-conversation", "g-p-existing"),
    ]
    assert store.load(task["manifest_path"]) == before


def test_grouping_collision_suffix_and_ambiguous_remote_fail_soft(tmp_path: Path, monkeypatch):
    store, worker = make_worker(tmp_path)
    repo_a = str((tmp_path / "a" / "same").resolve())
    repo_b = str((tmp_path / "b" / "same").resolve())
    store.set_repository_project(repo_a, "g-p-a")
    names, patches = [], []

    async def listed(_ctx): return []
    async def created(_ctx, name):
        names.append(name)
        return "g-p-b"
    async def patched(_ctx, conversation_id, project_id): patches.append((conversation_id, project_id))
    monkeypatch.setattr(worker_module, "backend_projects", listed)
    monkeypatch.setattr(worker_module, "backend_create_project", created)
    monkeypatch.setattr(worker_module, "backend_set_conversation_project", patched)
    asyncio.run(worker._group_repository_project(repo_b, "c-b", object()))
    assert names == [f"same-{hashlib.sha256(repo_b.encode()).hexdigest()[:8]}"]
    assert store.repository_projects()[repo_b] == "g-p-b"

    store2, worker2 = make_worker(tmp_path / "amb")
    repo = str((tmp_path / "amb" / "repo").resolve())
    created_calls = []
    async def ambiguous(_ctx): return [{"id": "g-p-existing", "name": "repo"}]
    async def must_not_create(_ctx, name): created_calls.append(name); return "g-p-new"
    monkeypatch.setattr(worker_module, "backend_projects", ambiguous)
    monkeypatch.setattr(worker_module, "backend_create_project", must_not_create)
    asyncio.run(worker2._group_repository_project(repo, "c", object()))
    assert created_calls == []
    assert repo not in store2.repository_projects()


def test_advance_persists_before_schedule_and_done_is_eligible(tmp_path: Path, monkeypatch):
    repository = str((tmp_path / "current-repo").resolve())
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-route", repository=repository
    )
    enriched = replace(receipt, conversation_id="reused-conversation")
    RequestLedger(hop["ledger_path"]).update(
        hop["request_id"], receipt=enriched.to_dict()
    )
    hop["receipt"] = enriched.to_dict()
    hop["state"] = "responded"
    hop["response"] = json.dumps(
        {"route": "DONE", "handoff": ".plan/alpha/alpha-plan_turn1_task-route.md"}
    )
    state = store.save(path, state)
    order = []
    original_persist = worker._persist_transport_result

    def persist(*args, **kwargs):
        saved = original_persist(*args, **kwargs)
        order.append(("persist", saved["repository"], saved["status"]))
        return saved

    def schedule(saved, hop_id, _context):
        completed = next(item for item in saved["hops"] if item["hop_id"] == hop_id)
        order.append(
            (
                "schedule",
                saved["repository"],
                saved["status"],
                completed["state"],
                completed["receipt"]["conversation_id"],
            )
        )

    monkeypatch.setattr(worker, "_persist_transport_result", persist)
    monkeypatch.setattr(worker, "_schedule_repository_project", schedule)
    saved = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert saved["status"] == "DONE"
    assert order == [
        ("persist", repository, "DONE"),
        ("schedule", repository, "DONE", "routed", "reused-conversation"),
    ]


def test_reused_conversation_schedules_against_each_current_repository(tmp_path: Path, monkeypatch):
    _store, worker = make_worker(tmp_path)
    seen = []

    async def grouped(repository, conversation_id, _context):
        seen.append((repository, conversation_id))

    monkeypatch.setattr(worker, "_group_repository_project", grouped)

    async def scenario():
        for repository in (tmp_path / "repo-a", tmp_path / "repo-b"):
            canonical = str(repository.resolve())
            state = {
                "repository": canonical,
                "hops": [
                    {
                        "hop_id": 1,
                        "state": "routed",
                        "kind": "normal",
                        "receipt": {"conversation_id": "same-conversation"},
                    }
                ],
            }
            worker._schedule_repository_project(state, 1, object())
        await asyncio.gather(*tuple(worker._repository_project_tasks))

    asyncio.run(scenario())
    assert seen == [
        (str((tmp_path / "repo-a").resolve()), "same-conversation"),
        (str((tmp_path / "repo-b").resolve()), "same-conversation"),
    ]


def test_schedule_filters_missing_receipt_and_route_repair_and_is_nonblocking(tmp_path: Path, monkeypatch):
    _store, worker = make_worker(tmp_path)
    repo = str((tmp_path / "repo").resolve())
    started = []
    gate = asyncio.Event()
    async def delayed(repository, conversation_id, _ctx):
        started.append((repository, conversation_id))
        await gate.wait()
    monkeypatch.setattr(worker, "_group_repository_project", delayed)

    async def scenario():
        base = {"repository": repo, "hops": [{"hop_id": 1, "state": "routed", "kind": "normal", "receipt": {}}]}
        worker._schedule_repository_project(base, 1, object())
        assert not worker._repository_project_tasks
        repair = {"repository": repo, "hops": [{"hop_id": 2, "state": "routed", "kind": "route_repair", "receipt": {"conversation_id": "c2"}}]}
        worker._schedule_repository_project(repair, 2, object())
        assert not worker._repository_project_tasks
        valid = {"repository": repo, "hops": [{"hop_id": 3, "state": "routed", "kind": "normal", "receipt": {"conversation_id": "c3"}}]}
        worker._schedule_repository_project(valid, 3, object())
        await asyncio.sleep(0)
        assert started == [(repo, "c3")]
        assert len(worker._repository_project_tasks) == 1
        gate.set()
        await asyncio.gather(*tuple(worker._repository_project_tasks))
        await asyncio.sleep(0)
        assert not worker._repository_project_tasks
    asyncio.run(scenario())
