from __future__ import annotations

import json
from pathlib import Path

import pytest

from playwright_auto.hcuw_runtime.audit import AuditTimeline
from playwright_auto.hcuw_runtime.config import load_runtime_config
from playwright_auto.hcuw_runtime.state import RuntimeStateStore
from playwright_auto.hcuw_runtime.task_runner import RuntimeTaskRunner, RuntimeTaskRunnerError


def make_runner(tmp_path: Path) -> RuntimeTaskRunner:
    config = load_runtime_config(repository_root=tmp_path)
    store = RuntimeStateStore(config)
    store.initialize()
    return RuntimeTaskRunner(config, store, AuditTimeline())


def test_task_runner_requires_task_text(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    with pytest.raises(RuntimeTaskRunnerError) as error:
        runner.submit_task({"task": ""})

    assert error.value.reason == "invalid_task_request"


def test_task_runner_rejects_solo_fields(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    with pytest.raises(RuntimeTaskRunnerError) as error:
        runner.submit_task({"task": "x", "solo_role": "PLAN"})

    assert error.value.reason == "invalid_task_request"
    assert "Team Task only" in error.value.message


def test_task_runner_accepts_explicit_roles_and_start_role(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)
    request = runner._validate_payload(
        {
            "task": "review repo",
            "team": "hcuw-v2-test",
            "browser": "9222",
            "roles": ["REVIEW", "DEV"],
            "start_role": "REVIEW",
        }
    )

    assert request.team == "hcuw-v2-test"
    assert request.browser == "9222"
    assert request.roles == ("REVIEW", "DEV")
    assert request.start_role == "REVIEW"
    assert request.task == "review repo"


def test_task_runner_defaults_start_role_to_first_canonical_role(tmp_path: Path) -> None:
    request = make_runner(tmp_path)._validate_payload({"task": "x", "roles": ["REVIEW", "DEV"]})

    assert request.start_role == "REVIEW"


def test_t009_task_runner_accepts_unregistered_valid_custom_role(tmp_path: Path) -> None:
    request = make_runner(tmp_path)._validate_payload(
        {
            "task": "custom role",
            "roles": ["custom-review"],
            "route_targets": ["custom-dev"],
        }
    )

    assert request.roles == ("CUSTOM_REVIEW",)
    assert request.start_role == "CUSTOM_REVIEW"
    assert request.closing_role == "CUSTOM_REVIEW"
    assert request.policy.reachable_roles == ("CUSTOM_REVIEW", "CUSTOM_DEV")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"task": "x"}, "roles must be a non-empty array"),
        ({"task": "x", "roles": "DEV,REVIEW"}, "roles must be an array"),
        ({"task": "x", "roles": ["DEV", "REVIEW"], "start_role": "PLAN"}, "start_role must belong"),
        ({"task": "x", "roles": ["REVIEW"], "workflow_mode": "solo"}, "Team Task only"),
        ({"task": "x", "roles": ["REVIEW"], "role": "REVIEW"}, "Team Task only"),
        ({"task": "x", "roles": ["REVIEW"], "conversation_url": "https://chatgpt.com/c/1"}, "Team Task only"),
        ({"task": "x", "roles": ["REVIEW"], "independent": True}, "Team Task only"),
    ],
)
def test_task_runner_rejects_invalid_requests_before_legacy_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict, message: str
) -> None:
    runner = make_runner(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid request must not reach legacy CDPA")

    monkeypatch.setattr(RuntimeTaskRunner, "ensure_legacy_runtime", forbidden)
    monkeypatch.setattr(RuntimeTaskRunner, "_post_legacy_api", forbidden)
    with pytest.raises(RuntimeTaskRunnerError) as error:
        runner.submit_task(payload)

    assert error.value.reason == "invalid_task_request"
    assert message in error.value.message


@pytest.mark.parametrize(
    "derived_field",
    [
        "task_id",
        "submitted_roles",
        "reachable_roles",
        "route_contract_id",
        "self_route_counts",
        "route_seq",
        "active_role",
        "status",
        "created_at",
        "updated_at",
        "role_slots",
    ],
)
def test_t015_task_runner_rejects_runtime_derived_fields_before_legacy_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, derived_field: str
) -> None:
    runner = make_runner(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("derived fields must not reach legacy CDPA")

    monkeypatch.setattr(RuntimeTaskRunner, "ensure_legacy_runtime", forbidden)
    monkeypatch.setattr(RuntimeTaskRunner, "_post_legacy_api", forbidden)
    payload = {"task": "x", "roles": ["DEV"]}
    payload[derived_field] = {} if derived_field in {"self_route_counts", "role_slots"} else "client-owned"
    with pytest.raises(RuntimeTaskRunnerError) as error:
        runner.submit_task(payload)

    assert error.value.reason == "invalid_task_request"
    assert "runtime-derived TeamTask fields are not client-writable" in error.value.message
    assert derived_field in error.value.message


def test_task_runner_submits_roles_and_start_role_to_legacy_team_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = make_runner(tmp_path)
    posted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        RuntimeTaskRunner, "ensure_legacy_runtime", lambda self: {"status": "already_running"}
    )

    def fake_post(self, path: str, payload: dict) -> dict:
        posted.append((path, payload))
        return {"command_id": "cmd-1", "task_id": "cdpa-1", "status": "queued"}

    monkeypatch.setattr(RuntimeTaskRunner, "_post_legacy_api", fake_post)
    record = runner.submit_task(
        {"task": "verify", "team": "duo", "roles": ["DEV", "REVIEW"], "start_role": "DEV"}
    )

    assert posted[0][0] == "/api/tasks"
    assert posted[0][1]["workflow_mode"] == "team"
    assert posted[0][1]["roles"] == ["DEV", "REVIEW"]
    assert posted[0][1]["start_role"] == "DEV"
    assert posted[0][1]["task"] == "verify"
    assert record["roles"] == ["DEV", "REVIEW"]
    assert record["start_role"] == "DEV"
    assert record["legacy_task_id"] == "cdpa-1"
    assert record["legacy_command_id"] == "cmd-1"
    assert record["updated_at"] == record["created_at"]
    assert runner.state_store.list_task_records() == [record]


