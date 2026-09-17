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


def test_task_snapshots_initial_endpoint_affinity_but_role_placement_can_move(tmp_path):
    store, endpoints = stores(tmp_path)
    original = endpoints.resolve()
    second = endpoints.add("9202")
    task = store.create_task("First task", task_id="first", requested_team="alpha")
    endpoints.update(second["endpoint_id"], is_default=True)

    assert store.load(task["manifest_path"])["browser_endpoint_id"] == original["endpoint_id"]
    moved = store.update(
        task["manifest_path"],
        lambda state: state["roles"]["PLAN"].update(browser_endpoint_id=second["endpoint_id"]),
    )
    assert moved["roles"]["PLAN"]["browser_endpoint_id"] == second["endpoint_id"]
    assert moved["browser_endpoint_id"] == original["endpoint_id"]
    later = store.create_task("Second task", task_id="second")
    assert later["browser_endpoint_id"] == second["endpoint_id"]


def test_explicit_browser_selection_accepts_registry_shorthand(tmp_path):
    store, endpoints = stores(tmp_path)
    second = endpoints.add("9202")
    task = store.create_task("Chosen browser", browser_endpoint_id="localhost:9202")
    assert task["browser_endpoint_id"] == second["endpoint_id"]
    with pytest.raises(ValueError, match="not registered"):
        store.create_task("Unknown browser", browser_endpoint_id="9302")


def test_workflow_required_browser_is_canonical_hard_affinity_and_immutable(tmp_path):
    store, endpoints = stores(tmp_path)
    required = endpoints.add("9202")
    other = endpoints.add("9212")
    task = store.create_task(
        "Gmail child",
        required_browser_endpoint_id="localhost:9202",
    )

    assert task["required_browser_endpoint_id"] == required["endpoint_id"]
    with pytest.raises(ValueError, match="not registered"):
        store.create_task(
            "Unknown Gmail browser",
            required_browser_endpoint_id="9302",
        )
    with pytest.raises(ValueError, match="immutable"):
        store.update(
            task["manifest_path"],
            lambda state: state.update(
                required_browser_endpoint_id=other["endpoint_id"]
            ),
        )


def test_exact_team_new_work_can_choose_a_different_endpoint(tmp_path):
    store, endpoints = stores(tmp_path)
    first = store.create_task("First task", task_id="first", requested_team="alpha")
    second = endpoints.add("9202", is_default=True)
    queued = store.create_task(
        "Next task",
        task_id="next",
        reuse_team=first["team"],
        browser_endpoint_id=second["endpoint_id"],
    )
    assert queued["browser_endpoint_id"] == second["endpoint_id"]
    assert queued["status"] == "WAITING"


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


def test_initial_endpoint_and_mode_are_durable_policy_not_role_placement(tmp_path):
    store, endpoints = stores(tmp_path)
    task = store.create_task("Durable policy")
    second = endpoints.add("9202")
    with pytest.raises(ValueError, match="immutable"):
        store.update(
            task["manifest_path"],
            lambda state: state.update(browser_endpoint_id=second["endpoint_id"]),
        )
    with pytest.raises(ValueError, match="immutable"):
        store.update(
            task["manifest_path"],
            lambda state: state.update(conversation_mode="temporary"),
        )
    moved = store.update(
        task["manifest_path"],
        lambda state: state["roles"]["DEV"].update(browser_endpoint_id=second["endpoint_id"]),
    )
    assert moved["roles"]["DEV"]["browser_endpoint_id"] == second["endpoint_id"]


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


def test_role_endpoint_requires_a_durable_identifier_but_not_task_equality(tmp_path):
    store, endpoints = stores(tmp_path)
    task = store.create_task("Role placement")
    other = endpoints.add("9202")
    moved = store.update(
        task["manifest_path"],
        lambda state: state["roles"]["PLAN"].update(browser_endpoint_id=other["endpoint_id"]),
    )
    assert moved["roles"]["PLAN"]["browser_endpoint_id"] == other["endpoint_id"]
    with pytest.raises(ValueError, match="browser_endpoint_id"):
        store.update(
            task["manifest_path"],
            lambda state: state["roles"]["PLAN"].update(browser_endpoint_id="bad endpoint"),
        )


