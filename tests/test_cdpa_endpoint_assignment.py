from __future__ import annotations

import json
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
from playwright_auto.cdpa_store import TaskStore
from test_cdpa_core import _stop_task, write_config


def stores(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return TaskStore(config), BrowserEndpointRegistry(config)


def test_task_snapshots_endpoint_and_all_roles(tmp_path):
    store, endpoints = stores(tmp_path)
    original = endpoints.resolve()
    second = endpoints.add("9202")
    task = store.create_task("First task", task_id="first", requested_team="alpha")
    endpoints.update(second["endpoint_id"], is_default=True)
    assert store.load(task["manifest_path"])["browser_endpoint_id"] == original["endpoint_id"]
    assert {role["browser_endpoint_id"] for role in task["roles"].values()} == {original["endpoint_id"]}
    later = store.create_task("Second task", task_id="second")
    assert later["browser_endpoint_id"] == second["endpoint_id"]


def test_explicit_browser_selection_accepts_registry_shorthand(tmp_path):
    store, endpoints = stores(tmp_path)
    second = endpoints.add("9202")
    task = store.create_task("Chosen browser", browser_endpoint_id="localhost:9202")
    assert task["browser_endpoint_id"] == second["endpoint_id"]
    with pytest.raises(ValueError, match="not registered"):
        store.create_task("Unknown browser", browser_endpoint_id="9302")


def test_exact_team_inherits_endpoint_not_current_default(tmp_path):
    store, endpoints = stores(tmp_path)
    first = store.create_task("First task", task_id="first", requested_team="alpha")
    second = endpoints.add("9202", is_default=True)
    queued = store.create_task("Next task", task_id="next", reuse_team=first["team"])
    assert queued["browser_endpoint_id"] == first["browser_endpoint_id"]
    assert queued["status"] == "WAITING"
    with pytest.raises(ValueError, match="endpoint.*locked"):
        store.create_task("Wrong browser", reuse_team=first["team"], browser_endpoint_id=second["endpoint_id"])


def test_temporary_is_creation_snapshot_and_temp_only_forces_true(tmp_path):
    store, endpoints = stores(tmp_path)
    endpoint = endpoints.resolve()
    regular = store.create_task("Persistent task")
    forced = store.create_task("Forced temporary", temporary=True)
    endpoints.update(endpoint["endpoint_id"], temporary_default=True)
    inherited = store.create_task("Inherited temporary")
    assert regular["conversation_mode"] == "persistent"
    assert forced["conversation_mode"] == inherited["conversation_mode"] == "temporary"
    endpoints.update(endpoint["endpoint_id"], temporary_default=False)
    assert store.load(inherited["manifest_path"])["conversation_mode"] == "temporary"
    with pytest.raises(ValueError, match="boolean"):
        store.create_task("Invalid temp", temporary="false")


def test_endpoint_and_mode_cannot_be_reassigned_by_save(tmp_path):
    store, endpoints = stores(tmp_path)
    task = store.create_task("Immutable assignment")
    second = endpoints.add("9202")
    for field, value in (("browser_endpoint_id", second["endpoint_id"]), ("conversation_mode", "temporary")):
        with pytest.raises(ValueError, match="immutable"):
            store.update(task["manifest_path"], lambda state: state.update({field: value}))
    assert store.load(task["manifest_path"])["browser_endpoint_id"] == task["browser_endpoint_id"]


def test_legacy_manifest_stays_on_original_endpoint_without_read_rewrite(tmp_path):
    store, endpoints = stores(tmp_path)
    task = store.create_task("Legacy task")
    task.pop("browser_endpoint_id", None)
    task.pop("conversation_mode", None)
    for role in task["roles"].values():
        role.pop("browser_endpoint_id", None)
    path = Path(task["manifest_path"])
    path.write_text(json.dumps(task), encoding="utf-8")
    before = path.read_bytes()
    endpoints.add("9202", is_default=True)
    loaded = store.load(path)
    assert "browser_endpoint_id" not in loaded
    assert store.endpoint_id_for(loaded) == endpoints.legacy_endpoint_id
    assert path.read_bytes() == before


def test_malformed_or_cross_endpoint_role_assignment_fails_validation(tmp_path):
    store, endpoints = stores(tmp_path)
    task = store.create_task("Role assignment")
    other = endpoints.add("9202")
    with pytest.raises(ValueError, match="role.*endpoint"):
        store.update(task["manifest_path"], lambda state: state["roles"]["PLAN"].update(browser_endpoint_id=other["endpoint_id"]))
    with pytest.raises(ValueError):
        store.update(task["manifest_path"], lambda state: state.update(conversation_mode="reopenable-temporary"))


def test_terminal_team_name_is_not_recycled_onto_another_endpoint(tmp_path):
    store, endpoints = stores(tmp_path)
    first = store.create_task("First task", requested_team="alpha")
    store.update(first["manifest_path"], lambda state: state.update(status="DONE", active_role=None, active_hop_id=None))
    second = endpoints.add("9202", is_default=True)
    later = store.create_task("Later task", requested_team="alpha")
    assert later["browser_endpoint_id"] == second["endpoint_id"]
    assert later["team"] != first["team"]
    assert first["team"] not in later["reusable_teams"]


def test_replacement_keeps_the_original_endpoint_and_conversation_mode(tmp_path):
    store, endpoints = stores(tmp_path)
    original = store.create_task("Continue existing workspace", temporary=True, requested_team="alpha")
    _stop_task(store, original)
    endpoints.add("9202", is_default=True)
    replacement = store.replace_task_and_rewire(
        original["task_id"], "Inspect existing reports before continuing.",
        reuse_team=True, rewire_children=False, incident_id="endpoint-recovery",
    )["replacement"]
    assert replacement["team"] == original["team"]
    assert replacement["browser_endpoint_id"] == original["browser_endpoint_id"]
    assert replacement["conversation_mode"] == original["conversation_mode"]


def test_independent_generation_keeps_endpoint_and_its_existing_mode_policy(tmp_path):
    store, endpoints = stores(tmp_path)
    first = store.create_independent_agent("Browser watcher", system_prompt="Inspect only.", temporary_chat=True)
    original = endpoints.resolve()["endpoint_id"]
    assert first["conversation_mode"] == "temporary"
    assert {role["browser_endpoint_id"] for role in first["roles"].values()} == {original}
    store.update(first["manifest_path"], lambda state: state.update(status="DONE", active_role=None, active_hop_id=None))
    endpoints.add("9202", is_default=True)
    later = store.create_independent_agent("Browser watcher", system_prompt="Inspect only.")
    assert later["browser_endpoint_id"] == original
    assert later["task_id"] != first["task_id"]


def test_new_work_for_draining_team_fails_without_reassignment(tmp_path):
    store, endpoints = stores(tmp_path)
    task = store.create_task("Draining team", requested_team="alpha")
    endpoints.add("9202", is_default=True)
    endpoints.remove(task["browser_endpoint_id"], has_nonterminal_tasks=True)
    with pytest.raises(ValueError, match="draining"):
        store.create_task("More work", reuse_team=task["team"])
    assert store.endpoint_id_for(store.load(task["manifest_path"])) == task["browser_endpoint_id"]
