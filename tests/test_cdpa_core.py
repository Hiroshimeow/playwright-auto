from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import playwright_auto.cdpa_store as store_module
from playwright_auto.cdpa_config import CDPAConfigError, load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto.cdpa_routes import RouteContractError, parse_route_response, validate_report
from playwright_auto.cdpa_store import TaskStore, utc_now
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
                    "maintainers_constructor": "prompts/cdpa/MAINTAINERS.md",
                    "response_guide": "prompts/cdpa/RESPONSE_GUIDE.md",
                },
                "roles": ["PLAN", "DEV", "REVIEW", "TEST", "AUDIT"],
                "dashboard": {"url": "http://127.0.0.1:9224", "port": 9224, "poll_seconds": 1},
                "browser": {"cdp_url": "http://127.0.0.1:9222", "workspace_timeout_seconds": 15},
                "route_repair": {"max_attempts": 3},
                "response": {"timeout_seconds": 7200, "refresh_after_seconds": 1200, "stable_ms": 1000, "poll_ms": 100},
                "maintenance": {"timeout_seconds": 300, "refresh_after_seconds": 120, "stable_ms": 1000, "poll_ms": 100},
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
    (root / "prompts" / "cdpa" / "MAINTAINERS.md").write_text(
        "# MAINTAINERS\nConstructor for Maintainers.\n", encoding="utf-8"
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


def test_packaged_default_config_targets_current_repository_without_local_config(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)

    assert config.repository_root == tmp_path.resolve()
    assert config.plans_root == (tmp_path / ".plan").resolve()
    assert config.config_path.name == "cdpa.json"
    assert all(path.is_file() for path in config.constructor_paths.values())
    assert config.response_guide_path.is_file()


def test_config_loads_root_json_compatible_yaml_and_validates_defaults(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    assert config.plans_root == (tmp_path / ".plan").resolve()
    assert config.route_repair_attempts == 3
    assert config.response_timeout_seconds == 7200
    assert config.response_refresh_after_seconds == 1200
    assert config.dashboard_url == "http://127.0.0.1:9224"


def test_dashboard_url_port_must_match_runtime_port(tmp_path: Path):
    config_path = write_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["dashboard"]["url"] = "http://127.0.0.1:9335"
    raw["dashboard"]["port"] = 9334
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CDPAConfigError, match="dashboard.url port must match dashboard.port"):
        load_cdpa_config(config_path, repository_root=tmp_path)


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

    envelope_text = first.text.split("\n\n# PLAN", 1)[0].removeprefix(
        "CDPA_TASK_ENVELOPE\n"
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


def test_exact_team_resume_fails_closed_for_missing_terminal_and_duplicate(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    with pytest.raises(ValueError, match="no CDPA task exists"):
        store.resume_team("missing")

    terminal = store.create_task("terminal", requested_team="alpha", task_id="task-terminal")
    store.update(
        terminal["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
        },
    )
    with pytest.raises(ValueError, match="no resumable nonterminal task"):
        store.resume_team("alpha")

    first = store.create_task("one", requested_team="beta", task_id="task-one")
    second = store.create_task("two", requested_team="gamma", task_id="task-two")
    second_path = Path(second["manifest_path"])
    duplicate_path = config.plans_root / "beta" / "task-two" / "two.json"
    duplicate = json.loads(json.dumps(second))
    duplicate.update({
        "manifest_path": str(duplicate_path.resolve()),
        "requested_team": "beta",
        "team_base": "beta",
        "team": "beta",
        "team_suffix": 1,
    })
    for logical, record in duplicate["roles"].items():
        record["physical_role"] = physical_role(logical, "beta", 1)
    for hop in duplicate["hops"]:
        hop["physical_role"] = duplicate["roles"][hop["target_role"]]["physical_role"]
        hop["ledger_path"] = str((duplicate_path.parent / "requests.json").resolve())
    duplicate_path.parent.mkdir(parents=True, exist_ok=True)
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    second_path.unlink()

    with pytest.raises(ValueError, match="duplicate nonterminal manifests"):
        store.resume_team("beta")


def test_exact_team_resume_reports_unreadable_state(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    task = store.create_task("bad state", requested_team="alpha", task_id="task-bad")
    Path(task["manifest_path"]).write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable manifest"):
        store.resume_team("alpha")

    store.catalog_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable catalog"):
        store.resume_team("alpha")


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


def test_corrupt_manifest_reserves_only_its_team_and_task_identity(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    corrupt = Path(state["manifest_path"])
    corrupt.write_text("{broken", encoding="utf-8")

    unrelated = store.create_task("Second", requested_team="beta", task_id="task-b")
    replacement = store.create_task("Replacement", requested_team="alpha", task_id="task-c")

    assert unrelated["team"] == "beta"
    assert replacement["team"] == "alpha2"
    with pytest.raises(ValueError, match="already cataloged"):
        store.create_task("Same identity", requested_team="gamma", task_id="task-a")
    tasks, errors = store.discover_with_errors()
    assert {(task["team"], task["task_id"]) for task in tasks} == {
        ("beta", "task-b"),
        ("alpha2", "task-c"),
    }
    assert errors == [
        {
            "manifest_path": str(corrupt.resolve()),
            "error": errors[0]["error"],
        }
    ]
    assert errors[0]["error"].startswith("InvalidManifestError:")


def test_create_rejects_valid_filesystem_task_id_when_catalog_is_missing(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    existing = store.create_task(
        "Existing", requested_team="alpha", task_id="task-existing"
    )
    existing_path = Path(existing["manifest_path"])
    existing_before = existing_path.read_bytes()
    store.catalog_path.unlink()

    with pytest.raises(ValueError, match="task_id is already"):
        store.create_task(
            "Duplicate",
            requested_team="beta",
            task_id=existing["task_id"],
        )

    assert existing_path.read_bytes() == existing_before
    assert [path for path in store.root.glob("*/*/*.json") if path.name != "requests.json"] == [
        existing_path
    ]


def test_malformed_primary_without_catalog_remains_diagnostic_and_reserves_suffix(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    corrupt = Path(state["manifest_path"])
    corrupt.write_text("{broken", encoding="utf-8")
    store.catalog_path.unlink()

    tasks, errors = store.discover_with_errors()
    assert tasks == []
    assert len(errors) == 1
    assert errors[0]["manifest_path"] == str(corrupt.resolve())
    assert "JSONDecodeError" in errors[0]["error"]
    with pytest.raises(ValueError, match="corrupt unreadable manifest"):
        store.resume_team("alpha")
    with pytest.raises(ValueError, match="reserved by corrupt filesystem state"):
        store.create_task("Same task", requested_team="beta", task_id="task-a")

    replacement = store.create_task("Replacement", requested_team="alpha", task_id="task-b")
    unrelated = store.create_task("Unrelated", requested_team="beta", task_id="task-c")
    assert replacement["team"] == "alpha2"
    assert unrelated["team"] == "beta"
    assert corrupt.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("variant", ["evidence", "partial"])
def test_cataloged_invalid_resume_candidates_fail_without_mutation(
    tmp_path: Path, variant: str
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    task_dir = config.plans_root / "alpha" / "task-a"
    task_dir.mkdir(parents=True)
    candidate = task_dir / f"{variant}.json"
    if variant == "evidence":
        candidate_state = {
            "schema_version": 1,
            "manifest_path": str(candidate.resolve()),
            "team": "alpha",
            "task_id": "task-a",
            "team_suffix": 1,
            "status": "RUNNING",
            "active_role": "PLAN",
            "controls": [],
        }
    else:
        candidate_state = {
            "schema_version": 1,
            "manifest_path": str(candidate.resolve()),
            "task_id": "task-a",
            "task_title": "partial",
            "task_text": "partial",
            "task_slug": "partial",
            "repository": str(tmp_path),
            "team_base": "alpha",
            "team": "alpha",
            "team_suffix": 1,
            "status": "RUNNING",
            "kanban_column": "WORKING",
            "active_role": "PLAN",
            "active_hop_id": 1,
            "active_action": "wait_response",
            "created_at": "2026-07-22T00:00:00+00:00",
            "updated_at": "2026-07-22T00:00:00+00:00",
            "controls": [],
            "reports": [],
            "route_timeline": [],
            "errors": [],
            "options": {},
            "cleanup": {"state": "ACTIVE", "phase": None},
        }
    candidate.write_text(json.dumps(candidate_state), encoding="utf-8")
    catalog = {
        "version": 1,
        "entries": {
            f"alpha/task-a/{variant}.json": {
                "manifest_path": str(candidate.resolve()),
                "task_id": "task-a",
                "team": "alpha",
                "team_suffix": 1,
                "status": "RUNNING",
                "updated_at": "2026-07-22T00:00:00+00:00",
            }
        },
    }
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    before = candidate.read_bytes()

    with pytest.raises(ValueError, match="corrupt cataloged manifest"):
        store.resume_team("alpha")

    assert candidate.read_bytes() == before
    assert json.loads(candidate.read_text(encoding="utf-8"))["controls"] == []
    assert store.discover_paths() == []


@pytest.mark.parametrize("poison", ["key", "team", "task"])
def test_exact_team_resume_rejects_wrong_catalog_identity(tmp_path: Path, poison: str):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    key, entry = next(iter(catalog["entries"].items()))
    if poison == "key":
        catalog["entries"]["alpha/wrong-task/wrong.json"] = dict(entry)
    elif poison == "team":
        catalog["entries"][key]["team"] = "wrong-team"
    else:
        catalog["entries"][key]["task_id"] = "wrong-task"
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    before = manifest.read_bytes()

    with pytest.raises(
        ValueError,
        match="corrupt (?:raw catalog entry|cataloged manifest)|duplicate raw catalog entries",
    ):
        store.resume_team("alpha")

    assert manifest.read_bytes() == before
    assert json.loads(manifest.read_text(encoding="utf-8"))["controls"] == []


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("non_mapping", "entry must be an object"),
        ("missing_path", "manifest_path must be a non-empty string"),
        ("empty_path", "manifest_path must be a non-empty string"),
        ("malformed_key", "catalog key must use"),
        ("missing_team", "team must be a non-empty string"),
        ("missing_task", "task_id must be a non-empty string"),
        ("bad_suffix", "team_suffix must be a positive integer"),
        ("bad_status", "status must be a supported"),
        ("duplicate_path", "catalog key does not match"),
    ],
)
def test_exact_team_resume_rejects_every_malformed_associated_raw_catalog_entry(
    tmp_path: Path,
    variant: str,
    expected: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    key, valid_entry = next(iter(catalog["entries"].items()))
    bad_key = "alpha/task-a/bad.json"
    bad_entry: object = dict(valid_entry)
    bad_entry["manifest_path"] = str((manifest.parent / "bad.json").resolve())
    if variant == "non_mapping":
        bad_entry = "not-an-object"
    elif variant == "missing_path":
        bad_entry.pop("manifest_path")
    elif variant == "empty_path":
        bad_entry["manifest_path"] = ""
    elif variant == "malformed_key":
        bad_key = "alpha/task-a"
    elif variant == "missing_team":
        bad_entry.pop("team")
    elif variant == "missing_task":
        bad_entry.pop("task_id")
    elif variant == "bad_suffix":
        bad_entry["team_suffix"] = "1"
    elif variant == "bad_status":
        bad_entry["status"] = 7
    elif variant == "duplicate_path":
        bad_entry["manifest_path"] = str(manifest.resolve())
    catalog["entries"][bad_key] = bad_entry
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    catalog_before = store.catalog_path.read_bytes()
    manifest_before = manifest.read_bytes()

    with pytest.raises(ValueError, match=expected):
        store.resume_team("alpha")

    assert store.catalog_path.read_bytes() == catalog_before
    assert manifest.read_bytes() == manifest_before
    assert json.loads(manifest.read_text(encoding="utf-8"))["controls"] == []


def test_exact_team_resume_preserves_long_allocated_suffix_identity(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    base = "a" * 48
    first = store.create_task("First", requested_team=base, task_id="task-a")
    second = store.create_task("Second", requested_team=base, task_id="task-b")
    assert first["team"] == base
    assert second["team"] == f"{base}2"
    assert len(second["team"]) == 49
    first_manifest = Path(first["manifest_path"])
    second_manifest = Path(second["manifest_path"])
    first_before = first_manifest.read_bytes()

    resumed = store.resume_team(second["team"])

    assert resumed["team"] == second["team"]
    assert resumed["task_id"] == "task-b"
    assert resumed["manifest_path"] == str(second_manifest.resolve())
    assert first_manifest.read_bytes() == first_before
    assert json.loads(first_manifest.read_text(encoding="utf-8"))["controls"] == []
    assert len(json.loads(second_manifest.read_text(encoding="utf-8"))["controls"]) == 1


@pytest.mark.parametrize(
    "invalid",
    ["Alpha", " alpha", "alpha ", "alpha!", "a" * 129],
)
def test_exact_team_resume_rejects_invalid_identifier_without_coercion(
    tmp_path: Path, invalid: str
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    before = manifest.read_bytes()

    with pytest.raises(ValueError, match="exact team"):
        store.resume_team(invalid)

    assert manifest.read_bytes() == before


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("wrong_suffix", "team_suffix"),
        ("wrong_status", "status"),
        ("missing_updated_at", "updated_at must be a non-empty string"),
        ("typed_updated_at", "updated_at must be a non-empty string"),
        ("stale_updated_at", "updated_at"),
    ],
)
def test_exact_team_resume_rejects_catalog_metadata_inconsistent_with_manifest(
    tmp_path: Path,
    variant: str,
    expected: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    key, entry = next(iter(catalog["entries"].items()))
    if variant == "wrong_suffix":
        entry["team_suffix"] = 7
    elif variant == "wrong_status":
        entry["status"] = "DONE"
    elif variant == "missing_updated_at":
        entry.pop("updated_at")
    elif variant == "typed_updated_at":
        entry["updated_at"] = 123
    else:
        entry["updated_at"] = "2000-01-01T00:00:00+00:00"
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    catalog_before = store.catalog_path.read_bytes()
    manifest_before = manifest.read_bytes()

    with pytest.raises(ValueError, match=expected):
        store.resume_team("alpha")

    assert store.catalog_path.read_bytes() == catalog_before
    assert manifest.read_bytes() == manifest_before
    assert json.loads(manifest.read_text(encoding="utf-8"))["controls"] == []


def test_exact_team_resume_accepts_complete_canonical_catalog_entry(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    catalog_before = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    key, entry = next(iter(catalog_before["entries"].items()))
    assert entry == store._catalog_entry(state)

    resumed = store.resume_team("alpha")

    assert resumed["task_id"] == "task-a"
    assert resumed["team"] == "alpha"
    catalog_after = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert catalog_after["entries"][key] == store._catalog_entry(resumed)
    controls = json.loads(manifest.read_text(encoding="utf-8"))["controls"]
    assert len(controls) == 1
    assert controls[0]["action"] == "resume"


def test_resume_locked_revalidation_rejects_catalog_manifest_timestamp_race(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    catalog_before = store.catalog_path.read_bytes()
    target_lock = store._lock_path(manifest).resolve()
    original_lock = store_module.exclusive_file_lock
    manifest_lock_exits = 0

    @contextmanager
    def racing_lock(path, *args, **kwargs):
        nonlocal manifest_lock_exits
        with original_lock(path, *args, **kwargs):
            yield
        if Path(path).resolve() == target_lock:
            manifest_lock_exits += 1
            if manifest_lock_exits == 1:
                changed = json.loads(manifest.read_text(encoding="utf-8"))
                changed["updated_at"] = "2099-01-01T00:00:00+00:00"
                manifest.write_text(json.dumps(changed), encoding="utf-8")

    monkeypatch.setattr(store_module, "exclusive_file_lock", racing_lock)

    with pytest.raises(ValueError, match="updated_at"):
        store.resume_team("alpha")

    assert store.catalog_path.read_bytes() == catalog_before
    current = json.loads(manifest.read_text(encoding="utf-8"))
    assert current["updated_at"] == "2099-01-01T00:00:00+00:00"
    assert current["controls"] == []


def test_resume_revalidates_inside_lock_when_candidate_becomes_corrupt(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    target_lock = store._lock_path(manifest).resolve()
    original_lock = store_module.exclusive_file_lock
    manifest_lock_exits = 0

    @contextmanager
    def racing_lock(path, *args, **kwargs):
        nonlocal manifest_lock_exits
        with original_lock(path, *args, **kwargs):
            yield
        if Path(path).resolve() == target_lock:
            manifest_lock_exits += 1
            if manifest_lock_exits == 1:
                manifest.write_text("{broken", encoding="utf-8")

    monkeypatch.setattr(store_module, "exclusive_file_lock", racing_lock)

    with pytest.raises(ValueError, match="became corrupt while resuming"):
        store.resume_team("alpha")

    assert manifest.read_text(encoding="utf-8") == "{broken"


def test_invalid_update_is_rejected_before_atomic_replacement(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    before = manifest.read_bytes()

    with pytest.raises(ValueError, match="refusing to write invalid"):
        store.update(manifest, lambda current: {**current, "roles": {}})

    assert manifest.read_bytes() == before
    assert store.load(manifest)["task_id"] == "task-a"


def test_malformed_noncanonical_evidence_outside_task_layout_is_ignored(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    evidence = config.plans_root / "alpha" / "evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{broken", encoding="utf-8")

    tasks, errors = store.discover_with_errors()
    assert tasks == []
    assert errors == []
    created = store.create_task("Task", requested_team="alpha", task_id="task-a")
    assert created["team"] == "alpha"


def test_discover_paths_ignores_request_ledgers_inside_task_directory(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    ledger = Path(state["manifest_path"]).parent / "requests.json"
    ledger.write_text('{"version": 2, "requests": {}}', encoding="utf-8")

    assert store.discover_paths() == [Path(state["manifest_path"])]
    assert [task["task_id"] for task in store.discover()] == ["task-a"]


def test_catalog_discovery_rejects_poisoned_nonprimary_and_mismatched_entries(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    task_dir = manifest.parent
    ledger = task_dir / "requests.json"
    evidence = task_dir / "evidence.json"
    mismatch = task_dir / "mismatch.json"
    ledger.write_text('{"version": 2, "requests": {}}', encoding="utf-8")
    evidence.write_text('{"kind": "evidence"}', encoding="utf-8")
    mismatch_state = dict(state)
    mismatch_state["manifest_path"] = str(manifest)
    mismatch.write_text(json.dumps(mismatch_state), encoding="utf-8")

    catalog_path = config.plans_root / ".cdpa-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["entries"].update({
        "alpha/task-a/requests.json": {
            "manifest_path": str(ledger.resolve()),
            "task_id": "task-a",
            "team": "alpha",
            "team_suffix": 1,
            "status": "RUNNING",
        },
        "alpha/task-a/evidence.json": {
            "manifest_path": str(evidence.resolve()),
            "task_id": "task-a",
            "team": "alpha",
            "team_suffix": 1,
            "status": "RUNNING",
        },
        "alpha/task-a/mismatch.json": {
            "manifest_path": str(mismatch.resolve()),
            "task_id": "task-a",
            "team": "alpha",
            "team_suffix": 1,
            "status": "RUNNING",
        },
        "wrong/catalog-key.json": {
            "manifest_path": str(manifest.resolve()),
            "task_id": "wrong-task",
            "team": "wrong-team",
            "team_suffix": 1,
            "status": "RUNNING",
        },
    })
    catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    assert store.discover_paths() == [manifest.resolve()]
    tasks, errors = store.discover_with_errors()
    assert [task["task_id"] for task in tasks] == ["task-a"]
    assert len(errors) == 4
    assert all(item["error"].startswith("InvalidManifestError:") for item in errors)
    assert {Path(item["manifest_path"]).name for item in errors} == {
        "requests.json", "evidence.json", "mismatch.json", manifest.name
    }


def test_catalog_valid_primary_entry_remains_discoverable(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")

    assert store.discover_paths() == [Path(state["manifest_path"])]
    tasks, errors = store.discover_with_errors()
    assert [task["task_id"] for task in tasks] == ["task-a"]
    assert errors == []


def test_schema_shaped_evidence_and_partial_manifest_are_diagnostic_only(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    evidence = manifest.parent / "evidence.json"
    partial = manifest.parent / "partial.json"
    evidence.write_text(
        json.dumps({
            "schema_version": 1,
            "manifest_path": str(evidence.resolve()),
            "team": "alpha",
            "task_id": "task-a",
            "kind": "evidence",
        }),
        encoding="utf-8",
    )
    partial_state = json.loads(json.dumps(state))
    partial_state.update({
        "manifest_path": str(partial.resolve()),
        "task_title": "partial",
        "task_text": "partial",
        "task_slug": "partial",
    })
    partial_state.pop("roles")
    partial_state.pop("hops")
    partial.write_text(json.dumps(partial_state), encoding="utf-8")

    assert store.discover_paths() == [manifest.resolve()]
    tasks, errors = store.discover_with_errors()
    assert [task["task_id"] for task in tasks] == ["task-a"]
    assert {Path(item["manifest_path"]).name for item in errors} == {
        "evidence.json",
        "partial.json",
    }
    assert all(item["error"].startswith("InvalidManifestError:") for item in errors)
    with pytest.raises(ValueError, match="invalid CDPA task manifest"):
        store.load(evidence)
    with pytest.raises(ValueError, match="invalid CDPA task manifest"):
        store.load(partial)
    assert store.load(manifest)["task_id"] == "task-a"


def test_full_copied_manifest_under_second_canonical_filename_fails_unique_primary(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    copied = manifest.parent / "copied-primary.json"
    copied_state = json.loads(json.dumps(state))
    copied_state.update({
        "manifest_path": str(copied.resolve()),
        "task_title": "copied primary",
        "task_text": "copied primary",
        "task_slug": "copied-primary",
    })
    copied.write_text(json.dumps(copied_state), encoding="utf-8")

    assert store.discover_paths() == []
    tasks, errors = store.discover_with_errors()
    assert tasks == []
    assert {Path(item["manifest_path"]).name for item in errors} == {
        manifest.name,
        copied.name,
    }
    assert all("exactly one canonical primary manifest" in item["error"] for item in errors)
    with pytest.raises(ValueError, match="exactly one canonical primary manifest"):
        store.load(manifest)
    with pytest.raises(ValueError, match="exactly one canonical primary manifest"):
        store.load(copied)


def test_direct_load_rejects_wrong_physical_role_and_active_hop_identity(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    invalid = manifest.parent / "invalid-identity.json"
    invalid_state = json.loads(json.dumps(state))
    invalid_state.update({
        "manifest_path": str(invalid.resolve()),
        "task_title": "invalid identity",
        "task_text": "invalid identity",
        "task_slug": "invalid-identity",
    })
    invalid_state["roles"]["PLAN"]["physical_role"] = "wrong-plan"
    invalid.write_text(json.dumps(invalid_state), encoding="utf-8")

    with pytest.raises(ValueError, match="physical_role"):
        store.load(invalid)
    assert store.discover_paths() == [manifest.resolve()]


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


def test_team_owner_and_queue_order_are_shared_pure_predicates():
    tasks = [
        {"task_id": "owner", "team": "alpha", "status": "RUNNING"},
        {
            "task_id": "task-z",
            "team": "alpha",
            "status": "WAITING",
            "created_at": "2026-07-23T00:00:00+00:00",
            "queue": {"reuse_team": True, "released_at": None},
        },
        {
            "task_id": "task-a",
            "team": "alpha",
            "status": "WAITING",
            "created_at": "2026-07-23T00:00:00+00:00",
            "queue": {"reuse_team": True, "released_at": None},
        },
    ]
    assert is_active_team_owner(tasks[0]) is True
    assert is_active_team_owner(tasks[1]) is False
    assert [item["task_id"] for item in queued_team_tasks(tasks, "alpha")] == [
        "task-a",
        "task-z",
    ]


def test_reuse_team_creates_waiting_task_without_allocating_suffix(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("one", requested_team="alpha", task_id="task-one")

    second = store.create_task("two", reuse_team="alpha", task_id="task-two")

    assert second["team"] == "alpha"
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


def test_reuse_team_requires_exact_existing_team_and_preserves_terminal_slot(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("one", requested_team="alpha", task_id="task-one")
    store.update(
        first["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )

    queued = store.create_task("two", reuse_team="alpha", task_id="task-two")

    assert queued["team"] == "alpha"
    assert queued["team_suffix"] == 1
    assert queued["reusable_teams"] == ["alpha"]
    assert queued["status"] == "WAITING"
    assert queued["waiting"]["blocked_by_task_id"] is None
    with pytest.raises(ValueError, match="exact team"):
        store.create_task("missing", reuse_team="missing", task_id="task-missing")
    with pytest.raises(ValueError, match="mutually exclusive"):
        store.create_task(
            "invalid",
            requested_team="alpha",
            reuse_team="alpha",
            task_id="task-invalid",
        )


def test_reuse_team_validates_exact_raw_catalog_before_any_write(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    key = store._catalog_key(owner["manifest_path"])
    catalog["entries"][key]["status"] = "DONE"
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    before_catalog = store.catalog_path.read_bytes()
    before_paths = store.discover_paths()

    with pytest.raises(ValueError, match="catalog field 'status' does not match"):
        store.create_task("queued", reuse_team="alpha", task_id="task-queued")

    assert store.catalog_path.read_bytes() == before_catalog
    assert store.discover_paths() == before_paths


def test_reuse_team_and_resume_treat_clearing_owner_as_availability_barrier(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    owner = store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "team cleared",
            "cleanup": {
                **state["cleanup"],
                "state": "CLEARING",
                "phase": "close_pending",
                "verified_empty_at": None,
            },
        },
    )

    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")

    assert queued["status"] == "WAITING"
    assert queued["waiting_code"] == "team_busy"
    assert queued["waiting"]["blocked_by_task_id"] == owner["task_id"]
    before = Path(queued["manifest_path"]).read_bytes()
    with pytest.raises(ValueError, match="cleanup is still clearing"):
        store.resume_team("alpha")
    assert Path(queued["manifest_path"]).read_bytes() == before

    store.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "cleanup": {
                **state["cleanup"],
                "state": "CLEARED",
                "phase": "cleared",
                "cleared_at": utc_now(),
                "verified_empty_at": utc_now(),
            },
        },
    )
    resumed = store.resume_team("alpha")
    assert resumed["task_id"] == queued["task_id"]
    assert resumed["status"] == "INBOX"
    assert resumed["queue"]["released_at"]


def test_reuse_team_combines_dependency_and_team_waiting_evidence(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    parent = store.create_task("parent", requested_team="parent", task_id="task-parent")

    queued = store.create_task(
        "queued",
        reuse_team="alpha",
        task_id="task-queued",
        depends_on_task_ids=("task-parent",),
    )

    assert queued["status"] == "WAITING"
    assert queued["waiting"]["reason"] == "dependency_team_busy"
    assert queued["waiting"]["waiting_on"] == ["task-parent"]
    assert queued["waiting"]["blocked_by_task_id"] == owner["task_id"]
    assert queued["waiting_code"] == "dependency_team_busy"
    assert parent["task_id"] in queued["waiting_reason"]


def test_queue_manifest_validation_rejects_persisted_derived_or_malformed_fields(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    path = Path(queued["manifest_path"])
    base = json.loads(path.read_text(encoding="utf-8"))

    for field, value in (
        ("queue", []),
        ("queue", {"reuse_team": "yes", "blocked_by_task_id": None, "enqueued_at": queued["created_at"], "released_at": None}),
        ("queue_position", 1),
        ("queue_length", 2),
        ("owner_task_ids", ["task-owner"]),
    ):
        raw = json.loads(json.dumps(base))
        raw[field] = value
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError):
            store.load(path)
    path.write_text(json.dumps(base), encoding="utf-8")


@pytest.mark.parametrize("owner_failure", ["missing", "invalid_json"])
def test_queue_scheduling_fails_closed_when_exact_team_owner_is_unreadable(
    tmp_path: Path,
    owner_failure: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    queued_path = Path(queued["manifest_path"])
    before = queued_path.read_bytes()
    owner_path = Path(owner["manifest_path"])
    if owner_failure == "missing":
        owner_path.unlink()
    else:
        owner_path.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="exact team 'alpha'"):
        store.refresh_scheduling(queued_path)

    assert queued_path.read_bytes() == before
    unchanged = json.loads(before)
    assert unchanged["status"] == "WAITING"
    assert unchanged["queue"]["released_at"] is None
    assert unchanged["hops"][0]["state"] == "pre_send"


@pytest.mark.parametrize(
    "catalog_corruption",
    [
        "status_mismatch",
        "non_mapping",
        "missing_manifest_path",
        "wrong_three_part_key",
        "backslash_key",
        "identity_missing_path",
        "identity_outside_path",
        "identity_ghost_path",
        "identity_other_manifest_path",
    ],
)
def test_discovery_reports_catalog_mismatch_without_reconciling_it(
    tmp_path: Path,
    catalog_corruption: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    unrelated = store.create_task(
        "unrelated", requested_team="beta", task_id="task-unrelated"
    )
    owner_key = store._catalog_key(owner["manifest_path"])
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    poisoned_key = owner_key
    if catalog_corruption == "status_mismatch":
        catalog["entries"][owner_key]["status"] = "DONE"
    elif catalog_corruption == "non_mapping":
        catalog["entries"][owner_key] = "corrupt"
    elif catalog_corruption == "missing_manifest_path":
        del catalog["entries"][owner_key]["manifest_path"]
    else:
        entry = catalog["entries"].pop(owner_key)
        if catalog_corruption == "wrong_three_part_key":
            poisoned_key = "wrong/task-owner/owner.json"
        elif catalog_corruption == "backslash_key":
            poisoned_key = "alpha\\bad/task-owner/owner.json"
        else:
            poisoned_key = "bad"
            if catalog_corruption == "identity_missing_path":
                del entry["manifest_path"]
            elif catalog_corruption == "identity_outside_path":
                entry["manifest_path"] = str((tmp_path / "outside-owner.json").resolve())
            elif catalog_corruption == "identity_ghost_path":
                entry["manifest_path"] = str(
                    (store.root / "ghost" / "task-owner" / "owner.json").resolve()
                )
            else:
                entry["manifest_path"] = unrelated["manifest_path"]
        catalog["entries"][poisoned_key] = entry
    poisoned = json.loads(json.dumps(catalog["entries"][poisoned_key]))
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    tasks, errors = store.discover_with_errors()

    assert {task["task_id"] for task in tasks} == {
        queued["task_id"],
        unrelated["task_id"],
    }
    assert any(
        item["manifest_path"] == owner["manifest_path"]
        and "catalog" in item["error"].lower()
        for item in errors
    )
    after = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert after["entries"][poisoned_key] == poisoned
    if poisoned_key != owner_key:
        assert owner_key not in after["entries"]


def test_valid_catalog_entry_wins_over_malformed_declared_identity_duplicate(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    owner_key = store._catalog_key(owner["manifest_path"])
    duplicate = json.loads(json.dumps(catalog["entries"][owner_key]))
    del duplicate["manifest_path"]
    catalog["entries"]["bad"] = duplicate
    poisoned = json.loads(json.dumps(catalog["entries"]["bad"]))
    store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    tasks, errors = store.discover_with_errors()

    assert [task["task_id"] for task in tasks] == [owner["task_id"]]
    assert any(
        item["manifest_path"] == owner["manifest_path"]
        and "raw catalog entry 'bad'" in item["error"]
        for item in errors
    )
    after = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert after["entries"][owner_key] == catalog["entries"][owner_key]
    assert after["entries"]["bad"] == poisoned


def test_unrelated_create_does_not_reconcile_catalog_invalid_owner(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    owner_key = store._catalog_key(owner["manifest_path"])
    owner_before = Path(owner["manifest_path"]).read_bytes()
    poisoned = poison_catalog_identity_entry(store, owner)

    before, _errors = store.discover_with_errors()
    created = store.create_task(
        "Unrelated", requested_team="beta", task_id="task-unrelated"
    )
    after, _errors = store.discover_with_errors()

    assert before == []
    assert [task["task_id"] for task in after] == [created["task_id"]]
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert owner_key not in catalog["entries"]
    assert catalog["entries"]["bad"] == poisoned
    assert Path(owner["manifest_path"]).read_bytes() == owner_before


def test_dependency_create_rejects_catalog_invalid_parent_without_writes(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team="alpha", task_id="task-parent"
    )
    parent = store.update(
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
    poison_catalog_identity_entry(store, parent)
    before_catalog = store.catalog_path.read_bytes()
    before_files = sorted(store.root.glob("*/*/*.json"))

    with pytest.raises(ValueError, match="missing dependency"):
        store.create_task(
            "Child",
            requested_team="beta",
            task_id="task-child",
            depends_on_task_ids=(parent["task_id"],),
        )

    assert store.catalog_path.read_bytes() == before_catalog
    assert sorted(store.root.glob("*/*/*.json")) == before_files
    tasks, _errors = store.discover_with_errors()
    assert tasks == []


@pytest.mark.parametrize("parent_status", ["DONE", "STOPPED"])
def test_refresh_scheduling_excludes_catalog_invalid_dependency_parent(
    tmp_path: Path,
    parent_status: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team="alpha", task_id="task-parent"
    )
    child = store.create_task(
        "Child",
        requested_team="beta",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    unrelated = store.create_task(
        "Unrelated", requested_team="gamma", task_id="task-unrelated"
    )
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    parent = store.update(
        parent_path,
        lambda state: {
            **state,
            "status": parent_status,
            "terminal_state": parent_status,
            "active_role": None,
            "active_hop_id": None,
            **(
                {"completed_at": utc_now()}
                if parent_status == "DONE"
                else {"stopped_at": utc_now(), "stop_reason": "failed parent"}
            ),
        },
    )
    parent_before = parent_path.read_bytes()
    poisoned = poison_catalog_identity_entry(store, parent)

    tasks, errors = store.discover_with_errors()
    refreshed, changed = store.refresh_scheduling(child_path)

    assert {task["task_id"] for task in tasks} == {
        child["task_id"],
        unrelated["task_id"],
    }
    assert any(
        item["manifest_path"] == parent["manifest_path"]
        and "raw catalog entry 'bad'" in item["error"]
        for item in errors
    )
    assert changed is True
    assert refreshed["status"] == "WAITING"
    assert refreshed["waiting_code"] == "dependency_missing"
    assert refreshed["waiting"]["missing"] == ["task-parent"]
    assert refreshed["hops"][0]["state"] == "pre_send"
    assert not any(
        item["status"] == "RELEASED" for item in refreshed["dependency_events"]
    )
    assert parent_path.read_bytes() == parent_before
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert catalog["entries"]["bad"] == poisoned


@pytest.mark.parametrize("parent_status", ["DONE", "STOPPED"])
def test_exact_team_resume_does_not_release_queue_through_catalog_invalid_parent(
    tmp_path: Path,
    parent_status: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "parent", requested_team="alpha", task_id="task-parent"
    )
    owner = store.create_task(
        "owner", requested_team="beta", task_id="task-owner"
    )
    queued = store.create_task(
        "queued",
        reuse_team="beta",
        task_id="task-queued",
        depends_on_task_ids=(parent["task_id"],),
    )
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": parent_status,
            "terminal_state": parent_status,
            "active_role": None,
            "active_hop_id": None,
            **(
                {"completed_at": utc_now()}
                if parent_status == "DONE"
                else {"stopped_at": utc_now(), "stop_reason": "failed parent"}
            ),
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
    queued_path = Path(queued["manifest_path"])
    poisoned = poison_catalog_identity_entry(store, parent)
    queued_before = queued_path.read_bytes()
    catalog_before = store.catalog_path.read_bytes()

    tasks, errors = store.discover_with_errors()
    assert parent["task_id"] not in {task["task_id"] for task in tasks}
    assert any(
        item["manifest_path"] == parent["manifest_path"]
        and "raw catalog entry 'bad'" in item["error"]
        for item in errors
    )

    with pytest.raises(ValueError, match="queued tasks are not dependency-ready"):
        store.resume_team("beta", reason="test resume")

    assert queued_path.read_bytes() == queued_before
    assert store.catalog_path.read_bytes() == catalog_before
    current = store.load(queued_path)
    assert current["status"] == "WAITING"
    assert current["queue"]["released_at"] is None
    assert not any(
        event.get("status") == "RELEASED" for event in current["queue_events"]
    )
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert catalog["entries"]["bad"] == poisoned


def test_exact_team_resume_accepts_active_owner_with_queued_tasks_and_releases_oldest_ready(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued_b = store.create_task("queued b", reuse_team="alpha", task_id="task-b")
    queued_a = store.create_task("queued a", reuse_team="alpha", task_id="task-a")
    same_time = "2026-07-23T00:00:00+00:00"
    for queued in (queued_a, queued_b):
        store.update(
            queued["manifest_path"],
            lambda state, same_time=same_time: {
                **state,
                "created_at": same_time,
                "queue": {**state["queue"], "enqueued_at": same_time},
            },
        )

    resumed_owner = store.resume_team("alpha", reason="owner resume")
    assert resumed_owner["task_id"] == owner["task_id"]

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
    resumed_queue = store.resume_team("alpha", reason="queue resume")
    assert resumed_queue["task_id"] == "task-a"
    assert resumed_queue["status"] == "INBOX"
    assert resumed_queue["queue"]["released_at"]
    assert resumed_queue["queue_events"][-1]["status"] == "RELEASED"
    assert resumed_queue["controls"][-1]["action"] == "resume"


def test_taskless_resume_releases_dependency_ready_ordinary_waiter(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("parent", requested_team="parent", task_id="task-parent")
    waiter = store.create_task(
        "waiter",
        requested_team="alpha",
        task_id="task-waiter",
        depends_on_task_ids=(parent["task_id"],),
    )
    original_hop = json.loads(json.dumps(waiter["hops"][0]))
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

    resumed = store.resume_team("alpha", reason="dependency-ready resume")

    assert resumed["task_id"] == waiter["task_id"]
    assert resumed["status"] == "INBOX"
    assert resumed["waiting_code"] is None
    assert resumed["controls"][-1]["action"] == "resume"
    assert resumed["controls"][-1]["reason"] == "dependency-ready resume"
    for field in ("hop_id", "turn", "request_id", "state", "handoff", "ledger_path"):
        assert resumed["hops"][0][field] == original_hop[field]


def test_taskless_resume_mixed_waiters_selects_worker_winner(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("parent", requested_team="parent", task_id="task-parent")
    first = store.create_task(
        "first",
        requested_team="alpha",
        task_id="task-first",
        depends_on_task_ids=(parent["task_id"],),
    )
    second = store.create_task(
        "second",
        reuse_team="alpha",
        task_id="task-second",
        depends_on_task_ids=(parent["task_id"],),
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
    losing_path = Path(second["manifest_path"])
    losing_before = losing_path.read_bytes()

    resumed = store.resume_team("alpha")

    assert resumed["task_id"] == first["task_id"]
    assert resumed["status"] == "INBOX"
    assert resumed["queue"] is None
    assert losing_path.read_bytes() == losing_before
    losing, changed = store.refresh_scheduling(second["manifest_path"])
    assert changed is True
    assert losing["status"] == "WAITING"
    assert losing["waiting"]["blocked_by_task_id"] == first["task_id"]


def test_dependency_waiter_does_not_release_beside_active_reuse_team_task(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("parent", requested_team="parent", task_id="task-parent")
    first = store.create_task(
        "first",
        requested_team="alpha",
        task_id="task-first",
        depends_on_task_ids=(parent["task_id"],),
    )
    second = store.create_task("second", reuse_team="alpha", task_id="task-second")

    released, changed = store.refresh_scheduling(second["manifest_path"])
    assert changed is True
    assert released["status"] == "INBOX"

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
    waiting, changed = store.refresh_scheduling(first["manifest_path"])

    assert changed is True
    assert waiting["status"] == "WAITING"
    assert waiting["waiting_code"] == "team_busy"
    assert waiting["waiting"]["reason"] == "team_busy"
    assert waiting["waiting"]["blocked_by_task_id"] == second["task_id"]
    owners = [
        task["task_id"]
        for task in store.discover_with_errors()[0]
        if task["team"] == "alpha" and is_active_team_owner(task)
    ]
    assert owners == [second["task_id"]]


def test_mixed_dependency_and_queue_waiters_release_one_deterministic_owner(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("parent", requested_team="parent", task_id="task-parent")
    first = store.create_task(
        "first",
        requested_team="alpha",
        task_id="task-first",
        depends_on_task_ids=(parent["task_id"],),
    )
    second = store.create_task(
        "second",
        reuse_team="alpha",
        task_id="task-second",
        depends_on_task_ids=(parent["task_id"],),
    )
    original_second_hop = json.loads(json.dumps(second["hops"][0]))
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

    later, changed = store.refresh_scheduling(second["manifest_path"])
    assert changed is True
    assert later["status"] == "WAITING"
    assert later["waiting"]["blocked_by_task_id"] == first["task_id"]

    winner, changed = store.refresh_scheduling(first["manifest_path"])
    assert changed is True
    assert winner["status"] == "INBOX"

    restarted = TaskStore(config)
    losing = restarted.load(second["manifest_path"])
    assert losing["status"] == "WAITING"
    assert losing["active_hop_id"] == 1
    for field in ("hop_id", "turn", "request_id", "state", "handoff", "ledger_path"):
        assert losing["hops"][0][field] == original_second_hop[field]
    assert losing["hops"][0]["state"] == "pre_send"
    owners = [
        task["task_id"]
        for task in restarted.discover_with_errors()[0]
        if task["team"] == "alpha" and is_active_team_owner(task)
    ]
    assert owners == [first["task_id"]]


def test_exact_team_resume_fails_closed_for_multiple_owners_or_dependency_blocked_queue(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    queued = store.create_task("queued", reuse_team="alpha", task_id="task-queued")
    store.update(
        queued["manifest_path"],
        lambda state: {
            **state,
            "status": "INBOX",
            "kanban_column": "INBOX",
            "active_action": "queued",
            "queue": {**state["queue"], "released_at": utc_now()},
            "waiting": {
                "reason": None,
                "waiting_on": [],
                "stopped": [],
                "missing": [],
                "blocked_by_task_id": None,
                "since": None,
            },
        },
    )
    with pytest.raises(ValueError, match="multiple active owners"):
        store.resume_team("alpha")

    blocked_root = tmp_path / "blocked"
    blocked_root.mkdir()
    isolated = TaskStore(
        load_cdpa_config(write_config(blocked_root), repository_root=blocked_root)
    )
    first = isolated.create_task("owner", requested_team="alpha", task_id="task-owner")
    parent = isolated.create_task("parent", requested_team="parent", task_id="task-parent")
    blocked = isolated.create_task(
        "blocked",
        reuse_team="alpha",
        task_id="task-blocked",
        depends_on_task_ids=(parent["task_id"],),
    )
    isolated.update(
        first["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )
    with pytest.raises(ValueError, match="not dependency-ready"):
        isolated.resume_team("alpha")
    assert isolated.load(blocked["manifest_path"])["status"] == "WAITING"


def test_catalog_detects_external_manifest_loss_and_reserves_slot(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("first", requested_team="alpha", task_id="task-a")
    catalog_path = config.plans_root / ".cdpa-catalog.json"
    assert catalog_path.is_file()

    Path(first["manifest_path"]).unlink()

    tasks, errors = store.discover_with_errors()
    assert tasks == []
    assert len(errors) == 1
    assert errors[0]["manifest_path"] == first["manifest_path"]
    assert errors[0]["error"].startswith("MissingManifestError:")

    second = store.create_task("second", requested_team="beta", task_id="task-b")
    assert second["team"] == "beta"
    assert second["team_suffix"] == 1
    assert second["roles"]["PLAN"]["physical_role"] == "beta-plan"


def test_cataloged_task_id_cannot_be_reused_after_manifest_loss(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("first", requested_team="alpha", task_id="task-a")
    Path(first["manifest_path"]).unlink()

    with pytest.raises(ValueError, match="already cataloged"):
        store.create_task("replacement", requested_team="alpha", task_id="task-a")


def test_catalog_status_tracks_manifest_saves_without_becoming_a_manifest(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_task("first", requested_team="alpha", task_id="task-a")
    updated = store.update(
        first["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
        },
    )
    catalog = json.loads((config.plans_root / ".cdpa-catalog.json").read_text())
    entries = list(catalog["entries"].values())
    assert len(entries) == 1
    assert entries[0]["task_id"] == "task-a"
    assert entries[0]["status"] == "DONE"
    assert Path(updated["manifest_path"]) in store.discover_paths()
    assert config.plans_root / ".cdpa-catalog.json" not in store.discover_paths()


def test_maintainers_config_is_dedicated_and_not_a_normal_route(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)

    assert config.maintainers_constructor_path.name == "MAINTAINERS.md"
    assert config.maintainers_constructor_path.is_file()
    assert config.maintenance_timeout_seconds == 300
    assert config.maintenance_refresh_after_seconds == 120
    assert config.maintenance_stable_ms == 1000
    assert config.maintenance_poll_ms == 100
    assert "MAINTAINERS" not in config.roles


def test_optional_maintenance_manifest_state_round_trips(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-maint")
    incident = {
        "incident_id": "maint-1",
        "key": "task-maint|BLOCKED|role_offline|1|PLAN|offline|time",
        "state": "OPEN",
    }
    state["maintenance"] = {
        "active_incident_id": "maint-1",
        "incidents": [incident],
        "last_resolved_at": None,
    }

    saved = store.save(state["manifest_path"], state)

    assert saved["maintenance"]["active_incident_id"] == "maint-1"
    assert store.load(state["manifest_path"])["maintenance"]["incidents"] == [incident]


@pytest.mark.parametrize(
    "maintenance",
    [
        {"active_incident_id": None, "incidents": "bad", "last_resolved_at": None},
        {
            "active_incident_id": "maint-1",
            "incidents": [
                {"incident_id": "maint-1", "key": "a", "state": "OPEN"},
                {"incident_id": "maint-1", "key": "b", "state": "RUNNING"},
            ],
            "last_resolved_at": None,
        },
        {
            "active_incident_id": "maint-missing",
            "incidents": [{"incident_id": "maint-1", "key": "a", "state": "OPEN"}],
            "last_resolved_at": None,
        },
        {
            "active_incident_id": "maint-1",
            "incidents": [{"incident_id": "maint-1", "key": "a", "state": "RESOLVED"}],
            "last_resolved_at": None,
        },
    ],
)
def test_invalid_optional_maintenance_manifest_state_is_rejected(
    tmp_path: Path, maintenance: object
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-invalid-maint")
    state["maintenance"] = maintenance

    with pytest.raises(ValueError, match="maintenance"):
        store.save(state["manifest_path"], state)


def test_global_maintainers_state_is_excluded_from_task_discovery(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    task = store.create_task("Task", requested_team="alpha", task_id="task-real")
    global_state = tmp_path / ".plan" / "maintainers" / "state.json"
    global_state.parent.mkdir(parents=True, exist_ok=True)
    global_state.write_text(
        json.dumps({"version": 1, "physical_role": "MAINTAINERS"}),
        encoding="utf-8",
    )

    assert store.discover_paths() == [Path(task["manifest_path"])]


def test_maintenance_save_merges_only_metadata_and_preserves_newer_task_changes(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    original = store.create_task("Task", requested_team="alpha", task_id="task-maint-merge")
    path = Path(original["manifest_path"])
    stale = store.load(path)
    stale["maintenance"] = {
        "active_incident_id": "maint-1",
        "incidents": [{"incident_id": "maint-1", "key": "snapshot", "state": "OPEN"}],
        "last_resolved_at": None,
    }
    newer = store.request_control(path, "pause", reason="operator pause")

    saved = store.save_maintenance(path, stale)

    assert saved["controls"] == newer["controls"]
    assert saved["maintenance"]["active_incident_id"] == "maint-1"



def test_task_report_mode_defaults_file_and_validates_inline(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)

    file_task = store.create_task(
        "File report", requested_team="alpha", task_id="task-file"
    )
    inline_task = store.create_task(
        "Inline report",
        requested_team="beta",
        task_id="task-inline",
        report_mode="inline",
    )

    assert file_task["options"]["report_mode"] == "file"
    assert inline_task["options"]["report_mode"] == "inline"
    assert store.load(inline_task["manifest_path"])["options"]["report_mode"] == "inline"
    with pytest.raises(ValueError, match="report_mode"):
        store.create_task(
            "Bad report mode",
            requested_team="gamma",
            task_id="task-bad-report-mode",
            report_mode="remote",
        )



@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, "other"])
def test_manifest_rejects_explicit_invalid_persisted_report_mode(
    tmp_path: Path,
    value: object,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Inline report",
        requested_team="alpha",
        task_id="task-invalid-report-mode",
        report_mode="inline",
    )
    manifest = Path(state["manifest_path"])
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["options"]["report_mode"] = value
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="report_mode"):
        store.load(manifest)


@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, "other"])
def test_create_task_rejects_explicit_invalid_report_mode(
    tmp_path: Path,
    value: object,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)

    with pytest.raises(ValueError, match="report_mode"):
        store.create_task(
            "Bad report mode",
            requested_team="alpha",
            task_id="task-bad-report-mode",
            report_mode=value,
        )



def test_legacy_manifest_without_report_mode_defaults_to_file(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Legacy file report",
        requested_team="alpha",
        task_id="task-legacy-report-mode",
    )
    manifest = Path(state["manifest_path"])
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    del raw["options"]["report_mode"]
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    loaded = store.load(manifest)

    assert "report_mode" not in loaded["options"]



def test_inline_first_generation_payload_is_authoritative_for_all_roles():
    repository = Path(__file__).resolve().parents[1]
    config = load_cdpa_config(repository / "cdpa.yaml", repository_root=repository)
    builder = PromptBuilder(config)
    forbidden = (
        "write the PLAN report at the expected path",
        "Write evidence and remaining risks into your own role-turn report",
        "Record exact commands and results in your own report",
    )

    for role in config.roles:
        prompt = builder.build(
            task_title="Inline contract",
            task_id="task-inline-contract",
            team="alpha",
            logical_role=role,
            physical_role=f"alpha-{role.lower()}",
            turn=1,
            allowed_routes=("PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"),
            workspace=str(repository),
            source_physical_role=None,
            handoff="inline contract",
            goal="inline contract",
            constructor_sent_generation=None,
            conversation_generation=0,
            report_mode="inline",
        ).text
        assert "Do not create, edit, or write any role-report file" in prompt
        assert "the worker owns report materialization" in prompt
        assert '"handoff":"INLINE"' in prompt
        assert ".plan/alpha/alpha-" not in prompt
        for phrase in forbidden:
            assert phrase not in prompt


def test_file_mode_response_guide_wording_remains_unchanged():
    repository = Path(__file__).resolve().parents[1]
    config = load_cdpa_config(repository / "cdpa.yaml", repository_root=repository)
    prompt = PromptBuilder(config).build(
        task_title="File contract",
        task_id="task-file-contract",
        team="alpha",
        logical_role="PLAN",
        physical_role="alpha-plan",
        turn=1,
        allowed_routes=("PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"),
        workspace=str(repository),
        source_physical_role=None,
        handoff="file contract",
        goal="file contract",
        constructor_sent_generation=None,
        conversation_generation=0,
        report_mode="file",
    ).text
    assert "Write the complete role report to the exact expected Markdown path" in prompt
    assert '"handoff":".plan/<team>/<physical-role>_turn<N>_<task-id>.md"' in prompt


def test_repository_and_packaged_role_contracts_are_report_mode_neutral():
    repository = Path(__file__).resolve().parents[1]
    forbidden = (
        "write the PLAN report at the expected path",
        "Write evidence and remaining risks into your own role-turn report",
        "Record exact commands and results in your own report",
    )
    for base in (
        repository / "prompts" / "cdpa",
        repository / "src" / "playwright_auto" / "cdpa_defaults" / "prompts" / "cdpa",
    ):
        for role in ("PLAN", "DEV", "TEST", "REVIEW", "AUDIT"):
            constructor = (base / f"{role}.md").read_text(encoding="utf-8")
            for phrase in forbidden:
                assert phrase not in constructor

    agents = (repository / "AGENTS.md").read_text(encoding="utf-8")
    assert "In file report mode" in agents
    assert "In inline report mode" in agents
    assert "the worker materializes" in agents


@pytest.mark.parametrize(
    "validation_error",
    [
        "Permission denied: '/repo/.plan/alpha/alpha-plan_turn1_task-x.md.lock'",
        "Input/output error: '/repo/.plan/alpha/alpha-plan_turn1_task-x.md.abc.tmp'",
        (
            "OSError: [Errno 5] Input/output error: "
            "'/repo/.plan/alpha/alpha-plan_turn1_task-x.md.abc.tmp' -> "
            "'/repo/.plan/alpha/alpha-plan_turn1_task-x.md'"
        ),
    ],
)
def test_inline_repair_redacts_report_path_derivatives(validation_error: str, tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    repair = PromptBuilder(config).repair(
        task_id="task-x",
        team="alpha",
        physical_role="alpha-plan",
        turn=1,
        validation_error=validation_error,
        report_mode="inline",
    )

    assert "alpha-plan_turn1_task-x.md" not in repair
    assert ".md.lock" not in repair
    assert ".md.abc.tmp" not in repair
    assert "Do not create, edit, or write any role-report file" in repair


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

    # Corrupting an existing graph is rejected by the primary manifest validator.
    raw_a = json.loads(Path(a["manifest_path"]).read_text(encoding="utf-8"))
    raw_a["depends_on_task_ids"] = ["task-b"]
    Path(a["manifest_path"]).write_text(json.dumps(raw_a), encoding="utf-8")
    with pytest.raises(ValueError, match="cycle"):
        store.load(a["manifest_path"])


def test_missing_edge_does_not_mask_cycle_before_atomic_update(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    missing_parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    a = store.create_task(
        "A",
        requested_team="a",
        task_id="task-a",
        depends_on_task_ids=("missing-parent",),
    )
    store.create_task(
        "B",
        requested_team="b",
        task_id="task-b",
        depends_on_task_ids=("task-a",),
    )
    Path(missing_parent["manifest_path"]).unlink()
    a_path = Path(a["manifest_path"])
    before_manifest = a_path.read_bytes()
    before_catalog = store.catalog_path.read_bytes()

    with pytest.raises(ValueError, match="cycle"):
        store.update(
            a_path,
            lambda state: {
                **state,
                "depends_on_task_ids": ["missing-parent", "task-b"],
            },
        )

    assert a_path.read_bytes() == before_manifest
    assert store.catalog_path.read_bytes() == before_catalog
    assert store.load(a_path)["depends_on_task_ids"] == ["missing-parent"]


def test_missing_edge_cycle_excludes_every_cycle_participant_from_discovery(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    missing_parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    a = store.create_task(
        "A",
        requested_team="a",
        task_id="task-a",
        depends_on_task_ids=("missing-parent",),
    )
    b = store.create_task(
        "B",
        requested_team="b",
        task_id="task-b",
        depends_on_task_ids=("task-a",),
    )
    Path(missing_parent["manifest_path"]).unlink()
    a_path = Path(a["manifest_path"])
    raw_a = json.loads(a_path.read_text(encoding="utf-8"))
    raw_a["depends_on_task_ids"] = ["missing-parent", "task-b"]
    a_path.write_text(json.dumps(raw_a), encoding="utf-8")

    with pytest.raises(ValueError, match="cycle"):
        store.load(a_path)
    with pytest.raises(ValueError, match="cycle"):
        store.load(b["manifest_path"])

    tasks, errors = store.discover_with_errors()
    assert tasks == []
    cycle_errors = {
        Path(item["manifest_path"]).resolve(): item["error"]
        for item in errors
        if "cycle" in item["error"]
    }
    assert set(cycle_errors) == {
        a_path.resolve(),
        Path(b["manifest_path"]).resolve(),
    }
    assert all("cycle" in error for error in cycle_errors.values())


def test_three_node_cycle_excludes_only_its_dependency_closure(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    a = store.create_task("A", requested_team="a", task_id="task-a")
    b = store.create_task(
        "B",
        requested_team="b",
        task_id="task-b",
        depends_on_task_ids=("task-a",),
    )
    c = store.create_task(
        "C",
        requested_team="c",
        task_id="task-c",
        depends_on_task_ids=("task-b",),
    )
    unrelated = store.create_task(
        "Unrelated", requested_team="unrelated", task_id="unrelated-task"
    )
    a_path = Path(a["manifest_path"])
    raw_a = json.loads(a_path.read_text(encoding="utf-8"))
    raw_a["depends_on_task_ids"] = ["task-c"]
    a_path.write_text(json.dumps(raw_a), encoding="utf-8")

    for state in (a, b, c):
        with pytest.raises(ValueError, match="cycle"):
            store.load(state["manifest_path"])
    assert store.load(unrelated["manifest_path"])["task_id"] == "unrelated-task"

    tasks, errors = store.discover_with_errors()
    assert [task["task_id"] for task in tasks] == ["unrelated-task"]
    assert sum("cycle" in item["error"] for item in errors) == 3


def test_unrelated_tasks_remain_operable_with_diagnostic_cycle(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    states = install_cycle_isolation_graph(store)
    cycle_paths = [Path(states[key]["manifest_path"]) for key in ("a", "b")]
    cycle_bytes = {path: path.read_bytes() for path in cycle_paths}
    catalog_before = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    cycle_catalog = {
        store._catalog_key(path): catalog_before["entries"][store._catalog_key(path)]
        for path in cycle_paths
    }

    for key in ("a", "b"):
        with pytest.raises(ValueError, match="cycle"):
            store.load(states[key]["manifest_path"])
    assert store.load(states["unrelated"]["manifest_path"])["task_id"] == "unrelated-task"
    assert store.load(states["missing_only"]["manifest_path"])["depends_on_task_ids"] == [
        "missing-parent"
    ]

    tasks, errors = store.discover_with_errors()
    assert {task["task_id"] for task in tasks} == {"missing-only", "unrelated-task"}
    assert {
        Path(item["manifest_path"]).resolve()
        for item in errors
        if "cycle" in item["error"]
    } == {path.resolve() for path in cycle_paths}

    created = store.create_task(
        "Later unrelated", requested_team="later", task_id="later-unrelated"
    )
    assert created["task_id"] == "later-unrelated"
    for path, data in cycle_bytes.items():
        assert path.read_bytes() == data
    catalog_after_create = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    for key, entry in cycle_catalog.items():
        assert catalog_after_create["entries"][key] == entry

    before_paths = store.discover_paths()
    before_catalog = store.catalog_path.read_bytes()
    with pytest.raises(ValueError, match="missing dependency"):
        store.create_task(
            "Invalid cycle dependent",
            requested_team="invalid-cycle-dependent",
            task_id="invalid-cycle-dependent",
            depends_on_task_ids=("task-a",),
        )
    assert store.discover_paths() == before_paths
    assert store.catalog_path.read_bytes() == before_catalog


def test_manifest_rejects_invalid_dependency_field_shapes(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("Task", requested_team="alpha", task_id="task-a")
    manifest = Path(state["manifest_path"])
    base = json.loads(manifest.read_text(encoding="utf-8"))
    cases = [
        ("depends_on_task_ids", "task-x"),
        ("depends_on_task_ids", [""]),
        ("depends_on_task_ids", ["task-x", "task-x"]),
        ("replaces_task_id", 3),
        ("replacement_incident_id", []),
        ("dependency_events", {}),
        ("waiting", []),
    ]
    for field, value in cases:
        raw = json.loads(json.dumps(base))
        raw[field] = value
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError):
            store.load(manifest)
    manifest.write_text(json.dumps(base), encoding="utf-8")


def test_duplicate_task_ids_fail_closed_without_poisoning_unrelated_tasks(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    states = install_duplicate_task_graph(store)

    for key in ("alpha", "beta"):
        with pytest.raises(ValueError, match="duplicate task ID 'dup-task'"):
            store.load(states[key]["manifest_path"])
    assert store.load(states["unique"]["manifest_path"])["task_id"] == "unique-task"
    with pytest.raises(ValueError, match="ambiguous dependency task.*dup-task"):
        store.load(states["child"]["manifest_path"])

    tasks, errors = store.discover_with_errors()
    assert [(task["team"], task["task_id"]) for task in tasks] == [
        ("unique", "unique-task")
    ]
    by_path = {item["manifest_path"]: item["error"] for item in errors}
    assert "duplicate task ID 'dup-task'" in by_path[states["alpha"]["manifest_path"]]
    assert "duplicate task ID 'dup-task'" in by_path[states["beta"]["manifest_path"]]
    assert "ambiguous dependency task" in by_path[states["child"]["manifest_path"]]


def test_unrelated_creation_survives_duplicate_identity_diagnostics(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    install_duplicate_task_graph(store)

    created = store.create_task(
        "Unrelated after duplicate identity corruption",
        requested_team="gamma",
        task_id="unrelated-after-duplicate",
    )

    assert created["team"] == "gamma"
    assert store.load(created["manifest_path"])["task_id"] == "unrelated-after-duplicate"
    tasks, errors = store.discover_with_errors()
    assert {task["task_id"] for task in tasks} == {
        "unique-task",
        "unrelated-after-duplicate",
    }
    assert len(errors) == 3


def test_dependency_creation_rejects_ambiguous_duplicate_parent_without_write(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    install_duplicate_task_graph(store)
    before_paths = store.discover_paths()
    before_catalog = store.catalog_path.read_bytes()

    with pytest.raises(ValueError, match="ambiguous dependency task.*dup-task"):
        store.create_task(
            "Depends on ambiguous parent",
            requested_team="gamma",
            task_id="ambiguous-child-create",
            depends_on_task_ids=("dup-task",),
        )

    assert store.discover_paths() == before_paths
    assert store.catalog_path.read_bytes() == before_catalog


def test_existing_missing_dependency_does_not_poison_unrelated_creation(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    child = store.create_task(
        "Missing child",
        requested_team="missing-child",
        task_id="missing-child",
        depends_on_task_ids=("missing-parent",),
    )
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    child_before = child_path.read_bytes()
    parent_path.unlink()
    catalog_before = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    child_key = store._catalog_key(child_path)
    child_catalog_before = catalog_before["entries"][child_key]

    created = store.create_task(
        "Unrelated valid task",
        requested_team="unrelated",
        task_id="unrelated-after-missing",
    )

    assert created["task_id"] == "unrelated-after-missing"
    assert store.load(child_path)["depends_on_task_ids"] == ["missing-parent"]
    assert child_path.read_bytes() == child_before
    catalog_after = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert catalog_after["entries"][child_key] == child_catalog_before

    before_paths = store.discover_paths()
    before_catalog = store.catalog_path.read_bytes()
    with pytest.raises(ValueError, match="missing dependency"):
        store.create_task(
            "New invalid missing child",
            requested_team="invalid-missing",
            task_id="new-invalid-missing",
            depends_on_task_ids=("missing-parent",),
        )
    assert store.discover_paths() == before_paths
    assert store.catalog_path.read_bytes() == before_catalog


def test_existing_missing_dependency_does_not_poison_unrelated_replacement(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    missing_parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    missing_child = store.create_task(
        "Missing child",
        requested_team="missing-child",
        task_id="missing-child",
        depends_on_task_ids=("missing-parent",),
    )
    target = store.create_task(
        "Recovery target", requested_team="recovery-target", task_id="recovery-target"
    )
    target_child = store.create_task(
        "Recovery child",
        requested_team="recovery-child",
        task_id="recovery-child",
        depends_on_task_ids=("recovery-target",),
    )
    target = _stop_task(store, target)
    missing_child_path = Path(missing_child["manifest_path"])
    target_path = Path(target["manifest_path"])
    target_child_path = Path(target_child["manifest_path"])
    missing_child_before = missing_child_path.read_bytes()
    target_before = target_path.read_bytes()
    Path(missing_parent["manifest_path"]).unlink()
    catalog_before = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    missing_child_key = store._catalog_key(missing_child_path)
    missing_child_catalog_before = catalog_before["entries"][missing_child_key]

    result = store.replace_task_and_rewire(
        "recovery-target",
        "Continue recovery target safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-missing-isolation",
    )
    replacement = result["replacement"]

    assert target_path.read_bytes() == target_before
    assert store.load(target_child_path)["depends_on_task_ids"] == [replacement["task_id"]]
    assert missing_child_path.read_bytes() == missing_child_before
    assert store.load(missing_child_path)["depends_on_task_ids"] == ["missing-parent"]
    catalog_after = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert catalog_after["entries"][missing_child_key] == missing_child_catalog_before


def test_replacement_does_not_reconcile_unrelated_catalog_invalid_owner(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("owner", requested_team="alpha", task_id="task-owner")
    target = store.create_task(
        "Target", requested_team="beta", task_id="task-target"
    )
    target = _stop_task(store, target)
    owner_key = store._catalog_key(owner["manifest_path"])
    target_before = Path(target["manifest_path"]).read_bytes()
    poisoned = poison_catalog_identity_entry(store, owner)
    before_catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))

    result = store.replace_task_and_rewire(
        target["task_id"],
        "Continue target safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-unrelated-catalog-invalid",
    )

    replacement = result["replacement"]
    tasks, _errors = store.discover_with_errors()
    assert {task["task_id"] for task in tasks} == {
        target["task_id"],
        replacement["task_id"],
    }
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    assert owner_key not in catalog["entries"]
    assert catalog["entries"]["bad"] == poisoned
    assert set(catalog["entries"]) == {
        *before_catalog["entries"],
        store._catalog_key(replacement["manifest_path"]),
    }
    assert Path(target["manifest_path"]).read_bytes() == target_before


def test_replacement_rejects_catalog_invalid_target_without_writes(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    target = store.create_task(
        "Target", requested_team="alpha", task_id="task-target"
    )
    target = _stop_task(store, target)
    target_before = Path(target["manifest_path"]).read_bytes()
    poison_catalog_identity_entry(store, target)
    catalog_before = store.catalog_path.read_bytes()

    with pytest.raises(ValueError, match="replacement target does not exist"):
        store.replace_task_and_rewire(
            target["task_id"],
            "Continue target safely",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-invalid-target",
        )

    assert Path(target["manifest_path"]).read_bytes() == target_before
    assert store.catalog_path.read_bytes() == catalog_before
    assert not store.phase4_journal_path.exists()
    assert list(store.root.rglob("*.phase4.tmp")) == []


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


def test_replace_task_rejects_unsafe_target_and_rolls_back_install_failure(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child", requested_team="child", task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    with pytest.raises(ValueError, match="STOPPED or BLOCKED"):
        store.replace_task_and_rewire(
            "task-parent", "Replacement", reuse_team=True,
            rewire_children=True, incident_id="maint-unsafe",
        )

    parent = _stop_task(store, parent)
    before_parent = Path(parent["manifest_path"]).read_bytes()
    before_child = Path(child["manifest_path"]).read_bytes()
    before_catalog = store.catalog_path.read_bytes()
    original_replace = store_module.os.replace
    calls = 0

    def fail_second_install(source, target):
        nonlocal calls
        if str(source).endswith(".phase4.tmp"):
            calls += 1
            if calls == 2:
                raise OSError("injected phase4 install failure")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", fail_second_install)
    with pytest.raises(OSError, match="injected"):
        store.replace_task_and_rewire(
            "task-parent", "Replacement", reuse_team=True,
            rewire_children=True, incident_id="maint-rollback",
        )

    assert Path(parent["manifest_path"]).read_bytes() == before_parent
    assert Path(child["manifest_path"]).read_bytes() == before_child
    assert store.catalog_path.read_bytes() == before_catalog
    replacements = [
        state for state in store.discover()
        if state.get("replacement_incident_id") == "maint-rollback"
    ]
    assert replacements == []


def test_replace_task_rolls_back_manifest_and_catalog_install_failure(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child", requested_team="child", task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    unrelated = store.create_task(
        "Unrelated", requested_team="other", task_id="task-other"
    )
    parent = _stop_task(store, parent)
    before = {
        Path(parent["manifest_path"]): Path(parent["manifest_path"]).read_bytes(),
        Path(child["manifest_path"]): Path(child["manifest_path"]).read_bytes(),
        Path(unrelated["manifest_path"]): Path(unrelated["manifest_path"]).read_bytes(),
        store.catalog_path: store.catalog_path.read_bytes(),
    }
    original_replace = store_module.os.replace
    failed = False

    def fail_catalog_install(source, target):
        nonlocal failed
        if Path(target).resolve() == store.catalog_path.resolve() and not failed:
            failed = True
            raise OSError("injected catalog install failure")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", fail_catalog_install)
    with pytest.raises(OSError, match="catalog install"):
        store.replace_task_and_rewire(
            "task-parent",
            "Replacement",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-catalog-rollback",
        )

    assert failed is True
    for path, data in before.items():
        assert path.read_bytes() == data
    assert not any(
        task.get("replacement_incident_id") == "maint-catalog-rollback"
        for task in store.discover()
    )


def test_replace_task_preserves_concurrent_child_update_and_catalog_exactness(
    tmp_path: Path, monkeypatch
):
    import threading

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _stop_task(store, parent)
    child_path = Path(child["manifest_path"])
    replacement_building = threading.Event()
    allow_replacement = threading.Event()
    updater_mutating = threading.Event()
    original_initial = store._initial_task_state

    def pause_replacement_build(**kwargs):
        if kwargs.get("normalized_replaces") == "task-parent":
            replacement_building.set()
            assert allow_replacement.wait(5)
        return original_initial(**kwargs)

    monkeypatch.setattr(store, "_initial_task_state", pause_replacement_build)
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def replace():
        try:
            results["replacement"] = store.replace_task_and_rewire(
                "task-parent",
                "Continue parent safely",
                reuse_team=True,
                rewire_children=True,
                incident_id="maint-concurrent",
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    marker = {
        "at": "2026-07-23T06:10:00+00:00",
        "error": "concurrent child update must survive",
    }

    def update_child():
        try:
            def mutate(state):
                updater_mutating.set()
                state.setdefault("errors", []).append(marker)
                return state

            results["updated"] = store.update(child_path, mutate)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    replacement_thread = threading.Thread(target=replace)
    replacement_thread.start()
    assert replacement_building.wait(5)
    updater_thread = threading.Thread(target=update_child)
    updater_thread.start()
    try:
        # The replacement must already own the child manifest lock before it
        # builds any rewired state. The updater therefore cannot enter its
        # mutator until the graph transaction releases that lock.
        assert updater_mutating.wait(0.25) is False
    finally:
        allow_replacement.set()
    replacement_thread.join(5)
    updater_thread.join(5)
    assert not replacement_thread.is_alive()
    assert not updater_thread.is_alive()
    assert errors == []

    replacement = results["replacement"]["replacement"]
    current = store.load(child_path)
    assert marker in current["errors"]
    assert current["depends_on_task_ids"] == [replacement["task_id"]]
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    key = store._catalog_key(child_path)
    assert catalog["entries"][key] == store._catalog_entry(current)


def test_replace_task_staging_failure_cleans_all_phase4_temporaries(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _stop_task(store, parent)
    before = {
        Path(parent["manifest_path"]): Path(parent["manifest_path"]).read_bytes(),
        Path(child["manifest_path"]): Path(child["manifest_path"]).read_bytes(),
        store.catalog_path: store.catalog_path.read_bytes(),
    }
    original_open = Path.open
    phase4_opens = 0

    def fail_second_manifest_stage(path, *args, **kwargs):
        nonlocal phase4_opens
        if str(path).endswith(".json.phase4.tmp") and path != store.catalog_path.with_suffix(
            store.catalog_path.suffix + ".phase4.tmp"
        ):
            phase4_opens += 1
            if phase4_opens == 2:
                raise OSError("injected second stage failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second_manifest_stage)
    with pytest.raises(OSError, match="second stage"):
        store.replace_task_and_rewire(
            "task-parent",
            "Replacement",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-stage-cleanup",
        )

    assert phase4_opens == 2
    for path, data in before.items():
        assert path.read_bytes() == data
    assert list(store.root.rglob("*.phase4.tmp")) == []
    assert list(store.root.rglob("*.phase4.rollback.tmp")) == []
    assert {
        item.name for item in (store.root / "parent").iterdir() if item.is_dir()
    } == {"task-parent"}
    assert not any(
        task.get("replacement_incident_id") == "maint-stage-cleanup"
        for task in store.discover()
    )


def test_replace_task_catalog_stage_failure_is_preinstall_and_cleans_temporaries(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _stop_task(store, parent)
    before = {
        Path(parent["manifest_path"]): Path(parent["manifest_path"]).read_bytes(),
        Path(child["manifest_path"]): Path(child["manifest_path"]).read_bytes(),
        store.catalog_path: store.catalog_path.read_bytes(),
    }
    catalog_stage = store.catalog_path.with_suffix(
        store.catalog_path.suffix + ".phase4.tmp"
    )
    original_open = Path.open

    def fail_catalog_stage(path, *args, **kwargs):
        if Path(path).resolve() == catalog_stage.resolve():
            raise OSError("injected catalog stage failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_catalog_stage)
    with pytest.raises(OSError, match="catalog stage"):
        store.replace_task_and_rewire(
            "task-parent",
            "Replacement",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-catalog-stage",
        )

    for path, data in before.items():
        assert path.read_bytes() == data
    assert list(store.root.rglob("*.phase4.tmp")) == []
    assert list(store.root.rglob("*.phase4.rollback.tmp")) == []
    assert {
        item.name for item in (store.root / "parent").iterdir() if item.is_dir()
    } == {"task-parent"}
    assert not any(
        task.get("replacement_incident_id") == "maint-catalog-stage"
        for task in store.discover()
    )


@pytest.mark.parametrize(
    ("parent_team", "child_team"),
    (("aaa", "zzz"), ("zzz", "aaa")),
    ids=("replacement-first", "child-first"),
)
def test_replace_task_recovers_after_process_interruption_between_manifest_installs(
    tmp_path: Path,
    monkeypatch,
    parent_team: str,
    child_team: str,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team=parent_team, task_id="task-parent"
    )
    child = store.create_task(
        "Child",
        requested_team=child_team,
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _stop_task(store, parent)
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    parent_before = parent_path.read_bytes()
    original_replace = store_module.os.replace
    manifest_installs = 0
    install_targets: list[Path] = []

    def interrupt_before_second_manifest(source, target):
        nonlocal manifest_installs
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name.endswith(".json.phase4.tmp"):
            manifest_installs += 1
            install_targets.append(target_path)
            if manifest_installs == 2:
                raise SystemExit("simulated worker interruption")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", interrupt_before_second_manifest)
    with pytest.raises(SystemExit, match="worker interruption"):
        store.replace_task_and_rewire(
            "task-parent",
            "Continue parent safely",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-crash-recovery",
        )
    assert manifest_installs == 2
    assert parent_path.read_bytes() == parent_before

    monkeypatch.setattr(store_module.os, "replace", original_replace)
    restarted = TaskStore(config)
    recovered = restarted.replace_task_and_rewire(
        "task-parent",
        "Continue parent safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-crash-recovery",
    )
    replacement = recovered["replacement"]
    current_child = restarted.load(child_path)
    tasks = restarted.discover()

    assert parent_path.read_bytes() == parent_before
    assert len(
        [
            task
            for task in tasks
            if task.get("replacement_incident_id") == "maint-crash-recovery"
        ]
    ) == 1
    assert replacement["replaces_task_id"] == "task-parent"
    assert current_child["depends_on_task_ids"] == [replacement["task_id"]]
    assert current_child["depends_on_task_ids"].count(replacement["task_id"]) == 1
    assert all(
        replacement["task_id"] not in task.get("depends_on_task_ids", [])
        or any(item["task_id"] == replacement["task_id"] for item in tasks)
        for task in tasks
    )
    catalog = json.loads(restarted.catalog_path.read_text(encoding="utf-8"))
    for task in tasks:
        key = restarted._catalog_key(task["manifest_path"])
        assert catalog["entries"][key] == restarted._catalog_entry(task)
    assert not restarted.phase4_journal_path.exists()
    assert list(restarted.root.rglob("*.phase4.tmp")) == []
    assert list(restarted.root.rglob("*.phase4.rollback.tmp")) == []
    assert install_targets[0] != install_targets[1]


def test_phase4_recovery_does_not_reconcile_unrelated_catalog_invalid_owner(
    tmp_path: Path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    owner = store.create_task("Owner", requested_team="alpha", task_id="task-owner")
    target = store.create_task(
        "Recovery target", requested_team="beta", task_id="task-target"
    )
    child = store.create_task(
        "Recovery child",
        requested_team="gamma",
        task_id="task-child",
        depends_on_task_ids=(target["task_id"],),
    )
    target = _stop_task(store, target)
    owner_key = store._catalog_key(owner["manifest_path"])
    owner_before = Path(owner["manifest_path"]).read_bytes()
    poisoned = poison_catalog_identity_entry(store, owner)
    original_replace = store_module.os.replace
    interrupted = False

    def interrupt_first_manifest(source, destination):
        nonlocal interrupted
        if Path(source).name.endswith(".json.phase4.tmp") and not interrupted:
            interrupted = True
            raise SystemExit("simulated filtered recovery interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(store_module.os, "replace", interrupt_first_manifest)
    with pytest.raises(SystemExit, match="filtered recovery interruption"):
        store.replace_task_and_rewire(
            target["task_id"],
            "Continue target safely",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-filtered-recovery",
        )
    assert store.phase4_journal_path.exists()

    monkeypatch.setattr(store_module.os, "replace", original_replace)
    restarted = TaskStore(config)
    recovered = restarted.recover_phase4_replacement()

    assert recovered is not None
    replacement = recovered["replacement"]
    assert restarted.load(child["manifest_path"])["depends_on_task_ids"] == [
        replacement["task_id"]
    ]
    catalog = json.loads(restarted.catalog_path.read_text(encoding="utf-8"))
    assert owner_key not in catalog["entries"]
    assert catalog["entries"]["bad"] == poisoned
    assert Path(owner["manifest_path"]).read_bytes() == owner_before
    tasks, _errors = restarted.discover_with_errors()
    assert owner["task_id"] not in {task["task_id"] for task in tasks}
    assert not restarted.phase4_journal_path.exists()
    assert list(restarted.root.rglob("*.phase4.tmp")) == []
    assert list(restarted.root.rglob("*.phase4.rollback.tmp")) == []


def test_phase4_recovery_ignores_unrelated_duplicate_diagnostics(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    duplicate_states = install_duplicate_task_graph(store)
    diagnostic_paths = {
        Path(duplicate_states[key]["manifest_path"])
        for key in ("alpha", "beta", "child")
    }
    diagnostic_bytes = {path: path.read_bytes() for path in diagnostic_paths}
    catalog_before = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    diagnostic_catalog = {
        store._catalog_key(path): catalog_before["entries"][store._catalog_key(path)]
        for path in diagnostic_paths
    }

    parent = store.create_task(
        "Recovery target", requested_team="recovery-parent", task_id="recovery-target"
    )
    child = store.create_task(
        "Recovery child",
        requested_team="recovery-child",
        task_id="recovery-child",
        depends_on_task_ids=("recovery-target",),
    )
    parent = _stop_task(store, parent)
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    parent_before = parent_path.read_bytes()
    original_replace = store_module.os.replace
    installs = 0

    def interrupt_before_second_manifest(source, target):
        nonlocal installs
        if Path(source).name.endswith(".json.phase4.tmp"):
            installs += 1
            if installs == 2:
                raise SystemExit("simulated duplicate-diagnostic recovery interruption")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", interrupt_before_second_manifest)
    with pytest.raises(SystemExit, match="duplicate-diagnostic recovery interruption"):
        store.replace_task_and_rewire(
            "recovery-target",
            "Continue recovery target safely",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-duplicate-diagnostic-recovery",
        )
    assert store.phase4_journal_path.exists()
    assert parent_path.read_bytes() == parent_before

    monkeypatch.setattr(store_module.os, "replace", original_replace)
    restarted = TaskStore(config)
    recovered = restarted.recover_phase4_replacement()
    assert recovered is not None
    replacement = recovered["replacement"]
    current_child = restarted.load(child_path)

    assert replacement["replaces_task_id"] == "recovery-target"
    assert replacement["replacement_incident_id"] == "maint-duplicate-diagnostic-recovery"
    assert current_child["depends_on_task_ids"] == [replacement["task_id"]]
    assert current_child["depends_on_task_ids"].count(replacement["task_id"]) == 1
    assert parent_path.read_bytes() == parent_before
    for path, data in diagnostic_bytes.items():
        assert path.read_bytes() == data
    catalog_after = json.loads(restarted.catalog_path.read_text(encoding="utf-8"))
    for key, entry in diagnostic_catalog.items():
        assert catalog_after["entries"][key] == entry
    for task in restarted.discover():
        key = restarted._catalog_key(task["manifest_path"])
        assert catalog_after["entries"][key] == restarted._catalog_entry(task)
    assert not restarted.phase4_journal_path.exists()
    assert list(restarted.root.rglob("*.phase4.tmp")) == []
    assert list(restarted.root.rglob("*.phase4.rollback.tmp")) == []

    later = restarted.create_task(
        "Unrelated after recovered journal",
        requested_team="later",
        task_id="later-after-recovery",
    )
    updated = restarted.update(
        later["manifest_path"],
        lambda state: {**state, "active_action": "verified_after_recovery"},
    )
    assert updated["active_action"] == "verified_after_recovery"


def test_phase4_recovery_ignores_unrelated_missing_dependency(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    missing_parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    missing_child = store.create_task(
        "Missing child",
        requested_team="missing-child",
        task_id="missing-child",
        depends_on_task_ids=("missing-parent",),
    )
    target = store.create_task(
        "Recovery target", requested_team="recovery-target", task_id="recovery-target"
    )
    target_child = store.create_task(
        "Recovery child",
        requested_team="recovery-child",
        task_id="recovery-child",
        depends_on_task_ids=("recovery-target",),
    )
    target = _stop_task(store, target)
    missing_parent_path = Path(missing_parent["manifest_path"])
    missing_child_path = Path(missing_child["manifest_path"])
    target_path = Path(target["manifest_path"])
    target_child_path = Path(target_child["manifest_path"])
    missing_child_before = missing_child_path.read_bytes()
    target_before = target_path.read_bytes()
    original_replace = store_module.os.replace
    installs = 0

    def interrupt_before_second_manifest(source, target_path_value):
        nonlocal installs
        if Path(source).name.endswith(".json.phase4.tmp"):
            installs += 1
            if installs == 2:
                raise SystemExit("simulated missing-dependency recovery interruption")
        return original_replace(source, target_path_value)

    monkeypatch.setattr(store_module.os, "replace", interrupt_before_second_manifest)
    with pytest.raises(SystemExit, match="missing-dependency recovery interruption"):
        store.replace_task_and_rewire(
            "recovery-target",
            "Continue recovery target safely",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-missing-recovery",
        )
    assert store.phase4_journal_path.exists()
    missing_parent_path.unlink()
    monkeypatch.setattr(store_module.os, "replace", original_replace)

    restarted = TaskStore(config)
    recovered = restarted.recover_phase4_replacement()
    assert recovered is not None
    replacement = recovered["replacement"]

    assert target_path.read_bytes() == target_before
    assert restarted.load(target_child_path)["depends_on_task_ids"] == [replacement["task_id"]]
    assert missing_child_path.read_bytes() == missing_child_before
    assert restarted.load(missing_child_path)["depends_on_task_ids"] == ["missing-parent"]
    assert not restarted.phase4_journal_path.exists()
    assert list(restarted.root.rglob("*.phase4.tmp")) == []
    assert list(restarted.root.rglob("*.phase4.rollback.tmp")) == []


def test_phase4_recovery_rejects_tampered_journal_without_deleting_evidence(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team="aaa", task_id="task-parent"
    )
    child = store.create_task(
        "Child",
        requested_team="zzz",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _stop_task(store, parent)
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    original_replace = store_module.os.replace
    installs = 0

    def interrupt(source, target):
        nonlocal installs
        if Path(source).name.endswith(".json.phase4.tmp"):
            installs += 1
            if installs == 2:
                raise SystemExit("simulated interruption")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", interrupt)
    with pytest.raises(SystemExit):
        store.replace_task_and_rewire(
            "task-parent",
            "Continue parent safely",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-tamper",
        )
    monkeypatch.setattr(store_module.os, "replace", original_replace)
    before = {
        parent_path: parent_path.read_bytes(),
        child_path: child_path.read_bytes(),
        store.catalog_path: store.catalog_path.read_bytes(),
    }
    journal = json.loads(store.phase4_journal_path.read_text(encoding="utf-8"))
    journal["writes"][0]["after_sha256"] = "0" * 64
    store.phase4_journal_path.write_text(
        json.dumps(journal, indent=2, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="after hash"):
        TaskStore(config).recover_phase4_replacement()

    assert store.phase4_journal_path.exists()
    for path, data in before.items():
        assert path.read_bytes() == data


def _pending_phase4_operation(
    tmp_path: Path,
    monkeypatch,
    *,
    parent_team: str = "aaa",
    child_team: str = "zzz",
    incident_id: str = "maint-writer-preflight",
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team=parent_team, task_id="task-parent"
    )
    child = store.create_task(
        "Child",
        requested_team=child_team,
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _stop_task(store, parent)
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    parent_before = parent_path.read_bytes()
    original_replace = store_module.os.replace
    installs = 0

    def interrupt_before_second_manifest(source, target):
        nonlocal installs
        if Path(source).name.endswith(".json.phase4.tmp"):
            installs += 1
            if installs == 2:
                raise SystemExit("simulated pending Phase-4 operation")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", interrupt_before_second_manifest)
    with pytest.raises(SystemExit, match="pending Phase-4"):
        store.replace_task_and_rewire(
            "task-parent",
            "Continue parent safely",
            reuse_team=True,
            rewire_children=True,
            incident_id=incident_id,
        )
    monkeypatch.setattr(store_module.os, "replace", original_replace)
    assert store.phase4_journal_path.exists()
    return config, TaskStore(config), parent_path, child_path, parent_before


@pytest.mark.parametrize(
    ("parent_team", "child_team", "mutation"),
    (
        ("aaa", "zzz", "update"),
        ("zzz", "aaa", "update"),
        ("aaa", "zzz", "request_control"),
        ("zzz", "aaa", "request_control"),
    ),
)
def test_pending_phase4_recovery_precedes_old_parent_mutation(
    tmp_path: Path,
    monkeypatch,
    parent_team: str,
    child_team: str,
    mutation: str,
):
    config, store, parent_path, child_path, parent_before = _pending_phase4_operation(
        tmp_path,
        monkeypatch,
        parent_team=parent_team,
        child_team=child_team,
        incident_id=f"maint-{mutation}-{parent_team}",
    )

    if mutation == "update":
        call = lambda: store.update(
            parent_path,
            lambda state: {
                **state,
                "errors": [*state.get("errors", []), {"at": utc_now(), "error": "late"}],
            },
        )
    else:
        call = lambda: store.request_control(
            parent_path,
            "clear_team",
            reason="dashboard clear during recovery",
            confirmed=True,
        )

    with pytest.raises(ValueError, match="immutable history"):
        call()

    assert parent_path.read_bytes() == parent_before
    current_child = store.load(child_path)
    replacements = [
        task
        for task in store.discover()
        if task.get("replacement_incident_id") == f"maint-{mutation}-{parent_team}"
    ]
    assert len(replacements) == 1
    replacement_id = replacements[0]["task_id"]
    assert current_child["depends_on_task_ids"] == [replacement_id]
    assert not store.phase4_journal_path.exists()
    catalog = json.loads(store.catalog_path.read_text(encoding="utf-8"))
    for task in store.discover():
        assert catalog["entries"][store._catalog_key(task["manifest_path"])] == store._catalog_entry(task)


def test_pending_phase4_recovery_precedes_maintenance_writers_and_preserves_rewire(
    tmp_path: Path, monkeypatch
):
    from playwright_auto.cdpa_maintenance import ensure_maintenance_incident

    config, store, _parent_path, child_path, _parent_before = _pending_phase4_operation(
        tmp_path,
        monkeypatch,
        parent_team="zzz",
        child_team="aaa",
        incident_id="maint-maintenance-writers",
    )
    child = store.load(child_path)
    child.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="send_failed",
        block_reason="child failure",
    )
    incident = ensure_maintenance_incident(child)
    assert incident is not None
    saved = store.save_maintenance(child_path, child)
    first_updated_at = saved["maintenance"]["worker_updated_at"]

    updated = store.update_maintenance(
        child_path,
        lambda current: {
            **current,
            "maintenance": {
                **current["maintenance"],
                "last_resolved_at": utc_now(),
            },
        },
    )

    replacement = next(
        task
        for task in store.discover()
        if task.get("replacement_incident_id") == "maint-maintenance-writers"
    )
    assert updated["depends_on_task_ids"] == [replacement["task_id"]]
    assert updated["maintenance"]["worker_updated_at"] >= first_updated_at
    assert updated["maintenance"]["last_resolved_at"]
    assert not store.phase4_journal_path.exists()


def test_pending_phase4_recovery_rejects_stale_full_save_without_overwriting_rewire(
    tmp_path: Path, monkeypatch
):
    config, store, _parent_path, child_path, _parent_before = _pending_phase4_operation(
        tmp_path,
        monkeypatch,
        incident_id="maint-stale-save",
    )
    stale = json.loads(child_path.read_text(encoding="utf-8"))
    stale["errors"].append({"at": utc_now(), "error": "stale full save"})

    with pytest.raises(ValueError, match="reload before saving"):
        store.save(child_path, stale)

    current = store.load(child_path)
    replacement = next(
        task
        for task in store.discover()
        if task.get("replacement_incident_id") == "maint-stale-save"
    )
    assert current["depends_on_task_ids"] == [replacement["task_id"]]
    assert not any(item.get("error") == "stale full save" for item in current["errors"])
    assert not store.phase4_journal_path.exists()


def test_failed_pending_phase4_preflight_leaves_public_mutation_target_unchanged(
    tmp_path: Path, monkeypatch
):
    config, store, parent_path, child_path, _parent_before = _pending_phase4_operation(
        tmp_path,
        monkeypatch,
        incident_id="maint-preflight-fail",
    )
    journal = json.loads(store.phase4_journal_path.read_text(encoding="utf-8"))
    journal["writes"][0]["after_sha256"] = "0" * 64
    store.phase4_journal_path.write_text(
        json.dumps(journal, indent=2, sort_keys=True), encoding="utf-8"
    )
    before = {
        parent_path: parent_path.read_bytes(),
        child_path: child_path.read_bytes(),
        store.catalog_path: store.catalog_path.read_bytes(),
    }

    with pytest.raises(ValueError, match="after hash"):
        store.update(
            parent_path,
            lambda state: {**state, "errors": [*state["errors"], {"at": utc_now(), "error": "must not persist"}]},
        )

    for path, data in before.items():
        assert path.read_bytes() == data
    assert store.phase4_journal_path.exists()


def test_completed_phase4_replacement_rejects_stale_child_full_save(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task(
        "Parent", requested_team="parent", task_id="task-parent"
    )
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _stop_task(store, parent)
    stale = store.load(child["manifest_path"])
    result = store.replace_task_and_rewire(
        "task-parent",
        "Continue parent safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-completed-stale-save",
    )
    stale["errors"].append(
        {"at": utc_now(), "error": "stale child state must not overwrite rewire"}
    )

    with pytest.raises(ValueError, match="reload before saving"):
        store.save(child["manifest_path"], stale)

    current = store.load(child["manifest_path"])
    assert current["depends_on_task_ids"] == [result["replacement"]["task_id"]]
    assert not any(
        item.get("error") == "stale child state must not overwrite rewire"
        for item in current["errors"]
    )


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


def test_resume_team_rejects_replaced_blocked_parent_without_mutation(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _block_task_for_replacement(store, parent)
    result = store.replace_task_and_rewire(
        "task-parent",
        "Replacement parent",
        reuse_team=False,
        rewire_children=True,
        incident_id="maint-resume-immutable",
    )
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    parent_before = parent_path.read_bytes()
    catalog_before = store.catalog_path.read_bytes()

    with pytest.raises(ValueError, match="immutable history"):
        store.resume_team("parent", reason="must not resume historical parent")

    assert parent_path.read_bytes() == parent_before
    assert store.catalog_path.read_bytes() == catalog_before
    assert store.load(child_path)["depends_on_task_ids"] == [
        result["replacement"]["task_id"]
    ]
    replacements = [
        task
        for task in store.discover()
        if task.get("replacement_incident_id") == "maint-resume-immutable"
    ]
    assert len(replacements) == 1


def test_resume_team_still_works_for_unreplaced_blocked_task(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    task = store.create_task("Blocked", requested_team="alpha", task_id="task-blocked")
    task = _block_task_for_replacement(store, task)

    resumed = store.resume_team("alpha", reason="normal blocked resume")

    assert resumed["task_id"] == "task-blocked"
    assert resumed["controls"][-1]["action"] == "resume"
    assert resumed["controls"][-1]["reason"] == "normal blocked resume"


def test_resume_team_recovers_pending_blocked_replacement_then_rejects_history(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = _block_task_for_replacement(store, parent)
    parent_path = Path(parent["manifest_path"])
    child_path = Path(child["manifest_path"])
    parent_before = parent_path.read_bytes()
    original_replace = store_module.os.replace
    installs = 0

    def interrupt_before_second_manifest(source, target):
        nonlocal installs
        if Path(source).name.endswith(".json.phase4.tmp"):
            installs += 1
            if installs == 2:
                raise SystemExit("pending blocked replacement")
        return original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", interrupt_before_second_manifest)
    with pytest.raises(SystemExit, match="pending blocked replacement"):
        store.replace_task_and_rewire(
            "task-parent",
            "Replacement parent",
            reuse_team=False,
            rewire_children=True,
            incident_id="maint-pending-resume-immutable",
        )
    monkeypatch.setattr(store_module.os, "replace", original_replace)
    restarted = TaskStore(config)

    with pytest.raises(ValueError, match="immutable history"):
        restarted.resume_team("parent", reason="recover then reject")

    replacement = next(
        task
        for task in restarted.discover()
        if task.get("replacement_incident_id") == "maint-pending-resume-immutable"
    )
    assert parent_path.read_bytes() == parent_before
    assert restarted.load(child_path)["depends_on_task_ids"] == [replacement["task_id"]]
    assert not restarted.phase4_journal_path.exists()
    catalog = json.loads(restarted.catalog_path.read_text(encoding="utf-8"))
    for task in restarted.discover():
        assert catalog["entries"][restarted._catalog_key(task["manifest_path"])] == restarted._catalog_entry(task)


def test_replacement_context_bounds_retained_report_references():
    from playwright_auto.cdpa_store import (
        replacement_continuation_text,
        retained_report_references,
    )

    reports = [
        {
            "report_id": index,
            "physical_role": "alpha-plan",
            "turn": index,
            "path": f"/repo/.plan/alpha/report-{index}.md",
            "body": f"secret report body {index}",
        }
        for index in range(25)
    ]
    state = {
        "task_id": "task-parent",
        "task_text": "Original outcome",
        "repository": "/repo",
        "reports": reports,
    }

    references = retained_report_references(state)
    context = replacement_continuation_text(state, "Continue safely")

    assert len(references) == 20
    assert [item["report_id"] for item in references] == list(range(5, 25))
    assert "report-4.md" not in context
    assert "report-5.md" in context
    assert "report-24.md" in context
    assert "secret report body" not in context


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


def test_upload_creation_failure_writes_no_manifest_or_catalog_entry(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)

    with pytest.raises(ValueError, match="missing.txt"):
        store.create_task(
            "Missing upload",
            requested_team="alpha",
            task_id="task-upload-missing",
            upload_paths=[tmp_path / "missing.txt"],
        )

    assert store.discover() == []
    assert not store.catalog_path.exists()


def test_attachment_manifest_validation_is_strict_but_backward_compatible(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    context = tmp_path / "context.txt"
    context.write_text("stable", encoding="utf-8")
    state = store.create_task(
        "Attachment validation",
        requested_team="alpha",
        task_id="task-upload-validation",
        upload_paths=[context],
    )
    path = Path(state["manifest_path"])

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["attachments"][0]["sha256"] = "NOT-A-DIGEST"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="attachment"):
        store.load(path)

    raw = state
    raw.pop("attachments")
    for role in raw["roles"].values():
        role.pop("attachments_uploaded_generation")
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load(path)
    assert "attachments" not in loaded
    assert all(
        "attachments_uploaded_generation" not in role
        for role in loaded["roles"].values()
    )


def test_queued_task_retains_attachment_identity_when_released(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    attachment = tmp_path / "queued-context.txt"
    attachment.write_text("queued", encoding="utf-8")
    owner = store.create_task(
        "Owner",
        requested_team="alpha",
        task_id="task-upload-owner",
    )
    queued = store.create_task(
        "Queued with context",
        reuse_team="alpha",
        task_id="task-upload-queued",
        upload_paths=[attachment],
    )
    expected = queued["attachments"]

    owner = store.update(
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
    released, changed = store.refresh_scheduling(queued["manifest_path"])

    assert owner["status"] == "DONE"
    assert changed is True
    assert released["status"] == "INBOX"
    assert released["attachments"] == expected
    assert released["roles"]["PLAN"]["attachments_uploaded_generation"] is None


def test_phase7_documentation_contract_is_complete():
    repository = Path(__file__).resolve().parents[1]
    readme = (repository / "README.md").read_text(encoding="utf-8")
    agents = (repository / "AGENTS.md").read_text(encoding="utf-8")
    learning = (repository / "LEARNING.md").read_text(encoding="utf-8")

    for example in (
        'cdpa "Build parent" --team alpha',
        'cdpa "Build child" --team beta --depends-on <parent-task-id>',
        'cdpa "Continue with same context" --reuse-team alpha --depends-on <other-task-id>',
        'cdpa "Analyze uploaded sources" --team analysis --inline-report --upload design.md',
        'cdpa --team <exact-existing-team>',
    ):
        assert example in readme

    for invariant in (
        "one global `MAINTAINERS` role for CDP 9222",
        "never a normal route",
        "Only PLAN may mark the task DONE",
        "persist only `depends_on_task_ids`",
        "A STOPPED parent keeps its child WAITING",
        "`--reuse-team` queues work for that exact team",
        "the worker materializes",
        "once per role conversation generation",
        "recover from the exact durable request ledger",
        "raw attachment paths or contents",
    ):
        assert invariant in agents

    assert (
        "Before an irreversible upload/send boundary, validate and use the same immutable byte snapshot. "
        "After the exact request crosses that boundary, recover from persisted durable evidence rather "
        "than rereading mutable source inputs."
    ) in learning

    combined = readme + agents
    assert "--report-back" not in combined
    assert "--report-to" not in combined