def test_terminal_team_reuse_metadata_is_not_endpoint_scoped(tmp_path):
    store, endpoints = stores(tmp_path)
    first = store.create_task("First task", requested_team="alpha")
    store.update(
        first["manifest_path"],
        lambda state: state.update(status="DONE", active_role=None, active_hop_id=None),
    )
    second = endpoints.add("9202", is_default=True)
    later = store.create_task("Later task", requested_team="alpha")
    assert later["browser_endpoint_id"] == second["endpoint_id"]
    assert first["team"] in later["reusable_teams"]


def test_replacement_keeps_conversation_mode_without_endpoint_ownership_semantics(tmp_path):
    store, endpoints = stores(tmp_path)
    original = store.create_task("Continue existing workspace", temporary=True, requested_team="alpha")
    _stop_task(store, original)
    endpoints.add("9202", is_default=True)
    replacement = store.replace_task_and_rewire(
        original["task_id"],
        "Inspect existing reports before continuing.",
        reuse_team=True,
        rewire_children=False,
        incident_id="endpoint-recovery",
    )["replacement"]
    assert replacement["team"] == original["team"]
    assert replacement["conversation_mode"] == original["conversation_mode"]


def test_independent_explicit_endpoint_is_durable_and_configurable_while_idle(tmp_path):
    store, endpoints = stores(tmp_path)
    pool_default = endpoints.resolve()
    pinned = endpoints.add("9212")
    agent = store.create_independent_agent(
        "Pinned watcher",
        system_prompt="Inspect only.",
        browser_endpoint_id="9212",
    )
    assert agent["browser_endpoint_id"] == pinned["endpoint_id"]
    assert agent["independent"]["browser_endpoint_id"] == pinned["endpoint_id"]
    assert agent["roles"]["AGENT"]["browser_endpoint_id"] == pinned["endpoint_id"]

    other = endpoints.add("9202")
    configured = store.update_independent_agent(
        agent["manifest_path"], browser_endpoint_id="9202"
    )
    assert configured["browser_endpoint_id"] == other["endpoint_id"]
    assert configured["independent"]["browser_endpoint_id"] == other["endpoint_id"]
    assert configured["roles"]["AGENT"]["browser_endpoint_id"] == other["endpoint_id"]

    cleared = store.update_independent_agent(
        agent["manifest_path"], browser_endpoint_id=None
    )
    assert cleared["browser_endpoint_id"] == pool_default["endpoint_id"]
    assert cleared["independent"]["browser_endpoint_id"] is None
    assert cleared["roles"]["AGENT"]["browser_endpoint_id"] == pool_default["endpoint_id"]


def test_independent_manual_run_browser_requirement_is_job_scoped(tmp_path):
    store, endpoints = stores(tmp_path)
    required = endpoints.add("9202")
    agent = store.create_independent_agent(
        "Pool manager",
        system_prompt="Inspect only.",
        trigger_settings={"interval_minutes": 30},
    )

    running = store.run_independent_now(
        agent["manifest_path"],
        instruction="Read Gmail before dispatching.",
        required_browser_endpoint_id="localhost:9202",
    )

    assert running["independent"]["browser_endpoint_id"] is None
    assert (
        running["independent"]["active_event"]["required_browser_endpoint_id"]
        == required["endpoint_id"]
    )
    with pytest.raises(ValueError, match="manual"):
        store.run_independent_now(
            agent["manifest_path"],
            trigger_type="check_all",
            required_browser_endpoint_id=required["endpoint_id"],
        )

    def mark_responded(state):
        hop = next(
            item
            for item in state["hops"]
            if item.get("hop_id") == state.get("active_hop_id")
        )
        hop["state"] = "responded"
        hop["response"] = "Gmail dispatch completed."
        hop["response_sha256"] = "a" * 64
        return state

    store.update(running["manifest_path"], mark_responded)
    released = store.complete_independent_task(
        running["manifest_path"],
        outcome="SUCCESS",
        summary="Gmail dispatch completed.",
    )
    assert released["independent"]["active_event"] is None
    assert released["independent"]["browser_endpoint_id"] is None
    assert released["independent"]["close_tab_when_idle"] is True

    next_run = store.run_independent_now(
        released["manifest_path"],
        instruction="Dispatch a non-Gmail task.",
    )
    assert "required_browser_endpoint_id" not in next_run["independent"]["active_event"]
    assert next_run["independent"]["browser_endpoint_id"] is None