def test_ensure_legacy_runtime_never_spawns_duplicate_when_port_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = make_runner(tmp_path)
    health = iter([False, False, True])
    monkeypatch.setattr(RuntimeTaskRunner, "_legacy_api_is_healthy", lambda self: next(health))
    monkeypatch.setattr(RuntimeTaskRunner, "_legacy_api_port_is_bound", lambda self: True)
    monkeypatch.setattr(
        RuntimeTaskRunner,
        "_start_legacy_runtime_process",
        lambda self: pytest.fail("a bound legacy port must not start another runtime"),
    )
    monkeypatch.setattr("playwright_auto.hcuw_runtime.task_runner.time.sleep", lambda _seconds: None)

    assert runner.ensure_legacy_runtime()["status"] == "already_running"


def test_legacy_bridge_config_allows_repository_and_disables_builtin_agents(tmp_path: Path) -> None:
    (tmp_path / "cdpa.yaml").write_text(
        json.dumps({"repositories": {"allowed_roots": ["C:/other"]}, "independent_agents": {"seed_builtins": True}}),
        encoding="utf-8",
    )
    runner = make_runner(tmp_path)

    bridge = json.loads(Path(runner._legacy_config_path()).read_text(encoding="utf-8"))

    assert bridge["repositories"]["allowed_roots"] == ["C:/other", str(runner.config.repository_root)]
    assert bridge["independent_agents"]["seed_builtins"] is False
    assert json.loads((tmp_path / "cdpa.yaml").read_text(encoding="utf-8"))["independent_agents"]["seed_builtins"] is True


def test_state_store_saves_task_records_under_runtime_v2(tmp_path: Path) -> None:
    config = load_runtime_config(repository_root=tmp_path)
    store = RuntimeStateStore(config)
    store.initialize()
    record = {"runtime_task_id": "rtask-test", "status": "submitted"}

    store.save_task_record(record)

    assert store.list_task_records() == [record]
    assert (tmp_path / ".runtime-v2" / "tasks" / "rtask-test.json").exists()
    assert not (tmp_path / ".runtime").exists()
