from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import CDPA_ROLES, load_cdpa_config
from playwright_auto.cdpa_experience_exporter import (
    export_history,
    iter_experience_records,
)
from playwright_auto.chatgpt import PageBinding
from playwright_auto.durable import DurableRequestRecord, RequestLedger, RequestStatus
from playwright_auto.cdpa_store import TaskStore


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_case(
    root: Path,
    team: str,
    task_id: str,
    *,
    accepted: bool = True,
    status: str = "DONE",
) -> Path:
    team_dir = root / team
    task_dir = team_dir / task_id
    task_dir.mkdir(parents=True)
    dev_report = team_dir / f"{team}-dev_turn1_{task_id}.md"
    test_report = team_dir / f"{team}-test_turn1_{task_id}.md"
    review_report = team_dir / f"{team}-review_turn1_{task_id}.md"
    plan_report = team_dir / f"{team}-plan_turn2_{task_id}.md"
    for path, text in [
        (dev_report, "# DEV\nroot cause\n"),
        (test_report, "# TEST\nPASS\n"),
        (review_report, "# REVIEW\nclean\n"),
        (plan_report, "# DONE\nPASS\n"),
    ]:
        path.write_text(text, encoding="utf-8")

    receipt = {
        "accepted_via": "user_message_identity",
        "attempts": 1,
        "binding": {"page_id": "page-1", "role": f"{team}-dev"},
        "user_message_id": "user-1",
    } if accepted else None
    ledger_path = task_dir / "requests.json"
    report_paths = [dev_report, test_report, review_report, plan_report]
    hop_roles = [("DEV", "PLAN"), ("TEST", "DEV"), ("REVIEW", "TEST"), ("PLAN", "REVIEW")]
    routes = ["TEST", "REVIEW", "PLAN", "DONE"]
    hops = []
    for hop_id, ((target_role, source_role), report_path, route) in enumerate(
        zip(hop_roles, report_paths, routes), start=1
    ):
        hop = {
            "hop_id": hop_id,
            "turn": 2 if target_role == "PLAN" else 1,
            "target_role": target_role,
            "physical_role": f"{team}-{target_role.lower()}",
            "source_role": source_role,
            "request_id": f"{task_id}-hop{hop_id}",
            "prompt": "dev prompt" if target_role == "DEV" else f"{target_role.lower()} prompt",
            "prompt_sha256": hashlib.sha256(("dev prompt" if target_role == "DEV" else f"{target_role.lower()} prompt").encode()).hexdigest(),
            "handoff": f"handoff to {target_role}",
            "ledger_path": str(ledger_path.resolve()),
            "expected_report_path": str(report_path.relative_to(root.parent)).replace("\\", "/"),
            "report_path": str(report_path.relative_to(root.parent)).replace("\\", "/"),
            "report_sha256": _sha(report_path),
            "report_size": report_path.stat().st_size,
            "route": route,
            "state": "routed",
            "wait": {},
            "timestamps": {},
            "errors": [],
        }
        hops.append(hop)
    hops[0].update({
        "response": '{"route":"TEST"}',
        "response_sha256": hashlib.sha256(b'{"route":"TEST"}').hexdigest(),
        "browser_endpoint_id": "endpoint-1",
        "conversation_generation": 2,
        "conversation_url": "https://example.invalid/c/1",
        "receipt": receipt,
        "state": "routed" if accepted else "sending",
        "timestamps": {"sending_at": "2026-09-17T00:00:00Z", "sent_at": "2026-09-17T00:00:01Z" if accepted else None},
        "errors": [{"code": "hop-warning"}] if status == "BLOCKED" else [],
    })
    manifest_path = task_dir / "fixture-task.json"
    manifest = {
        "schema_version": 1,
        "manifest_path": str(manifest_path.resolve()),
        "task_id": task_id,
        "task_title": "Fixture task",
        "task_text": "Fixture task",
        "task_slug": "fixture-task",
        "team_base": team,
        "team_suffix": 1,
        "team": team,
        "status": status,
        "task_mode": "workflow",
        "workspace": str(root.parent),
        "repository": str(root.parent),
        "kanban_column": "History" if status == "DONE" else status.title(),
        "active_action": "complete" if status == "DONE" else status.lower(),
        "created_at": "2026-09-17T00:00:00Z",
        "updated_at": "2026-09-17T01:00:00Z",
        "completed_at": "2026-09-17T01:00:00Z" if status == "DONE" else None,
        "active_role": None if status in {"DONE", "STOPPED"} else "DEV",
        "active_hop_id": None if status in {"DONE", "STOPPED"} else 1,
        "block_code": "recovery_required" if status == "BLOCKED" else None,
        "block_reason": "needs recovery" if status == "BLOCKED" else None,
        "block_retryable": False,
        "roles": {
            role: {
                "logical_role": role,
                "physical_role": f"{team}-{role.lower()}",
                "turn": 1 if role in {"DEV", "TEST", "REVIEW"} else (2 if role == "PLAN" else 0),
                "status": "cleared",
                "online": False,
            }
            for role in CDPA_ROLES
        },
        "reports": [],
        "controls": ([{"action": "pause", "status": "applied"}, {"action": "resume", "status": "failed"}] if status == "BLOCKED" else []),
        "cleanup": {},
        "route_timeline": [],
        "errors": [{"code": "task-error"}] if status == "BLOCKED" else [],
        "options": {"report_mode": "file"},
        "hops": hops,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    durable = DurableRequestRecord(
        request_id=f"{task_id}-hop1",
        idempotency_key="fixture-key",
        role=f"{team}-dev",
        prompt="dev prompt",
        rendered_prompt="dev prompt",
        marker="",
        status=RequestStatus.COMPLETED if accepted else RequestStatus.SENDING,
        created_at=1.0,
        updated_at=2.0,
        accepted_at=1.0 if accepted else None,
        attempts=1,
        source_context={
            "task_id": task_id,
            "team": team,
            "hop_id": 1,
            "manifest": str(manifest_path.resolve()),
        },
        binding=PageBinding(page_id="page-1", role=f"{team}-dev"),
        receipt=receipt,
        error="transport ambiguous" if not accepted else None,
    )
    ledger = {"version": RequestLedger.VERSION, "records": {durable.request_id: durable.to_dict()}}
    ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    return manifest_path


def test_export_is_deterministic_read_only_and_joins_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    plan = repo / ".plan"
    for team in [
        "g6-rootfix-20260917",
        "cdpa-solo-task-mode-20260916",
        "windows-worker-pid-handoff-fix-20260916",
        "cdpa-report-context-integrate-20260915",
    ]:
        _write_case(plan, team, f"{team}-task")
    source_hashes = {p: _sha(p) for p in plan.rglob("*") if p.is_file()}

    records = list(iter_experience_records(repo))
    assert len(records) == 4
    g6 = next(record for record in records if record["task_identity"]["team"] == "g6-rootfix-20260917")
    assert g6["replayability"]["class"] == "DECISION_REPLAY"
    assert g6["transport"]["receipt"]["accepted_via"] == "user_message_identity"
    assert [item["role"] for item in g6["downstream"]["reports"]] == ["TEST", "REVIEW", "PLAN"]
    assert g6["contamination"] == {"family_id": None, "split": "unassigned"}
    assert g6["baseline_identity"]["pre_state_available"] is False
    assert g6["replayability"]["oracle_available"] is False

    out = tmp_path / "out"
    index1 = export_history(repo, out)
    first_bytes = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    index2 = export_history(repo, out)
    second_bytes = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert index1 == index2
    assert first_bytes == second_bytes
    assert source_hashes == {p: _sha(p) for p in plan.rglob("*") if p.is_file()}


def test_fail_closed_replayability_split_baseline_and_ambiguous_operations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    plan = repo / ".plan"
    _write_case(plan, "blocked-family", "task-1", accepted=False, status="BLOCKED")

    records = list(iter_experience_records(
        repo,
        split_manifest={"task-1": {"family_id": "family-a", "split": "heldout"}},
        source_identities={"task-1": {"requested_baseline": "abc", "observed_head": "abc", "dirty_state": "clean"}},
        baseline_known_failures={"abc": ["known-red-1"]},
    ))
    record = records[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    assert record["replayability"]["pre_state_available"] is False
    assert record["replayability"]["oracle_available"] is False
    assert record["contamination"] == {"family_id": "family-a", "split": "heldout"}
    assert record["baseline_identity"]["known_failures"] == ["known-red-1"]
    assert record["transport"]["send_state"] == "sending"
    assert record["transport"]["ledger_record"]["error"] == "transport ambiguous"
    assert record["operations"]["block_code"] == "recovery_required"
    assert record["operations"]["controls"]
    assert record["operations"]["pause_events"] == [{"action": "pause", "status": "applied"}]
    assert record["operations"]["errors"]
    assert record["dev_output"]["response"] == '{"route":"TEST"}'

    mismatch = list(iter_experience_records(
        repo,
        source_identities={"task-1": {"requested_baseline": "abc", "observed_head": "def"}},
        baseline_known_failures={"abc": ["known-red-1"]},
    ))[0]
    assert mismatch["baseline_identity"]["known_failures"] == []


def test_full_static_requires_explicit_prestate_and_oracle(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_case(repo / ".plan", "static-family", "task-1", status="PAUSED")
    record = list(iter_experience_records(repo, source_identities={
        "task-1": {
            "requested_baseline": "abc",
            "observed_head": "abc",
            "dirty_state": "clean",
            "pre_state_sha256": "1" * 64,
            "oracle_sha256": "2" * 64,
        }
    }))[0]
    assert record["replayability"]["class"] == "FULL_STATIC"


def test_inventory_ignores_non_manifest_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_case(repo / ".plan", "family", "task-1")
    decoy = repo / ".plan" / "decoy" / "task-2"
    decoy.mkdir(parents=True)
    (decoy / "evidence.json").write_text("[]", encoding="utf-8")

    records = list(iter_experience_records(repo))
    assert [record["task_identity"]["task_id"] for record in records] == ["task-1"]


def test_fixture_matches_repository_canonical_manifest_contract(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    state = json.loads(manifest_path.read_text(encoding="utf-8"))
    store = TaskStore(load_cdpa_config(repository_root=repo))
    assert store._manifest_value_error(manifest_path, state) is None


def test_inventory_rejects_manifest_shaped_decoy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    canonical = _write_case(repo / ".plan", "family", "task-1")
    (canonical.parent / "snapshot.json").write_bytes(canonical.read_bytes())

    decoy_path = _write_case(repo / ".plan", "decoy-team", "task-2")
    decoy = json.loads(decoy_path.read_text(encoding="utf-8"))
    decoy["roles"] = {}
    decoy_path.write_text(json.dumps(decoy, indent=2), encoding="utf-8")

    store = TaskStore(load_cdpa_config(repository_root=repo))
    assert store._manifest_value_error(decoy_path, decoy) is not None
    records = list(iter_experience_records(repo))
    assert [record["task_identity"]["task_id"] for record in records] == ["task-1"]


def test_paused_history_with_downstream_reports_is_qualitative(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_case(repo / ".plan", "paused-family", "task-1", status="PAUSED")
    record = list(iter_experience_records(repo))[0]
    assert [item["role"] for item in record["downstream"]["reports"]] == ["TEST", "REVIEW", "PLAN"]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"


def test_dev_report_drift_downgrades_replay_and_preserves_durable_hash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hop = manifest["hops"][0]
    expected_hash = hop["report_sha256"]
    report_path = repo / hop["report_path"]
    report_path.write_text("# DEV\nmodified after manifest\n", encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"] == {
        "class": "QUALITATIVE_ONLY",
        "pre_state_available": False,
        "oracle_available": False,
        "exclusion_reason": "evidence integrity check failed",
    }
    assert record["dev_output"]["report_sha256"] == expected_hash
    failure = next(item for item in record["provenance"]["integrity_failures"] if item["kind"] == "report")
    assert failure["role"] == "DEV"
    assert failure["expected_sha256"] == expected_hash
    assert failure["observed_sha256"] == _sha(report_path)
    source = next(item for item in record["provenance"]["sources"] if item["path"] == hop["report_path"])
    assert source["sha256"] == _sha(report_path)


def test_stale_prompt_hash_downgrades_replay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hash = manifest["hops"][0]["prompt_sha256"]
    manifest["hops"][0]["prompt"] = "tampered prompt"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    failure = next(item for item in record["provenance"]["integrity_failures"] if item["kind"] == "prompt")
    assert failure["role"] == "DEV"
    assert failure["expected_sha256"] == expected_hash
    assert failure["observed_sha256"] == hashlib.sha256(b"tampered prompt").hexdigest()


def test_stale_response_hash_downgrades_replay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hash = manifest["hops"][0]["response_sha256"]
    manifest["hops"][0]["response"] = '{"route":"REVIEW"}'
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    failure = next(item for item in record["provenance"]["integrity_failures"] if item["kind"] == "response")
    assert failure["role"] == "DEV"
    assert failure["expected_sha256"] == expected_hash
    assert failure["observed_sha256"] == hashlib.sha256(b'{"route":"REVIEW"}').hexdigest()


def test_downstream_report_drift_is_not_accepted_replay_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    test_hop = manifest["hops"][1]
    report_path = repo / test_hop["report_path"]
    report_path.write_text("# TEST\nmodified after manifest\n", encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    assert [item["role"] for item in record["downstream"]["reports"]] == ["REVIEW", "PLAN"]
    failure = next(item for item in record["provenance"]["integrity_failures"] if item["kind"] == "report")
    assert failure["role"] == "TEST"
    assert failure["expected_sha256"] == test_hop["report_sha256"]
    assert failure["observed_sha256"] == _sha(report_path)


def test_external_dev_report_path_is_not_admitted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    external = tmp_path / "not-a-role-report.md"
    external.write_text("# DEV\nexternal evidence\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hop = manifest["hops"][0]
    hop["report_path"] = str(external)
    hop["report_sha256"] = _sha(external)
    hop["report_size"] = external.stat().st_size
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert list(iter_experience_records(repo)) == []


def test_missing_downstream_report_is_not_a_path_integrity_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hop = manifest["hops"][1]
    hop.pop("report_path")
    hop.pop("report_sha256")
    hop.pop("report_size")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "DECISION_REPLAY"
    assert [item["role"] for item in record["downstream"]["reports"]] == ["REVIEW", "PLAN"]
    assert record["provenance"]["integrity_failures"] == []


def test_external_downstream_report_path_downgrades_replay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    external = tmp_path / "not-a-test-report.md"
    external.write_text("# TEST\nexternal evidence\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hop = manifest["hops"][1]
    hop["report_path"] = str(external)
    hop["report_sha256"] = _sha(external)
    hop["report_size"] = external.stat().st_size
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    assert [item["role"] for item in record["downstream"]["reports"]] == ["REVIEW", "PLAN"]
    failure = next(item for item in record["provenance"]["integrity_failures"] if item["kind"] == "report_path")
    assert failure["role"] == "TEST"
    assert "assigned team" in failure["reason"] or "exactly match" in failure["reason"]


def test_dev_report_symlink_is_not_admitted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hop = manifest["hops"][0]
    report_path = repo / hop["report_path"]
    original = report_path.read_bytes()
    target = tmp_path / "symlink-target.md"
    target.write_bytes(original)
    report_path.unlink()
    try:
        report_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")

    assert list(iter_experience_records(repo)) == []


def test_malformed_request_ledger_records_fail_closed_without_crashing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    ledger_path = manifest_path.parent / "requests.json"
    ledger_path.write_text(json.dumps({"version": 1, "records": [{"request_id": "task-1-hop1"}]}), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    assert record["transport"]["ledger_record"] is None
    assert any(item["kind"] == "request_ledger" for item in record["provenance"]["integrity_failures"])


def test_unsupported_request_ledger_version_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    ledger_path = manifest_path.parent / "requests.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["version"] = 999
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    assert record["transport"]["ledger_record"] is None
    assert any("unsupported durable ledger version" in item["reason"] for item in record["provenance"]["integrity_failures"])


def test_malformed_exact_request_record_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    ledger_path = manifest_path.parent / "requests.json"
    ledger_path.write_text(json.dumps({"version": 1, "records": {"task-1-hop1": {"request_id": "task-1-hop1", "status": "completed"}}}), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    assert record["transport"]["ledger_record"] is None
    assert any(item["kind"] == "request_ledger" for item in record["provenance"]["integrity_failures"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "other-request"),
        ("role", "family-test"),
        ("prompt", "different durable prompt"),
        ("rendered_prompt", "different rendered prompt"),
        ("source_context", {"task_id": "other", "team": "other", "hop_id": 999, "manifest": "other.json"}),
    ],
)
def test_schema_valid_request_record_must_match_dev_hop_identity(
    tmp_path: Path, field: str, value: object
) -> None:
    repo = tmp_path / "repo"
    manifest_path = _write_case(repo / ".plan", "family", "task-1")
    ledger_path = manifest_path.parent / "requests.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["records"]["task-1-hop1"][field] = value
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    record = list(iter_experience_records(repo))[0]
    assert record["replayability"]["class"] == "QUALITATIVE_ONLY"
    assert record["transport"]["ledger_record"] is None
    failure = next(
        item
        for item in record["provenance"]["integrity_failures"]
        if item["kind"] == "request_ledger"
    )
    assert "identity does not match DEV hop" in failure["reason"]


def test_export_rejects_descendant_junction_escape(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction regression")
    repo = tmp_path / "repo"
    _write_case(repo / ".plan", "family", "task-1")
    destination = tmp_path / "out"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = destination / "evolution_evidence"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert junction.is_junction()

    try:
        with pytest.raises(ValueError, match="reparse|junction|symlink|destination"):
            export_history(repo, destination)
        assert not (outside / "experience_v1" / "index.json").exists()
    finally:
        if junction.exists():
            os.rmdir(junction)


def test_export_rejects_destination_inside_plan_and_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_case(repo / ".plan", "family", "task-1")
    with pytest.raises(ValueError, match="destination"):
        export_history(repo, repo / ".plan" / "bad")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="symlink|reparse|junction"):
        export_history(repo, link)
