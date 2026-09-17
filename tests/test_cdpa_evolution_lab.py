from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from playwright_auto.cdpa_evolution_lab import (
    EvolutionError,
    LabStore,
    canonical_bytes,
    document_hash,
    gate,
    make_document,
    propose,
    validate_document,
)


def observation(**fact_overrides):
    facts = {
        "send_state": "pre_send",
        "allocation": "fresh",
        "provider_challenge": True,
        "existing_page_ready": True,
        "fresh_page_ready": False,
    }
    facts.update(fact_overrides)
    return make_document({
        "schema_version": 1,
        "observation_id": "incident-0001",
        "created_at": "2026-09-17T00:00:00Z",
        "incident_class": "browser_acquisition",
        "source_identity": {"ancestry_sha": "a" * 40, "dirty": True, "consumed_hashes": {"report": "b" * 64}},
        "evidence": [{"evidence_id": "pre-fix", "kind": "incident_report", "path_label": "incident/report.md", "observed_at": "2026-09-17T00:00:00Z", "sha256": "c" * 64, "facts": facts}],
        "allowed_candidate_surfaces": ["policy_description"],
        "provenance": {"exporter_id": "cdpa-evolution-lab", "exporter_version": "0", "source_report_hashes": ["c" * 64]},
    })


def oracle(obs, accepted_sha):
    return make_document({
        "schema_version": 1,
        "oracle_id": "incident-0001-oracle",
        "observation_sha256": obs["sha256"],
        "expected_diagnosis_predicates": ["fresh_failure", "explicit_challenge", "readiness_contradiction"],
        "expected_candidate_classes": ["typed_acquisition_throttle"],
        "accepted_direction_sha256": accepted_sha,
        "holdouts": [
            {"holdout_id": "non-challenge", "input_facts": {"provider_challenge": False, "failure": "composer_timeout"}, "expected_result": "not_throttle", "invariant_predicates": ["no_generic_timeout_throttle"]},
            {"holdout_id": "accepted-send", "input_facts": {"send_state": "accepted"}, "expected_result": "no_replay", "invariant_predicates": ["accepted_send_non_replay"]},
        ],
        "hard_vetoes": ["reuse_unowned_tab", "replay_accepted_send", "executable_candidate"],
        "provenance": {"evaluator_id": "cdpa-static-gate", "evaluator_version": "0"},
    })


def test_canonical_hash_and_schema_are_strict():
    doc = observation()
    assert canonical_bytes(doc) == json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert document_hash({k: v for k, v in doc.items() if k != "sha256"}) == doc["sha256"]
    bad = dict(doc, surprise=True)
    with pytest.raises(EvolutionError, match="unknown fields"):
        validate_document("observation", bad)
    missing = dict(doc)
    missing.pop("incident_class")
    with pytest.raises(EvolutionError, match="missing fields"):
        validate_document("observation", missing)


def test_observation_rejects_oracle_leakage():
    for key in ("oracle_path", "expected_class", "accepted_direction", "holdout_answers", "verdict"):
        bad = dict(observation(), **{key: "leak"})
        with pytest.raises(EvolutionError):
            validate_document("observation", bad)


def test_proposer_classifies_only_explicit_challenge():
    accepted = propose(observation())
    assert accepted["candidate_class"] == "typed_acquisition_throttle"
    assert accepted["mutable_surface"] == "policy_description"
    assert set(accepted).isdisjoint({"verdict", "score", "command", "script", "patch"})
    generic = propose(observation(provider_challenge=False, existing_page_ready=False))
    assert generic["candidate_class"] == "unclassified_timeout"


def test_gate_passes_direction_and_rejects_negative_controls():
    obs = observation()
    accepted = propose(obs)
    accepted_sha = accepted["sha256"]
    ev = gate(obs, oracle(obs, accepted_sha), accepted)
    assert ev["verdict"] == "PASS"
    timeout = make_document({**{k: v for k, v in accepted.items() if k != "sha256"}, "candidate_id": "timeout-only", "candidate_class": "generic_timeout", "effects": ["increase_timeout"]})
    assert gate(obs, oracle(obs, accepted_sha), timeout)["verdict"] == "REJECT"
    unsafe = make_document({**{k: v for k, v in accepted.items() if k != "sha256"}, "candidate_id": "reuse-unowned", "effects": ["reuse_unowned_tab"]})
    rejected = gate(obs, oracle(obs, accepted_sha), unsafe)
    assert rejected["verdict"] == "REJECT"
    assert "reuse_unowned_tab" in rejected["reasons"]
    overbroad = make_document({**{k: v for k, v in accepted.items() if k != "sha256"}, "candidate_id": "all-timeouts", "candidate_class": "all_timeouts_are_throttle"})
    assert gate(obs, oracle(obs, accepted_sha), overbroad)["verdict"] == "REJECT"


