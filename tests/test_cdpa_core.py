from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import playwright_auto.cdpa_store as store_module
from playwright_auto.cdpa_config import CDPAConfigError, load_cdpa_config
from playwright_auto.cdpa_projection import build_dashboard_actions
from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto.cdpa_routes import RouteContractError, parse_route_response, validate_report
from playwright_auto.cdpa_store import TaskStore, effective_task_goal, task_goal_for_hop, utc_now
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.cdpa_team import (
    cleanup_eligible,
    is_active_team_owner,
    physical_role,
    queued_team_tasks,
)


def write_config(root: Path) -> Path:
    path = root / "cdpa.yaml"
    path.write_text(
        json.dumps(
            {
                "paths": {
                    "plans_root": ".plan",
                    "constructors": {
                        role: f"prompts/cdpa/{role}.md"
                        for role in ("PLAN", "DEV", "REVIEW", "TEST", "AUDIT")
                    },
                    "independent_rule": "prompts/cdpa/INDEPENDENT_RULE.md",
                    "response_guide": "prompts/cdpa/RESPONSE_GUIDE.md",
                },
                "roles": ["PLAN", "DEV", "REVIEW", "TEST", "AUDIT"],
                "dashboard": {"url": "http://127.0.0.1:9224", "port": 9224, "poll_seconds": 1},
                "browser": {"cdp_url": "http://127.0.0.1:9222", "workspace_timeout_seconds": 15},
                "route_repair": {"max_attempts": 3},
                "response": {"timeout_seconds": 7200, "refresh_after_seconds": 1200, "stable_ms": 1000, "poll_ms": 100},
                "independent_agents": {
                    "seed_builtins": False,
                    "idle_close_seconds": 60,
                },
                "cleanup": {"terminal_idle_seconds": 3600},
                "worker": {"poll_seconds": 1},
                "delays": {"minimum_seconds": 1.0, "maximum_seconds": 1.5, "multipliers": {"send": 3}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    for role in ("PLAN", "DEV", "REVIEW", "TEST", "AUDIT"):
        target = root / "prompts" / "cdpa" / f"{role}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {role}\nConstructor for {role}.\n", encoding="utf-8")
    (root / "prompts" / "cdpa" / "INDEPENDENT_RULE.md").write_text(
        "INDEPENDENT_AGENT_OPERATING_RULE\nAct directly and use explicit completion controls.\n",
        encoding="utf-8",
    )
    (root / "prompts" / "cdpa" / "RESPONSE_GUIDE.md").write_text(
        "Return only the strict route JSON.\nReport: .plan/<team>/<physical-role>_turn<N>_<task-id>.md", encoding="utf-8"
    )
    return path


def install_cycle_isolation_graph(store: TaskStore) -> dict[str, dict[str, object]]:
    missing_parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    missing_only = store.create_task(
        "Missing only",
        requested_team="missing-only",
        task_id="missing-only",
        depends_on_task_ids=("missing-parent",),
    )
    a = store.create_task("A", requested_team="a", task_id="task-a")
    b = store.create_task(
        "B",
        requested_team="b",
        task_id="task-b",
        depends_on_task_ids=("task-a",),
    )
    unrelated = store.create_task(
        "Unrelated", requested_team="unrelated", task_id="unrelated-task"
    )
    Path(missing_parent["manifest_path"]).unlink()
    a_path = Path(a["manifest_path"])
    raw_a = json.loads(a_path.read_text(encoding="utf-8"))
    raw_a["depends_on_task_ids"] = ["task-b"]
    a_path.write_text(json.dumps(raw_a), encoding="utf-8")
    return {
        "missing_parent": missing_parent,
        "missing_only": missing_only,
        "a": a,
        "b": b,
        "unrelated": unrelated,
    }


def poison_catalog_identity_entry(
    store: TaskStore,
    state: dict[str, object],
    *,
    mapping_path: str | Path | None = None,
    key: str = "bad",
) -> dict[str, object]:
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    canonical_key = store._catalog_key(str(state["manifest_path"]))
    entry = catalog["entries"].pop(canonical_key)
    if mapping_path is None:
        entry.pop("manifest_path", None)
    else:
        entry["manifest_path"] = str(Path(mapping_path).expanduser().resolve())
    catalog["entries"][key] = entry
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    return json.loads(json.dumps(entry))


def install_duplicate_task_graph(store: TaskStore) -> dict[str, dict[str, object]]:
    alpha = store.create_task("Alpha", requested_team="alpha", task_id="dup-task")
    alpha_path = Path(alpha["manifest_path"])
    alpha_bytes = alpha_path.read_bytes()
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    alpha_key, alpha_entry = next(iter(catalog["entries"].items()))
    alpha_path.unlink()
    del catalog["entries"][alpha_key]
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    beta = store.create_task("Beta", requested_team="beta", task_id="dup-task")
    unique = store.create_task("Unique", requested_team="unique", task_id="unique-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=("dup-task",),
    )

    alpha_path.write_bytes(alpha_bytes)
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    catalog["entries"][alpha_key] = alpha_entry
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    return {"alpha": alpha, "beta": beta, "unique": unique, "child": child}




def test_config_loads_root_json_compatible_yaml_and_validates_defaults(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    assert config.plans_root == (tmp_path / ".plan").resolve()
    assert config.route_repair_attempts == 3
    assert config.response_timeout_seconds == 7200
    assert config.response_refresh_after_seconds == 1200
    assert config.response_stream_status_poll_seconds == 3.0
    assert config.dashboard_url == "http://127.0.0.1:9224"


def test_stream_status_poll_interval_is_configurable_and_bounded(tmp_path: Path):
    path = write_config(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["response"]["stream_status_poll_seconds"] = 0.5
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_cdpa_config(path, repository_root=tmp_path).response_stream_status_poll_seconds == 0.5

    for invalid in (0, -1, 0.19, 60.01, float("nan"), float("inf"), "nope"):
        raw["response"]["stream_status_poll_seconds"] = invalid
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CDPAConfigError, match="stream_status_poll_seconds"):
            load_cdpa_config(path, repository_root=tmp_path)




def test_task_creation_allocates_same_base_concurrently_and_reuses_terminal(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("first task", requested_team="alpha", task_id="task-a")
    second = store.create_task("second task", requested_team="alpha", task_id="task-b")
    third = store.create_task("third task", requested_team="alpha", task_id="task-c")
    assert [first["team"], second["team"], third["team"]] == ["alpha", "alpha2", "alpha3"]
    assert physical_role("DEV", "alpha", second["team_suffix"]) == "alpha-dev2"
    assert first["roles"]["PLAN"]["physical_role"] == "alpha-plan"
    assert second["roles"]["DEV"]["physical_role"] == "alpha-dev2"

    different_base = store.create_task("different base", requested_team="beta", task_id="task-beta")
    assert different_base["team"] == "beta"
    assert different_base["roles"]["PLAN"]["physical_role"] == "beta-plan"

    store.update(
        first["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
        },
    )
    reused = store.create_task("fourth task", requested_team="alpha", task_id="task-d")
    assert reused["team"] == "alpha"
    assert reused["roles"]["PLAN"]["physical_role"] == "alpha-plan"
    assert reused["reusable_teams"] == ["alpha"]


def test_manifest_path_and_initial_plan_are_lazy(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    state = TaskStore(config).create_task(
        "Build production CDPA", requested_team="prod", task_id="task-prod", new_roles=("DEV",), new_all=False
    )
    expected = tmp_path / ".plan" / "prod" / "task-prod" / "build-production-cdpa.json"
    assert Path(state["manifest_path"]) == expected.resolve()
    assert state["active_role"] == "PLAN"
    assert state["roles"]["PLAN"]["status"] == "pending"
    assert state["roles"]["DEV"]["status"] == "unallocated"
    assert state["roles"]["DEV"]["reset_requested"] is True
    assert state["hops"][0]["state"] == "pre_send"


def test_workflow_role_subset_is_canonical_and_exact_team_reuse_inherits(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)

    first = store.create_task(
        "small workflow",
        requested_team="alpha",
        task_id="task-subset",
        roles=("REVIEW", "PLAN"),
    )
    assert list(first["roles"]) == ["PLAN", "REVIEW"]
    assert list(store.load(first["manifest_path"])["roles"]) == ["PLAN", "REVIEW"]

    inherited = store.create_task(
        "reuse subset",
        reuse_team="alpha",
        task_id="task-subset-reuse",
    )
    assert list(inherited["roles"]) == ["PLAN", "REVIEW"]

    matched = store.create_task(
        "reuse matching subset",
        reuse_team="alpha",
        task_id="task-subset-match",
        roles=("PLAN", "REVIEW"),
    )
    assert list(matched["roles"]) == ["PLAN", "REVIEW"]

    with pytest.raises(ValueError, match="role composition"):
        store.create_task(
            "reuse mismatched subset",
            reuse_team="alpha",
            task_id="task-subset-mismatch",
            roles=("PLAN", "DEV"),
        )
    with pytest.raises(ValueError, match="must include PLAN"):
        store.create_task(
            "missing plan",
            requested_team="missing-plan",
            task_id="task-missing-plan",
            roles=("DEV",),
        )
    with pytest.raises(ValueError, match="must not contain duplicates"):
        store.create_task(
            "duplicate plan",
            requested_team="duplicate-plan",
            task_id="task-duplicate-plan",
            roles=("PLAN", "PLAN"),
        )

    legacy = store.create_task(
        "legacy full workflow",
        requested_team="legacy",
        task_id="task-legacy-full",
    )
    assert set(store.load(legacy["manifest_path"])["roles"]) == set(config.roles)


def _prompt_kwargs(config, tmp_path: Path, **overrides):
    value = {
        "task_title": "full task",
        "task_id": "task-a",
        "team": "alpha",
        "logical_role": "PLAN",
        "physical_role": "alpha-plan",
        "turn": 1,
        "allowed_routes": ("PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"),
        "workspace": str(tmp_path),
        "source_physical_role": None,
        "handoff": "full task",
        "goal": "full task",
        "constructor_sent_generation": None,
        "conversation_generation": 0,
    }
    value.update(overrides)
    return value


def test_constructor_is_once_per_generation_and_prompt_is_compact_allowlist(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    builder = PromptBuilder(config)
    first = builder.build(**_prompt_kwargs(config, tmp_path))
    later = builder.build(
        **_prompt_kwargs(
            config,
            tmp_path,
            logical_role="DEV",
            physical_role="alpha-dev",
            source_physical_role="alpha-plan",
            handoff=".plan/alpha/alpha-plan_turn1_task-a.md",
            constructor_sent_generation=0,
        )
    )
    restarted = builder.build(
        **_prompt_kwargs(
            config,
            tmp_path,
            handoff="handoff",
            constructor_sent_generation=0,
            conversation_generation=1,
        )
    )
    repair = builder.repair(
        task_id="task-a",
        team="alpha",
        physical_role="alpha-plan",
        turn=1,
        validation_error="bad keys at .plan/alpha/alpha-plan_turn1_task-a.md",
    )

    assert first.text.startswith("alpha · role: plan\n{")
    envelope_text = first.text.split("\n\n# PLAN", 1)[0].removeprefix(
        "alpha · role: plan\n"
    )
    envelope = json.loads(envelope_text)
    assert envelope == {
        "title": "full task",
        "task-id": "task-a",
        "team": "alpha",
        "role": "alpha-plan",
        "source-role": None,
        "turn": 1,
        "workspace": str(tmp_path),
        "allowed-routes": ["PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"],
        "goal": "full task",
        "handoff": "full task",
    }
    assert "Constructor for PLAN" in first.text
    assert "Constructor for DEV" not in later.text
    assert "Constructor for PLAN" in restarted.text
    assert '"source-role": "alpha-plan"' in later.text
    assert '"handoff": ".plan/alpha/alpha-plan_turn1_task-a.md"' in later.text
    assert '"goal": "full task"' in later.text
    assert ".plan/alpha/alpha-plan_turn1_task-a.md" not in first.text
    for forbidden in (
        "logical_role", "physical_role", "source_physical_role", "allowed_roles",
        "team_members", "repository", "report_naming_rule", "controller_id",
        "run_id", "request_id", "page_id", "prompt_sha256", "ledger_path",
        "created_at", "updated_at", "ROLE_REQUEST_ID",
    ):
        assert forbidden not in first.text

    assert "full task" not in repair
    assert "alpha-dev_turn1_task-a.md" not in repair
    assert "Constructor for PLAN" not in repair
    assert ".plan/alpha/alpha-plan_turn1_task-a.md" not in repair
    assert "bad keys" in repair
    assert '"task-id": "task-a"' in repair
    assert '"role": "alpha-plan"' in repair
    assert ".plan/<team>/<physical-role>_turn<N>_<task-id>.md" in repair


def test_exact_team_resume_preserves_identity_and_creation_still_allocates(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    created = store.create_task("first task", requested_team="alpha", task_id="task-a")
    manifest = Path(created["manifest_path"])
    manifest_count = len(store.discover_paths())
    catalog_count = len(json.loads(store.catalog_path.read_text(encoding="utf-8"))["entries"])
    created = store.update(
        manifest,
        lambda state: {
            **state,
            "reports": [
                {
                    "report_id": 1,
                    "physical_role": "alpha-plan",
                    "turn": 1,
                    "path": str(tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"),
                    "sha256": "a" * 64,
                    "size": 12,
                }
            ],
        },
    )
    hop = created["hops"][0]
    identity_before = {
        "task_id": created["task_id"],
        "manifest_path": created["manifest_path"],
        "active_hop_id": created["active_hop_id"],
        "request_id": hop["request_id"],
        "hop_turn": hop["turn"],
        "role_turns": {role: record["turn"] for role, record in created["roles"].items()},
        "reports": json.loads(json.dumps(created["reports"])),
    }

    resumed = store.resume_team("alpha")
    resumed_again = store.resume_team("alpha")
    resumed_via_generic_control = store.request_control(
        manifest, "resume", role="DEV", reason="resume requested"
    )

    assert resumed["task_id"] == created["task_id"]
    assert resumed["manifest_path"] == created["manifest_path"]
    assert resumed["team"] == "alpha"
    assert resumed["team_suffix"] == 1
    assert resumed["active_hop_id"] == created["active_hop_id"]
    assert resumed["hops"][0]["request_id"] == hop["request_id"]
    assert {
        "task_id": resumed["task_id"],
        "manifest_path": resumed["manifest_path"],
        "active_hop_id": resumed["active_hop_id"],
        "request_id": resumed["hops"][0]["request_id"],
        "hop_turn": resumed["hops"][0]["turn"],
        "role_turns": {role: record["turn"] for role, record in resumed["roles"].items()},
        "reports": resumed["reports"],
    } == identity_before
    assert len(store.discover_paths()) == manifest_count
    assert len(json.loads(store.catalog_path.read_text(encoding="utf-8"))["entries"]) == catalog_count
    pending = [
        item for item in resumed_via_generic_control["controls"]
        if item["action"] == "resume" and item["status"] == "requested"
    ]
    assert len(pending) == 1
    assert pending[0]["role"] == "PLAN"
    assert manifest.exists()

    second = store.create_task("second task", requested_team="alpha", task_id="task-b")
    assert second["team"] == "alpha2"
    assert second["task_id"] == "task-b"






def test_route_contract_and_report_validation(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    state = TaskStore(config).create_task("Task", requested_team="alpha", task_id="task-1")
    report = tmp_path / ".plan" / "alpha" / "PLAN_turn1_task-1.md"
    report.write_text("final report", encoding="utf-8")
    decision = parse_route_response('{"route":"DEV","handoff":".plan/alpha/PLAN_turn1_task-1.md"}')
    evidence = validate_report(
        decision.handoff,
        repository_root=tmp_path,
        plans_root=config.plans_root,
        team="alpha",
        physical_role="PLAN",
        turn=1,
        task_id="task-1",
    )
    assert evidence.path == str(report.resolve())
    assert evidence.size == len("final report")
    assert len(evidence.sha256) == 64

    real_report = report.with_name("real-report.md")
    real_report.write_text("symlink target", encoding="utf-8")
    report.unlink()
    report.symlink_to(real_report)
    with pytest.raises(RouteContractError, match="symlink"):
        validate_report(
            ".plan/alpha/PLAN_turn1_task-1.md",
            repository_root=tmp_path,
            plans_root=config.plans_root,
            team="alpha",
            physical_role="PLAN",
            turn=1,
            task_id="task-1",
        )

    for invalid in (
        '{"route":"DEV","handoff":"x","extra":true}',
        '{"route":"DEV","route":"PLAN","handoff":"x"}',
        'prose {"route":"DEV","handoff":"x"}',
    ):
        with pytest.raises(RouteContractError):
            parse_route_response(invalid)

    with pytest.raises(RouteContractError, match="PLAN"):
        parse_route_response('{"route":"DONE","handoff":"x"}', source_role="DEV")














































def test_cleanup_only_terminal_and_idle():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    assert cleanup_eligible({"status": "DONE", "last_role_activity_at": old}, now=now, idle_seconds=3600)
    assert cleanup_eligible({"status": "STOPPED", "last_role_activity_at": old}, now=now, idle_seconds=3600)
    assert not cleanup_eligible({"status": "BLOCKED", "last_role_activity_at": old}, now=now, idle_seconds=3600)
    assert not cleanup_eligible({"status": "DONE", "active_role": "PLAN", "last_role_activity_at": old}, now=now, idle_seconds=3600)
    assert not cleanup_eligible({"status": "DONE", "last_role_activity_at": now.isoformat()}, now=now, idle_seconds=3600)
    assert not cleanup_eligible(
        {"status": "DONE", "last_role_activity_at": old},
        now=now,
        idle_seconds=3600,
        queued_team_work=True,
    )




def test_reuse_team_creates_waiting_task_without_allocating_suffix(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("one", requested_team="alpha", task_id="task-one")

    assert build_dashboard_actions([first])["reuse_teams"] == [
        {
            "team": "alpha",
            "status": "available",
            "roles": list(config.roles),
        }
    ]

    second = store.create_task("two", reuse_team="alpha", task_id="task-two")

    assert second["team"] == "alpha"
    assert second["reusable_teams"] == ["alpha"]
    assert second["team_suffix"] == first["team_suffix"]
    assert second["roles"]["PLAN"]["physical_role"] == first["roles"]["PLAN"]["physical_role"]
    assert second["status"] == "WAITING"
    assert second["waiting"]["reason"] == "team_busy"
    assert second["waiting"]["blocked_by_task_id"] == "task-one"
    assert second["queue"] == {
        "reuse_team": True,
        "blocked_by_task_id": "task-one",
        "enqueued_at": second["created_at"],
        "released_at": None,
    }
    assert second["queue_events"][0]["status"] == "WAITING"












































def test_independent_agent_config_is_shared_and_not_a_normal_route(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)

    assert config.independent_rule_path.name == "INDEPENDENT_RULE.md"
    assert config.independent_rule_path.is_file()
    assert config.independent_idle_close_seconds == 60
    assert "MAINTAINERS" not in config.roles
    assert "MONITOR" not in config.roles








def test_new_workflow_tasks_are_file_report_only_across_repositories(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    execution_repository = tmp_path.parent / f"{tmp_path.name}-execution"
    execution_repository.mkdir()

    same_root_default = store.create_task(
        "File report", requested_team="alpha", task_id="task-file"
    )
    same_root_explicit = store.create_task(
        "Explicit file report",
        requested_team="beta",
        task_id="task-explicit-file",
        report_mode="file",
    )
    cross_root_default = store.create_task(
        "Cross-root default report",
        requested_team="delta",
        task_id="task-cross-default",
        repository=execution_repository,
    )
    cross_root_explicit = store.create_task(
        "Cross-root explicit file report",
        requested_team="epsilon",
        task_id="task-cross-explicit",
        repository=execution_repository,
        report_mode="file",
    )

    assert same_root_default["options"]["report_mode"] == "file"
    assert same_root_explicit["options"]["report_mode"] == "file"
    assert cross_root_default["options"]["report_mode"] == "file"
    assert cross_root_explicit["options"]["report_mode"] == "file"
    assert store.load(cross_root_explicit["manifest_path"])["options"]["report_mode"] == "file"
    for mode in ("inline", "remote"):
        with pytest.raises(ValueError, match="report_mode"):
            store.create_task(
                f"Bad report mode {mode}",
                requested_team=f"bad-{mode}",
                task_id=f"task-bad-report-mode-{mode}",
                report_mode=mode,
            )











def test_workflow_prompt_builder_rejects_inline_report_mode():
    repository = Path(__file__).resolve().parents[1]
    config = load_cdpa_config(repository / "cdpa.yaml", repository_root=repository)
    builder = PromptBuilder(config)

    with pytest.raises(ValueError, match="inline"):
        builder.build(
            task_title="Removed inline contract",
            task_id="task-inline-contract",
            team="alpha",
            logical_role="PLAN",
            physical_role="alpha-plan",
            turn=1,
            allowed_routes=("PLAN", "DEV", "REVIEW", "DONE"),
            workspace=str(repository),
            source_physical_role=None,
            handoff="legacy inline must not be selectable",
            goal="file-only contract",
            constructor_sent_generation=None,
            conversation_generation=0,
            report_mode="inline",
        )










def test_dependency_creation_waits_and_legacy_manifests_remain_compatible(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )

    assert child["depends_on_task_ids"] == ["task-parent"]
    assert child["status"] == "WAITING"
    assert child["kanban_column"] == "WAITING"
    assert child["active_action"] == "waiting_dependency"
    assert child["active_hop_id"] == 1
    assert child["hops"][0]["state"] == "pre_send"
    assert child["waiting"]["waiting_on"] == ["task-parent"]
    assert child["waiting"]["stopped"] == []
    assert child["waiting"]["missing"] == []
    assert child["waiting"]["since"]
    assert len(child["dependency_events"]) == 1
    assert "child_task_ids" not in child

    raw = json.loads(Path(parent["manifest_path"]).read_text(encoding="utf-8"))
    for key in ("depends_on_task_ids", "replaces_task_id", "replacement_incident_id", "dependency_events", "waiting"):
        raw.pop(key, None)
    Path(parent["manifest_path"]).write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load(parent["manifest_path"])
    assert "depends_on_task_ids" not in loaded


def test_dependency_creation_ready_when_all_parents_done(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": state["updated_at"],
        },
    )

    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    assert child["status"] == "INBOX"
    assert child["waiting"]["reason"] is None
    assert child["dependency_events"] == []


def test_dependency_creation_rejects_missing_self_duplicate_and_cycle_before_write(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    a = store.create_task("A", requested_team="a", task_id="task-a")
    b = store.create_task(
        "B", requested_team="b", task_id="task-b", depends_on_task_ids=("task-a",)
    )
    before = sorted(str(path) for path in store.discover_paths())

    for task_id, parents, match in (
        ("task-self", ("task-self",), "itself"),
        ("task-dup", ("task-a", "task-a"), "duplicate"),
        ("task-missing", ("absent",), "missing"),
    ):
        with pytest.raises(ValueError, match=match):
            store.create_task(
                task_id,
                requested_team=task_id,
                task_id=task_id,
                depends_on_task_ids=parents,
            )
    assert sorted(str(path) for path in store.discover_paths()) == before

    # Ordinary single-manifest loads remain local; one-time runtime hydration owns
    # repository-wide dependency validation.
    raw_a = json.loads(Path(a["manifest_path"]).read_text(encoding="utf-8"))
    raw_a["depends_on_task_ids"] = ["task-b"]
    Path(a["manifest_path"]).write_text(json.dumps(raw_a), encoding="utf-8")
    assert store.load(a["manifest_path"])["depends_on_task_ids"] == ["task-b"]
    worker = CDPAWorker(config, store=store)
    catalog = worker.hydrate_runtime()
    assert catalog["complete"] is False
    assert any("cycle" in item["error"] for item in catalog["errors"])
    assert worker.runtime_degraded is True






















def _stop_task(store: TaskStore, state: dict, reason="stopped parent") -> dict:
    return store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": reason,
        },
    )


def test_replace_task_and_rewire_preserves_old_parent_and_child_order(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    grandparent = store.create_task("Grandparent", requested_team="gp", task_id="task-gp")
    store.update(
        grandparent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )
    parent = store.create_task(
        "Parent", requested_team="parent", task_id="task-parent",
        depends_on_task_ids=("task-gp",),
    )
    child = store.create_task(
        "Child", requested_team="child", task_id="task-child",
        depends_on_task_ids=("task-gp", "task-parent"),
    )
    parent = _stop_task(store, parent)
    old_bytes = Path(parent["manifest_path"]).read_bytes()

    result = store.replace_task_and_rewire(
        "task-parent",
        "Continue parent safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-1",
    )
    replacement = result["replacement"]
    updated_child = store.load(child["manifest_path"])

    assert Path(parent["manifest_path"]).read_bytes() == old_bytes
    assert replacement["task_id"] != "task-parent"
    assert replacement["team"] == parent["team"]
    assert replacement["team_suffix"] == parent["team_suffix"]
    assert replacement["replaces_task_id"] == "task-parent"
    assert replacement["replacement_incident_id"] == "maint-1"
    assert replacement["depends_on_task_ids"] == ["task-gp"]
    assert replacement["hops"][0]["state"] == "pre_send"
    assert updated_child["depends_on_task_ids"] == ["task-gp", replacement["task_id"]]
    assert "child_task_ids" not in replacement and "child_task_ids" not in updated_child
    assert updated_child["dependency_events"][-1]["old_task_id"] == "task-parent"
    assert updated_child["dependency_events"][-1]["new_task_id"] == replacement["task_id"]
    assert replacement["dependency_events"][-1]["incident_id"] == "maint-1"

    repeated = store.replace_task_and_rewire(
        "task-parent",
        "Continue parent safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-1",
    )
    assert repeated["replacement"]["task_id"] == replacement["task_id"]
    assert store.load(child["manifest_path"])["depends_on_task_ids"] == [
        "task-gp", replacement["task_id"]
    ]






















def test_historical_terminal_inline_manifest_remains_readable_without_rewrite(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Historical inline task",
        requested_team="history-inline",
        task_id="task-history-inline",
    )
    state = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "options": {**current["options"], "report_mode": "inline"},
        },
    )
    terminal = _stop_task(store, state, reason="historical record")
    path = Path(terminal["manifest_path"])
    before = path.read_bytes()

    loaded = store.load(path)

    assert loaded["status"] == "STOPPED"
    assert loaded["options"]["report_mode"] == "inline"
    assert path.read_bytes() == before


def test_replacement_of_legacy_inline_task_is_file_mode(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    execution_repository = tmp_path.parent / f"{tmp_path.name}-replacement-execution"
    execution_repository.mkdir()
    original = store.create_task(
        "Legacy cross-workspace task",
        requested_team="legacy-cross",
        task_id="task-legacy-cross",
        repository=execution_repository,
    )
    original = store.update(
        original["manifest_path"],
        lambda state: {
            **state,
            "options": {**state["options"], "report_mode": "inline"},
        },
    )
    original = _stop_task(store, original)

    result = store.replace_task_and_rewire(
        original["task_id"],
        "Continue without the obsolete report contract",
        reuse_team=True,
        rewire_children=False,
        incident_id="legacy-cross-report",
    )

    assert result["replacement"]["repository"] == str(execution_repository.resolve())
    assert result["replacement"]["options"]["report_mode"] == "file"


def _block_task_for_replacement(store: TaskStore, state: dict) -> dict:
    return store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "send_failed",
            "block_reason": "unsafe blocked parent",
            "block_retryable": False,
        },
    )








def test_upload_paths_are_hashed_when_task_is_created(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    context = tmp_path / "context.md"
    context.write_text("evidence", encoding="utf-8")

    state = store.create_task(
        "Analyze uploaded context",
        requested_team="alpha",
        task_id="task-upload-create",
        upload_paths=[context],
    )

    assert state["attachments"] == [
        {
            "path": str(context.resolve()),
            "name": "context.md",
            "size": len("evidence"),
            "sha256": __import__("hashlib").sha256(b"evidence").hexdigest(),
            "mime_type": "text/markdown",
        }
    ]
    assert all(
        role["attachments_uploaded_generation"] is None
        for role in state["roles"].values()
    )
    manifest_text = Path(state["manifest_path"]).read_text(encoding="utf-8")
    assert "evidence" not in manifest_text
















def test_runtime_config_defaults_to_loopback_three_process_layout(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)

    assert config.runtime_database == (tmp_path / ".runtime" / "cdpa-control.sqlite3").resolve()
    assert config.dashboard_api_host == "127.0.0.1"
    assert config.dashboard_api_port == 9225
    assert config.dashboard_api_port != config.dashboard_port
    assert config.command_poll_seconds == 1
    assert config.heartbeat_seconds == 5
    assert config.browser_inventory_seconds == 5
    assert config.repository_allowed_roots == (tmp_path.parent.resolve(),)




def test_noop_manifest_update_performs_no_replace_or_timestamp_write(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("noop", requested_team="noop", task_id="task-noop")
    manifest = Path(state["manifest_path"])
    before_stat = manifest.stat()
    before = store.load(manifest)

    returned = store.update(manifest, lambda current: current)

    after_stat = manifest.stat()
    assert returned == before
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_change_goal_is_durable_next_hop_only_and_rejects_invalid_tasks(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Original goal", requested_team="goal", task_id="task-goal")
    state = store.update(state["manifest_path"], lambda current: {**current, "status": "RUNNING"})
    original_hop = json.loads(json.dumps(state["hops"][0]))
    changed = store.change_goal(state["manifest_path"], "Replacement goal\nwith full text", external_command_id="cmd-goal-1")
    assert changed["task_text"] == "Original goal"
    assert changed["effective_goal"] == "Replacement goal\nwith full text"
    assert changed["hops"][0] == original_hop
    assert changed["goal_revisions"][0]["applies_from_hop_id"] == 2
    assert changed["goal_revisions"][0]["external_command_id"] == "cmd-goal-1"
    assert effective_task_goal(changed) == "Replacement goal\nwith full text"
    assert task_goal_for_hop(changed, 1) == "Original goal"
    assert task_goal_for_hop(changed, 2) == "Replacement goal\nwith full text"
    latest = store.change_goal(changed["manifest_path"], "Latest replacement", external_command_id="cmd-goal-2")
    assert task_goal_for_hop(latest, 2) == "Latest replacement"
    replay = store.change_goal(latest["manifest_path"], "Latest replacement", external_command_id="cmd-goal-2")
    assert replay["goal_revisions"] == latest["goal_revisions"]
    with pytest.raises(ValueError, match="provenance"):
        store.change_goal(latest["manifest_path"], "Different payload", external_command_id="cmd-goal-2")
    with pytest.raises(ValueError, match="must not be blank"):
        store.change_goal(latest["manifest_path"], "   ")
    with pytest.raises(ValueError, match="unchanged"):
        store.change_goal(latest["manifest_path"], "Latest replacement")
    paused = store.update(latest["manifest_path"], lambda current: {**current, "status": "PAUSED"})
    before = Path(paused["manifest_path"]).read_bytes()
    with pytest.raises(ValueError, match="RUNNING"):
        store.change_goal(paused["manifest_path"], "Rejected")
    assert Path(paused["manifest_path"]).read_bytes() == before
    done = store.update(paused["manifest_path"], lambda current: {
        **current, "status": "DONE", "terminal_state": "DONE",
        "active_role": None, "active_hop_id": None,
    })
    with pytest.raises(ValueError, match="RUNNING"):
        store.change_goal(done["manifest_path"], "Rejected terminal")
    agent = store.create_independent_agent("Goal agent", system_prompt="Inspect", task_id="agent-goal-g1", enabled=True)
    agent = store.update(agent["manifest_path"], lambda current: {**current, "status": "RUNNING"})
    with pytest.raises(ValueError, match="workflow"):
        store.change_goal(agent["manifest_path"], "Rejected")
