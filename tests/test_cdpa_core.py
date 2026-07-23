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
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_team import cleanup_eligible, physical_role


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