def test_candidate_executable_fields_fail_closed():
    candidate = dict(propose(observation()), command="whoami")
    with pytest.raises(EvolutionError, match="unsupported_executable_candidate"):
        validate_document("candidate", candidate)


def test_store_rejects_plan_root_symlink_and_identity_collision(tmp_path):
    with pytest.raises(EvolutionError, match="plan"):
        LabStore(tmp_path / ".plan" / "lab")
    root = tmp_path / "lab"
    store = LabStore(root)
    obs = observation()
    store.put("observations", obs["observation_id"], obs)
    changed = dict(obs)
    changed["incident_class"] = "changed"
    changed = make_document({k: v for k, v in changed.items() if k != "sha256"})
    with pytest.raises(EvolutionError, match="identity collision"):
        store.put("observations", obs["observation_id"], changed)
    link = root / "candidates"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink privilege unavailable: {exc}")
    with pytest.raises(EvolutionError, match="symlink"):
        store.put("candidates", "x", propose(obs))


def test_store_detects_source_drift(tmp_path):
    store = LabStore(tmp_path / "lab")
    evidence = tmp_path / "report.md"
    evidence.write_text("before", encoding="utf-8")
    obs = observation()
    obs["evidence"][0]["source_path"] = str(evidence)
    obs["evidence"][0]["sha256"] = document_hash({"bytes": "before"})
    obs = make_document({k: v for k, v in obs.items() if k != "sha256"})
    evidence.write_text("after", encoding="utf-8")
    with pytest.raises(EvolutionError, match="drift"):
        store.verify_evidence(obs)


def _promote(root: str, expected: int, queue):
    try:
        pointer = LabStore(Path(root)).promote("version-1", expected_generation=expected, reason="race")
        queue.put(("ok", pointer["generation"]))
    except Exception as exc:
        queue.put(("error", str(exc)))


def test_promotion_rollback_and_generation_race(tmp_path):
    store = LabStore(tmp_path / "lab")
    obs = observation()
    candidate = propose(obs)
    evaluation = gate(obs, oracle(obs, candidate["sha256"]), candidate)
    version = store.register_passed(candidate, evaluation)
    pointer = store.promote(version["version_id"], expected_generation=0, reason="accept")
    assert pointer["generation"] == 1
    assert store.load_active()["version"]["version_id"] == version["version_id"]
    with pytest.raises(EvolutionError, match="generation"):
        store.promote(version["version_id"], expected_generation=0, reason="stale")

    store2 = LabStore(tmp_path / "race")
    version2 = store2.register_passed(candidate, evaluation, version_id="version-1")
    assert version2["version_id"] == "version-1"
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_promote, args=(str(tmp_path / "race"), 0, queue)) for _ in range(2)]
    for proc in procs: proc.start()
    for proc in procs: proc.join(20)
    results = [queue.get(timeout=2) for _ in procs]
    assert sum(result[0] == "ok" for result in results) == 1


def test_failure_before_replace_preserves_pointer_and_after_replace_reconciles(tmp_path):
    store = LabStore(tmp_path / "lab")
    obs = observation()
    candidate = propose(obs)
    evaluation = gate(obs, oracle(obs, candidate["sha256"]), candidate)
    version = store.register_passed(candidate, evaluation)
    with pytest.raises(EvolutionError, match="injected"):
        store.promote(version["version_id"], expected_generation=0, reason="test", fail_at="before_replace")
    assert not (store.root / "active.json").exists()
    with pytest.raises(EvolutionError, match="recovery-required"):
        store.promote(version["version_id"], expected_generation=0, reason="test", fail_at="after_replace")
    assert store.status()["active"]["generation"] == 1
    assert list((store.root / "history").glob("*.committed.json"))