def test_independent_endpoint_clear_refuses_active_job(tmp_path):
    store, endpoints = stores(tmp_path)
    pinned = endpoints.add("9212")
    agent = store.create_independent_agent(
        "Pinned watcher",
        system_prompt="Inspect only.",
        browser_endpoint_id=pinned["endpoint_id"],
    )
    agent = store.run_independent_now(agent["manifest_path"])
    with pytest.raises(ValueError, match="active job"):
        store.update_independent_agent(agent["manifest_path"], browser_endpoint_id=None)


def test_independent_endpoint_config_refuses_to_move_owned_live_tab(tmp_path):
    store, endpoints = stores(tmp_path)
    pinned = endpoints.add("9212")
    agent = store.create_independent_agent(
        "Pinned watcher",
        system_prompt="Inspect only.",
        browser_endpoint_id=pinned["endpoint_id"],
    )
    other = endpoints.add("9202")
    store.update(
        agent["manifest_path"],
        lambda state: state["roles"]["AGENT"].update(
            page_id="owned", page_url="https://chatgpt.com/c/owned", online=True
        ),
    )
    with pytest.raises(ValueError, match="live tab"):
        store.update_independent_agent(
            agent["manifest_path"], browser_endpoint_id=other["endpoint_id"]
        )
    with pytest.raises(ValueError, match="live tab"):
        store.update_independent_agent(agent["manifest_path"], browser_endpoint_id=None)


def test_independent_endpoint_clear_allows_verified_idle_closed_tab_with_saved_identity(tmp_path):
    store, endpoints = stores(tmp_path)
    pinned = endpoints.add("9212")
    agent = store.create_independent_agent(
        "Pinned watcher",
        system_prompt="Inspect only.",
        browser_endpoint_id=pinned["endpoint_id"],
    )
    saved_url = "https://chatgpt.com/c/saved"

    def mark_verified_closed(state):
        state["roles"]["AGENT"].update(
            page_id="saved-page",
            page_url=saved_url,
            online=False,
        )
        state["independent"]["idle_tab_closed_at"] = "2026-09-14T00:00:00+00:00"
        return state

    store.update(agent["manifest_path"], mark_verified_closed)
    cleared = store.update_independent_agent(agent["manifest_path"], browser_endpoint_id=None)

    assert cleared["independent"]["browser_endpoint_id"] is None
    assert cleared["roles"]["AGENT"]["page_id"] == "saved-page"
    assert cleared["roles"]["AGENT"]["page_url"] == saved_url


def test_independent_generation_uses_current_endpoint_capacity_and_keeps_mode_policy(tmp_path):
    store, endpoints = stores(tmp_path)
    first = store.create_independent_agent(
        "Browser watcher", system_prompt="Inspect only.", temporary_chat=True
    )
    store.update(
        first["manifest_path"],
        lambda state: state.update(status="DONE", active_role=None, active_hop_id=None),
    )
    second = endpoints.add("9202", is_default=True)
    later = store.create_independent_agent("Browser watcher", system_prompt="Inspect only.")
    assert later["browser_endpoint_id"] == second["endpoint_id"]
    assert later["conversation_mode"] == "temporary"
    assert later["task_id"] != first["task_id"]


def test_draining_historical_endpoint_does_not_strand_exact_team(tmp_path):
    store, endpoints = stores(tmp_path)
    task = store.create_task("Draining team", requested_team="alpha")
    second = endpoints.add("9202", is_default=True)
    endpoints.remove(task["browser_endpoint_id"], has_nonterminal_tasks=True)
    queued = store.create_task("More work", reuse_team=task["team"])
    assert queued["browser_endpoint_id"] == second["endpoint_id"]
    assert queued["status"] == "WAITING"
