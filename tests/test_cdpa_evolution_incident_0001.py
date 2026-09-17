from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from playwright_auto.cdpa_evolution_lab import gate, validate_document


FIXTURE = Path(__file__).parent / "fixtures" / "cdpa_evolution" / "incident_0001"


def load(name: str):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def normalized(evaluation):
    return {
        "candidate_sha256": evaluation["candidate_sha256"],
        "candidate_predicates": evaluation["candidate_predicates"],
        "holdout_results": evaluation["holdout_results"],
        "invariant_results": evaluation["invariant_results"],
        "metrics": evaluation["metrics"],
        "verdict": evaluation["verdict"],
        "reasons": evaluation["reasons"],
    }


def test_incident_0001_reconstruction_and_controls_are_deterministic():
    observation = validate_document("observation", load("observation.json"))
    oracle = validate_document("oracle", load("oracle.json"))
    candidates = {
        name: validate_document("candidate", load(f"candidates/{name}.json"))
        for name in ("accepted_direction", "timeout_only", "reuse_unowned_tab", "all_timeouts")
    }
    runs = []
    for _ in range(3):
        results = {name: gate(observation, oracle, candidate) for name, candidate in candidates.items()}
        runs.append({name: normalized(result) for name, result in results.items()})
    assert runs[0] == runs[1] == runs[2]
    assert runs[0]["accepted_direction"]["verdict"] == "PASS"
    assert sum(item["verdict"] == "REJECT" for name, item in runs[0].items() if name != "accepted_direction") == 3


def test_cli_help_and_gate_json_contract():
    help_result = subprocess.run(
        [sys.executable, "-m", "playwright_auto.cdpa_evolution_cli", "--help"],
        text=True, capture_output=True, check=False,
    )
    assert help_result.returncode == 0
    for command in ("observe", "propose", "gate", "status", "promote", "rollback"):
        assert command in help_result.stdout
    assert all(token not in help_result.stdout for token in ("--command", "--module", "--script", "--hook"))

    result = subprocess.run(
        [
            sys.executable, "-m", "playwright_auto.cdpa_evolution_cli", "gate",
            "--observation", str(FIXTURE / "observation.json"),
            "--oracle", str(FIXTURE / "oracle.json"),
            "--candidate", str(FIXTURE / "candidates" / "timeout_only.json"),
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["verdict"] == "REJECT"


def test_lab_root_is_sibling_not_under_plan():
    assert ".plan" not in FIXTURE.parts
    repository = Path(__file__).resolve().parents[1]
    lab_root = repository.parent / ".cdpa-evolution"
    plans_root = repository / ".plan"
    assert plans_root not in lab_root.parents
