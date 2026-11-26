from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from playwright_auto import cdpa_worker
from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_role_policy import build_role_policy
from playwright_auto.cdpa_route_contract import apply_valid_route_transition, build_route_contract
from playwright_auto.cdpa_routes import RouteContractError, expected_report_relative
from playwright_auto.cdpa_runtime_db import IdempotencyConflict, RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions


def _store(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return TaskStore(config)


def _set_block(store: TaskStore, state: dict, *, code: str) -> dict:
    def mutate(current: dict) -> dict:
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = code
        current["block_retryable"] = False
        current["block_reason"] = code
        current["active_action"] = "blocked"
        return current

    return store.update(state["manifest_path"], mutate)


def test_t011_t014_t028_lazy_slot_projection_and_durable_transition(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create_task(
        "lazy delegation",
        requested_team="lazy-delegation",
        task_id="task-lazy-delegation",
        roles=("REVIEW",),
        route_targets=("DEV",),
        start_role="REVIEW",
        closing_role="REVIEW",
    )

    assert state["submitted_roles"] == ["REVIEW"]
    assert state["initial_roles"] == ["REVIEW"]
    assert state["reachable_roles"] == ["REVIEW", "DEV"]
    assert state["role_slots"]["REVIEW"]["state"] == "materialized"
    assert state["role_slots"]["DEV"]["state"] == "declared"

    before = build_task_projection(state, tasks=[state])
    assert before.summary["submitted_roles"] == ["REVIEW"]
    assert before.summary["reachable_roles"] == ["REVIEW", "DEV"]
    assert before.detail["role_slots"]["DEV"]["state"] == "declared"

    working = copy.deepcopy(state)
    frozen_roles = tuple(working["reachable_roles"])
    apply_valid_route_transition(
        working,
        source_role="REVIEW",
        target_role="DEV",
        materialized_at="2026-10-01T00:00:00+00:00",
    )
    assert working["route_seq"] == 1
    assert working["role_slots"]["DEV"]["state"] == "materialized"
    assert working["roles"]["DEV"]["slot_state"] == "materialized"
    assert tuple(working["reachable_roles"]) == frozen_roles

    persisted = store.update(state["manifest_path"], lambda _current: working)
    restarted = store.load(persisted["manifest_path"])
    assert restarted["route_seq"] == 1
    assert restarted["role_slots"]["DEV"] == persisted["role_slots"]["DEV"]
    assert tuple(restarted["reachable_roles"]) == frozen_roles

    after = build_task_projection(restarted, tasks=[restarted])
    assert after.summary["route_seq"] == 1
    assert after.detail["role_slots"]["DEV"]["state"] == "materialized"


def test_t009_durable_store_accepts_unregistered_custom_roles_with_generic_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create_task(
        "generic custom delegation",
        requested_team="generic-custom",
        task_id="task-generic-custom",
        roles=("CUSTOM_REVIEW",),
        route_targets=("CUSTOM_DEV",),
        closing_role="CUSTOM_REVIEW",
    )

    assert state["initial_roles"] == ["CUSTOM_REVIEW"]
    assert state["reachable_roles"] == ["CUSTOM_REVIEW", "CUSTOM_DEV"]
    assert list(state["workflow_agents"]) == ["CUSTOM_REVIEW", "CUSTOM_DEV"]
    assert state["workflow_agents"]["CUSTOM_REVIEW"]["is_system"] is False
    assert state["workflow_agents"]["CUSTOM_DEV"]["is_system"] is False
    assert state["workflow_agents"]["CUSTOM_REVIEW"]["system_prompt"]
    assert state["semantic_routes"] == {"CUSTOM_REVIEW": {}, "CUSTOM_DEV": {}}

    worker = CDPAWorker(store.config, store=store)
    contract = worker._gate_a_route_contract(state, "CUSTOM_REVIEW")
    assert contract is not None
    assert contract.allowed_routes == ("CUSTOM_DEV", "PAUSE", "DONE")


def test_team_uses_saved_route_contract_after_builtin_prompt_update(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create_task("preserve routes", roles=("PLAN", "DEV"))
    initial = CDPAWorker(store.config, store=store)._gate_a_route_contract(state, "PLAN")

    path = store.config.constructor_paths["PLAN"]
    path.write_text(path.read_text(encoding="utf-8") + "\nRoute to DEV when implementation is needed.\n", encoding="utf-8")

    updated = CDPAWorker(store.config, store=store)._gate_a_route_contract(state, "PLAN")
    assert updated == initial
    next_task = store.create_task("use updated prompt", roles=("PLAN", "DEV"))
    assert "Route to DEV when implementation is needed." in next_task["workflow_agents"]["PLAN"]["system_prompt"]


def test_new_team_keeps_default_route_metadata_and_async_wake_allowance(tmp_path) -> None:
    state = _store(tmp_path).create_task("larger team", roles=("PLAN", "DEV"))
    assert state["max_routes"] == 100
    assert CDPAWorker._execution_budget(state)["limit"] >= 101


def test_t016_route_repair_limit_control_is_atomic_replay_safe_and_resume_separate(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create_task(
        "repair budget",
        requested_team="repair-budget",
        task_id="task-repair-budget",
        roles=("DEV", "REVIEW"),
        closing_role="DEV",
        max_route_repairs=1,
        max_routes=3,
    )

    def exhaust(current: dict) -> dict:
        current["route_repair_count"] = 1
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "ROUTE_VALIDATION_EXHAUSTED"
        current["block_retryable"] = False
        current["block_reason"] = "repair budget exhausted"
        current["active_action"] = "blocked"
        return current

    blocked = store.update(state["manifest_path"], exhaust)
    assert workflow_control_eligibility(blocked, "retry")["eligible"] is False
    assert workflow_control_eligibility(blocked, "resume")["eligible"] is False

    baseline = {
        key: copy.deepcopy(blocked.get(key))
        for key in (
            "max_routes",
            "max_route_repairs",
            "route_seq",
            "self_route_counts",
            "route_repair_count",
        )
    }
    with pytest.raises(ValueError, match="requires max_route_repairs"):
        store.request_control(blocked["manifest_path"], "increase_route_limits")
    with pytest.raises(ValueError, match="strictly above"):
        store.request_control(
            blocked["manifest_path"],
            "increase_route_limits",
            max_route_repairs=blocked["max_route_repairs"],
        )
    with pytest.raises(ValueError, match="historical metadata"):
        store.request_control(
            blocked["manifest_path"],
            "increase_route_limits",
            max_routes=4,
            max_route_repairs=2,
        )
    unchanged = store.load(blocked["manifest_path"])
    assert {key: unchanged.get(key) for key in baseline} == baseline

    raised = store.request_control(
        blocked["manifest_path"],
        "increase_route_limits",
        max_route_repairs=2,
        external_command_id="cmd-raise-repair-limit",
    )
    assert raised["max_route_repairs"] == 2
    assert raised["route_repair_count"] == 1
    assert raised["block_code"] == "ROUTE_VALIDATION_EXHAUSTED"
    assert workflow_control_eligibility(raised, "resume")["eligible"] is True

    replay = store.request_control(
        blocked["manifest_path"],
        "increase_route_limits",
        max_route_repairs=2,
        external_command_id="cmd-raise-repair-limit",
    )
    assert replay["max_route_repairs"] == 2
    assert sum(
        1 for item in replay["controls"] if item.get("action") == "increase_route_limits"
    ) == 1

    resumed = store.request_control(
        blocked["manifest_path"],
        "resume",
        reason="continue repair",
        external_command_id="cmd-resume-repair",
    )
    assert resumed["route_repair_count"] == 1
    assert resumed["controls"][-1]["action"] == "resume"


def test_t018_self_route_counters_are_task_lifetime_and_budgeted() -> None:
    policy = build_role_policy(
        ["A"],
        semantic_routes={"A": {"retry": "A"}},
        self_route_policy={"mode": "bounded", "max_self_routes": 2},
        max_routes=3,
    )
    state = {
        "reachable_roles": ["A"],
        "route_seq": 0,
        "route_repair_count": 0,
        "self_route_counts": {"A": 0},
        "role_slots": policy.initial_role_slots(),
        "roles": {"A": {"slot_state": "materialized", "materialized_at": None}},
    }
    apply_valid_route_transition(state, source_role="A", target_role="A", materialized_at="t1")
    apply_valid_route_transition(state, source_role="A", target_role="A", materialized_at="t2")
    assert state["route_seq"] == 2
    assert state["self_route_counts"] == {"A": 2}

    contract = build_route_contract(
        current_role="A",
        reachable_roles=policy.reachable_roles,
        closing_role=policy.closing_role,
        semantic_routes=policy.semantic_routes,
        route_contract_id=policy.route_contract_id,
        route_seq=state["route_seq"],
        max_route_repairs=policy.max_route_repairs,
        self_route_policy=policy.self_route_policy,
        self_route_counts=state["self_route_counts"],
    )
    assert "A" not in contract.allowed_routes
    assert contract.allowed_routes == ("PAUSE", "DONE")


@pytest.mark.parametrize("status", ["BLOCKED", "PAUSED"])
def test_t019_historical_route_budget_block_resumes_without_changing_limits(tmp_path, status) -> None:
    store = _store(tmp_path)
    state = store.create_task(
        "route budget",
        requested_team="route-budget",
        task_id="task-route-budget",
        roles=("DEV", "REVIEW"),
        closing_role="DEV",
        max_routes=1,
        max_route_repairs=2,
    )

    def exhaust(current: dict) -> dict:
        current["route_seq"] = 1
        current["self_route_counts"]["DEV"] = 1
        current["route_repair_count"] = 1
        current["status"] = status
        current["kanban_column"] = status
        current["block_code"] = "ROUTE_BUDGET_EXHAUSTED"
        current["block_retryable"] = False
        current["block_reason"] = "route budget exhausted"
        current["active_action"] = status.lower()
        return current

    blocked = store.update(state["manifest_path"], exhaust)
    assert workflow_control_eligibility(blocked, "retry")["eligible"] is False
    assert workflow_control_eligibility(blocked, "resume")["eligible"] is True
    assert workflow_control_eligibility(blocked, "increase_route_limits")["eligible"] is False

    snapshot = {
        key: copy.deepcopy(blocked.get(key))
        for key in (
            "max_routes",
            "max_route_repairs",
            "route_seq",
            "self_route_counts",
            "route_repair_count",
        )
    }
    with pytest.raises(ValueError, match="historical metadata"):
        store.request_control(
            blocked["manifest_path"],
            "increase_route_limits",
            max_routes=2,
            max_route_repairs=3,
        )
    unchanged = store.load(blocked["manifest_path"])
    assert {key: unchanged.get(key) for key in snapshot} == snapshot

    resumed = store.request_control(
        blocked["manifest_path"],
        "resume",
        reason="continue past historical route cap",
        external_command_id="cmd-resume-route",
    )
    assert {key: resumed.get(key) for key in snapshot} == snapshot
    assert resumed["route_repair_count"] == 1


@pytest.mark.parametrize(
    ("origin", "verified", "manually_paused"),
    [
        ("operator", True, False),
        ("scheduler", True, False),
        ("operator", False, False),
        ("operator", True, True),
    ],
)
def test_resume_past_historical_cap_verifies_progress_and_preserves_route_metadata(
    tmp_path, origin, verified, manually_paused, monkeypatch,
) -> None:
    store = _store(tmp_path)
    worker = CDPAWorker(store.config, store=store)
    state = store.create_task(
        "resume a long team", roles=("PLAN", "DEV"), start_role="DEV", max_routes=8,
    )
    assert worker._prepare_execution_endpoint(state, str(state["browser_endpoint_id"]))
    state["route_seq"] = 8
    state["self_route_counts"]["DEV"] = 1
    budget = worker._execution_budget(state)
    budget.update(limit=12, charged_hop_ids=list(range(1, 13)))
    state = store.save(state["manifest_path"], state)
    blocked = _set_block(store, state, code="ROUTE_BUDGET_EXHAUSTED")
    if manually_paused:
        worker._apply_pause_transition(blocked, {"reason": "Operator pause"})
        blocked = store.save(blocked["manifest_path"], blocked)
    queued = store.request_resume(blocked["manifest_path"])
    queued["controls"][-1]["origin"] = origin
    queued = store.save(queued["manifest_path"], queued)
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(queued, actions, queued["manifest_path"]))
    control = queued["controls"][-1]
    assert control["status"] == "recovering"
    assert queued["kanban_column"] == "WORKING"
    assert queued["max_routes"] == 8
    assert "route_budget_start_seq" not in queued

    path = queued["manifest_path"]
    fresh_store = TaskStore(store.config)
    fresh_worker = CDPAWorker(store.config, store=fresh_store)
    monkeypatch.setattr(cdpa_worker, "CDPATabActions", lambda *_args, **_kwargs: actions)
    if not verified:
        async def unavailable(*_args, **_kwargs):
            raise RuntimeError("Owned tab could not be attached.")

        monkeypatch.setattr(actions, "acquire", unavailable)
    asyncio.run(fresh_worker.advance(
        path, SimpleNamespace(pages=[]), endpoint_id=queued["browser_endpoint_id"],
    ))

    queued = fresh_store.load(path)
    control = queued["controls"][-1]
    assert _active_hop(queued)["request_id"] == _active_hop(blocked)["request_id"]
    assert queued["route_seq"] == 8
    assert queued["self_route_counts"]["DEV"] == 1
    if not verified:
        assert control["status"] == "failed", control["result"]
        assert queued["max_routes"] == 8
        assert "route_budget_start_seq" not in queued
        assert queued["execution_budget"] == budget
        return

    assert control["status"] == "applied", control["result"]["reason"]
    assert queued["max_routes"] == 8
    assert "route_budget_start_seq" not in queued
    budget = queued["execution_budget"]
    assert len(budget["charged_hop_ids"]) == 12
    assert budget["limit"] - len(budget["charged_hop_ids"]) >= 100


def test_resume_preserves_presend_block_reason_without_unrelated_recovery_advice(
    tmp_path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    worker = CDPAWorker(store.config, store=store)
    state = store.create_task("verify pre-send Resume failure", roles=("PLAN", "DEV"))
    state = _set_block(store, state, code="ROUTE_CONTRACT_INVALID")
    state = store.request_resume(state["manifest_path"])
    actions = FakeActions()

    async def invalid_route(current, hop, actions):
        worker._block(
            current, "Semantic route destination is unavailable",
            code="ROUTE_CONTRACT_INVALID", retryable=False,
        )

    monkeypatch.setattr(worker, "_pre_send", invalid_route)
    assert asyncio.run(worker._apply_control(state, actions))
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "ROUTE_CONTRACT_INVALID"
    assert control["result"]["reason"] == "Semantic route destination is unavailable"
    assert control["result"]["next_safe_action"] is None


def test_t016_worker_route_repair_exhausts_per_turn_without_retryable_block(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create_task(
        "malformed route repair",
        requested_team="repair-worker",
        task_id="task-repair-worker",
        roles=("DEV", "REVIEW"),
        closing_role="DEV",
        max_route_repairs=1,
    )
    worker = CDPAWorker(store.config, store=store)
    hop = next(item for item in state["hops"] if item["hop_id"] == state["active_hop_id"])
    hop["response"] = '{"route":"NOT_REACHABLE","handoff":"x"}'

    worker._repair_route(state, hop, RouteContractError("route outside contract"))
    assert state["route_repair_count"] == 1
    assert state["status"] != "BLOCKED"
    repair = next(item for item in state["hops"] if item["hop_id"] == state["active_hop_id"])
    assert repair["kind"] == "route_repair"

    repair["response"] = '{"route":"STILL_BAD","handoff":"x"}'
    worker._repair_route(state, repair, RouteContractError("still outside contract"))
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "ROUTE_VALIDATION_EXHAUSTED"
    assert state["block_retryable"] is False
    assert state["route_repair_count"] == 1


def test_t016_t019_idempotency_key_replay_returns_original_limit_command_result(tmp_path) -> None:
    store = _store(tmp_path)
    db = RuntimeDB(store.config.runtime_database)
    db.ensure_schema()
    payload = {"action": "increase_route_limits", "max_routes": 9, "max_route_repairs": 4}
    command = db.enqueue_command(
        command_id="cmd-limit-replay",
        idempotency_key="limit-replay-key",
        kind="task_control",
        task_id="task-limit-replay",
        expected_task_version=7,
        payload=payload,
    )
    db.finish_command(command["command_id"], result={"max_routes": 9, "max_route_repairs": 4})

    replay = db.enqueue_command(
        command_id="cmd-limit-replay-new-id-is-ignored",
        idempotency_key="limit-replay-key",
        kind="task_control",
        task_id="task-limit-replay",
        expected_task_version=7,
        payload=payload,
    )
    assert replay["command_id"] == "cmd-limit-replay"
    assert replay["status"] == "applied"
    assert replay["result"] == {"max_routes": 9, "max_route_repairs": 4}

    with pytest.raises(IdempotencyConflict):
        db.enqueue_command(
            command_id="cmd-limit-conflict",
            idempotency_key="limit-replay-key",
            kind="task_control",
            task_id="task-limit-replay",
            expected_task_version=7,
            payload={"action": "increase_route_limits", "max_routes": 10},
        )


def test_t016_t019_stale_expected_task_version_rejects_before_limit_mutation(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create_task(
        "stale route limit control",
        requested_team="stale-limit",
        task_id="task-stale-limit",
        roles=("DEV", "REVIEW"),
        closing_role="DEV",
        max_routes=1,
    )

    def exhaust(current: dict) -> dict:
        current["route_seq"] = 1
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "ROUTE_BUDGET_EXHAUSTED"
        current["block_retryable"] = False
        current["block_reason"] = "route budget exhausted"
        current["active_action"] = "blocked"
        return current

    blocked = store.update(state["manifest_path"], exhaust)
    worker = CDPAWorker(store.config, store=store)
    worker.runtime_db.ensure_schema()
    worker._publish_command_state(blocked)
    current_version = worker.runtime_db.get_task_version(blocked["task_id"])
    assert current_version is not None
    worker.runtime_db.enqueue_command(
        command_id="cmd-stale-route-limit",
        idempotency_key="stale-route-limit",
        kind="task_control",
        task_id=blocked["task_id"],
        expected_task_version=current_version + 1,
        payload={"action": "increase_route_limits", "max_routes": 2},
    )

    result = worker._apply_next_command(allowed_kinds=("task_control",))
    assert result is not None
    assert result["status"] == "failed"
    assert "stale task version" in str(result["error"])
    unchanged = store.load(blocked["manifest_path"])
    assert unchanged["max_routes"] == 1
    assert unchanged["route_seq"] == 1
    assert unchanged["block_code"] == "ROUTE_BUDGET_EXHAUSTED"
    assert not any(item.get("action") == "increase_route_limits" for item in unchanged["controls"])


def test_t016_done_closure_resets_current_turn_repair_count(tmp_path) -> None:
    store = _store(tmp_path)
    state = store.create_task(
        "closing repair reset",
        requested_team="closing-repair-reset",
        task_id="task-closing-repair-reset",
        roles=("DEV",),
        closing_role="DEV",
    )
    worker = CDPAWorker(store.config, store=store)
    hop = next(item for item in state["hops"] if item["hop_id"] == state["active_hop_id"])
    handoff = expected_report_relative(
        plans_root=store.config.plans_root,
        repository_root=state["repository"],
        team=state["team"],
        physical_role=hop["physical_role"],
        turn=hop["turn"],
        task_id=state["task_id"],
    )
    report = store.config.repository_root / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# closing report\n", encoding="utf-8")
    state["route_repair_count"] = 1
    hop["expected_report_path"] = handoff
    hop["ledger_path"] = None
    hop["request_id"] = None
    hop["response"] = f'{{"route":"DONE","handoff":"{handoff.replace(chr(92), chr(47))}"}}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["status"] == "DONE"
    assert state["route_repair_count"] == 0
