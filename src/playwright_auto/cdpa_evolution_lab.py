from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .file_lock import atomic_replace, exclusive_file_lock, fsync_parent_directory


class EvolutionError(ValueError):
    pass


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_HEX = re.compile(r"^[0-9a-f]+$")
_SCHEMA_FIELDS = {
    "observation": {
        "schema_version", "observation_id", "created_at", "incident_class",
        "source_identity", "evidence", "allowed_candidate_surfaces", "provenance", "sha256",
    },
    "oracle": {
        "schema_version", "oracle_id", "observation_sha256",
        "expected_diagnosis_predicates", "expected_candidate_classes",
        "accepted_direction_sha256", "holdouts", "hard_vetoes", "provenance", "sha256",
    },
    "candidate": {
        "schema_version", "candidate_id", "observation_sha256", "parent_version_id",
        "proposer", "diagnosis", "candidate_class", "rationale", "mutable_surface",
        "effects", "touched_path_labels", "known_limits", "created_at", "sha256",
    },
    "evaluation": {
        "schema_version", "evaluation_id", "observation_sha256", "oracle_sha256",
        "baseline_sha256", "candidate_sha256", "evaluator_version", "started_at",
        "finished_at", "baseline_predicates", "candidate_predicates", "holdout_results",
        "invariant_results", "metrics", "verdict", "reasons", "sha256",
    },
    "version": {
        "schema_version", "version_id", "candidate_sha256", "evaluation_sha256",
        "bundle_sha256", "parent_version_id", "verified_at", "sha256",
    },
    "pointer": {
        "schema_version", "generation", "active_version_id", "bundle_sha256",
        "decision_id", "sha256",
    },
    "decision": {
        "schema_version", "decision_id", "phase", "action", "from_version_id",
        "to_version_id", "expected_generation", "new_generation", "prior_pointer_sha256",
        "new_pointer_sha256", "evaluation_sha256", "operator_reason", "created_at", "sha256",
    },
}
_EXECUTABLE_FIELDS = {
    "command", "executable", "module", "hook", "script", "patch", "patch_bytes",
    "environment", "test_selector", "callable", "storage_target",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def document_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def make_document(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("sha256", None)
    result["sha256"] = document_hash(result)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_slug(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise EvolutionError(f"invalid {label}")


def validate_document(kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if kind not in _SCHEMA_FIELDS or not isinstance(value, Mapping):
        raise EvolutionError(f"invalid {kind} document")
    keys = set(value)
    expected = _SCHEMA_FIELDS[kind]
    missing = expected - keys
    unknown = keys - expected
    if missing:
        raise EvolutionError(f"missing fields: {sorted(missing)}")
    if unknown:
        if kind == "candidate" and unknown & _EXECUTABLE_FIELDS:
            raise EvolutionError("unsupported_executable_candidate")
        raise EvolutionError(f"unknown fields: {sorted(unknown)}")
    if value.get("schema_version") != 1:
        raise EvolutionError("unsupported schema_version")
    supplied = value.get("sha256")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise EvolutionError("invalid sha256")
    bare = {k: v for k, v in value.items() if k != "sha256"}
    if document_hash(bare) != supplied:
        raise EvolutionError("document hash mismatch")
    id_key = {
        "observation": "observation_id", "oracle": "oracle_id", "candidate": "candidate_id",
        "evaluation": "evaluation_id", "version": "version_id", "pointer": "decision_id",
        "decision": "decision_id",
    }[kind]
    _require_slug(value[id_key], id_key)
    if kind == "observation":
        serialized = canonical_bytes(value).decode("utf-8").lower()
        forbidden = ("oracle_", "expected_", "accepted_direction", "holdout_answer", '"verdict"', '"score"')
        if any(token in serialized for token in forbidden):
            raise EvolutionError("observation contains evaluator-owned data")
        if value["allowed_candidate_surfaces"] != ["policy_description"]:
            raise EvolutionError("unsupported candidate surface")
    if kind == "candidate":
        if value["mutable_surface"] != "policy_description":
            raise EvolutionError("unsupported candidate surface")
        serialized = canonical_bytes(value).decode("utf-8").lower()
        if any(f'"{field}"' in serialized for field in _EXECUTABLE_FIELDS):
            raise EvolutionError("unsupported_executable_candidate")
    return dict(value)


def _facts(observation: Mapping[str, Any]) -> dict[str, Any]:
    combined: dict[str, Any] = {}
    for item in observation.get("evidence", []):
        if isinstance(item, Mapping) and isinstance(item.get("facts"), Mapping):
            combined.update(item["facts"])
    return combined


def propose(observation: Mapping[str, Any]) -> dict[str, Any]:
    obs = validate_document("observation", observation)
    facts = _facts(obs)
    accepted_boundary = facts.get("send_state") in {"accepted", "ambiguous"}
    throttle = (
        facts.get("send_state") == "pre_send"
        and facts.get("allocation") == "fresh"
        and facts.get("provider_challenge") is True
        and facts.get("existing_page_ready") is True
        and facts.get("fresh_page_ready") is False
    )
    if accepted_boundary:
        candidate_class = "recovery_without_replay"
        diagnosis = "Accepted or ambiguous Send requires exact execution recovery without replay."
        effects = ["preserve_execution_identity", "forbid_replay"]
    elif throttle:
        candidate_class = "typed_acquisition_throttle"
        diagnosis = "Fresh allocation hit an explicit provider challenge while an existing page remained ready."
        effects = ["classify_fresh_acquisition_challenge", "enter_existing_cooldown_contract"]
    else:
        candidate_class = "unclassified_timeout"
        diagnosis = "Evidence does not justify mapping a generic timeout to throttle."
        effects = ["preserve_existing_classification"]
    candidate_id = f"{obs['observation_id']}-{candidate_class.replace('_', '-')}"
    return make_document({
        "schema_version": 1,
        "candidate_id": candidate_id,
        "observation_sha256": obs["sha256"],
        "parent_version_id": None,
        "proposer": {"id": "cdpa-deterministic-classifier", "version": "0", "deterministic": True},
        "diagnosis": diagnosis,
        "candidate_class": candidate_class,
        "rationale": "Classification is derived only from proposer-visible pre-fix facts.",
        "mutable_surface": "policy_description",
        "effects": effects,
        "touched_path_labels": [],
        "known_limits": ["static descriptive candidate", "no production activation"],
        "created_at": obs["created_at"],
    })


def gate(observation: Mapping[str, Any], oracle: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    started = _now()
    obs = validate_document("observation", observation)
    ora = validate_document("oracle", oracle)
    cand = validate_document("candidate", candidate)
    reasons: list[str] = []
    facts = _facts(obs)
    if ora["observation_sha256"] != obs["sha256"] or cand["observation_sha256"] != obs["sha256"]:
        reasons.append("source_drift")
    effects = set(cand["effects"])
    for veto in ora["hard_vetoes"]:
        if veto == "reuse_unowned_tab" and "reuse_unowned_tab" in effects:
            reasons.append(veto)
        elif veto == "replay_accepted_send" and ("replay_send" in effects or "resend" in effects):
            reasons.append(veto)
        elif veto == "executable_candidate" and any(field in cand for field in _EXECUTABLE_FIELDS):
            reasons.append(veto)
    candidate_predicates = {
        "fresh_failure": facts.get("allocation") == "fresh" and facts.get("send_state") == "pre_send",
        "explicit_challenge": facts.get("provider_challenge") is True,
        "readiness_contradiction": facts.get("existing_page_ready") is True and facts.get("fresh_page_ready") is False,
        "expected_class": cand["candidate_class"] in ora["expected_candidate_classes"],
        "accepted_direction": cand["sha256"] == ora["accepted_direction_sha256"],
    }
    baseline_predicates = {key: False for key in candidate_predicates}
    holdout_results = []
    for holdout in ora["holdouts"]:
        passed = True
        if holdout["expected_result"] == "not_throttle":
            passed = cand["candidate_class"] not in {"all_timeouts_are_throttle", "generic_timeout"}
        if holdout["expected_result"] == "no_replay":
            passed = not ({"replay_send", "resend"} & effects)
        holdout_results.append({"holdout_id": holdout["holdout_id"], "passed": passed})
        if not passed:
            reasons.append(f"holdout_failed:{holdout['holdout_id']}")
    if not all(candidate_predicates.values()):
        reasons.append("predicate_mismatch")
    invariant_results = [
        {"predicate": "descriptive_static_only", "passed": cand["mutable_surface"] == "policy_description", "evidence_sha256": cand["sha256"]},
        {"predicate": "accepted_send_non_replay", "passed": not ({"replay_send", "resend"} & effects), "evidence_sha256": ora["sha256"]},
        {"predicate": "user_owned_tab_unchanged", "passed": "reuse_unowned_tab" not in effects, "evidence_sha256": ora["sha256"]},
    ]
    if any(not item["passed"] for item in invariant_results):
        reasons.append("invariant_violation")
    reasons = sorted(set(reasons))
    verdict = "REJECT" if reasons else "PASS"
    finished = _now()
    return make_document({
        "schema_version": 1,
        "evaluation_id": f"eval-{cand['sha256'][:16]}",
        "observation_sha256": obs["sha256"],
        "oracle_sha256": ora["sha256"],
        "baseline_sha256": document_hash({"candidate_class": "no_structured_lab"}),
        "candidate_sha256": cand["sha256"],
        "evaluator_version": "cdpa-static-gate-v0",
        "started_at": started,
        "finished_at": finished,
        "baseline_predicates": baseline_predicates,
        "candidate_predicates": candidate_predicates,
        "holdout_results": holdout_results,
        "invariant_results": invariant_results,
        "metrics": {
            "accepted_direction_passed": verdict == "PASS",
            "holdouts_total": len(holdout_results),
            "holdouts_passed": sum(item["passed"] for item in holdout_results),
            "invariant_violations": sum(not item["passed"] for item in invariant_results),
        },
        "verdict": verdict,
        "reasons": reasons,
    })


class LabStore:
    NAMESPACES = {"observations", "oracles", "candidates", "evaluations", "versions", "history"}

    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        if any(part.lower() == ".plan" for part in self.root.parts):
            raise EvolutionError("lab root must not be inside .plan")
        self._reject_symlink_chain(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".mutation.lock"

    def _reject_symlink_chain(self, path: Path) -> None:
        current = path
        while current != current.parent:
            if current.exists() and current.is_symlink():
                raise EvolutionError(f"symlink rejected: {current}")
            current = current.parent

    def _target(self, namespace: str, identity: str) -> Path:
        if namespace not in self.NAMESPACES:
            raise EvolutionError("unowned namespace")
        _require_slug(identity, "identity")
        parent = self.root / namespace
        self._reject_symlink_chain(parent)
        if parent.exists() and parent.is_symlink():
            raise EvolutionError("symlink namespace rejected")
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / f"{identity}.json"
        self._reject_symlink_chain(target)
        if target.exists() and target.is_symlink():
            raise EvolutionError("symlink entry rejected")
        if self.root not in target.parents:
            raise EvolutionError("path containment failure")
        return target

    def put(self, namespace: str, identity: str, document: Mapping[str, Any]) -> Path:
        target = self._target(namespace, identity)
        payload = canonical_bytes(document)
        try:
            with target.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_parent_directory(target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise EvolutionError(f"immutable identity collision: {namespace}/{identity}")
        return target

    def _read(self, namespace: str, identity: str, kind: str) -> dict[str, Any]:
        target = self._target(namespace, identity)
        if not target.is_file():
            raise EvolutionError(f"missing {namespace}/{identity}")
        return validate_document(kind, json.loads(target.read_text(encoding="utf-8")))

    def verify_evidence(self, observation: Mapping[str, Any]) -> None:
        for item in observation.get("evidence", []):
            source = item.get("source_path")
            if source:
                data = Path(source).read_text(encoding="utf-8")
                if document_hash({"bytes": data}) != item["sha256"]:
                    raise EvolutionError("evidence drift")

    def register_passed(
        self, candidate: Mapping[str, Any], evaluation: Mapping[str, Any], *, version_id: str | None = None
    ) -> dict[str, Any]:
        cand = validate_document("candidate", candidate)
        ev = validate_document("evaluation", evaluation)
        if ev["verdict"] != "PASS" or ev["candidate_sha256"] != cand["sha256"]:
            raise EvolutionError("exact PASS evaluation required")
        self.put("candidates", cand["candidate_id"], cand)
        self.put("evaluations", ev["evaluation_id"], ev)
        version = make_document({
            "schema_version": 1,
            "version_id": version_id or f"version-{cand['sha256'][:16]}",
            "candidate_sha256": cand["sha256"],
            "evaluation_sha256": ev["sha256"],
            "bundle_sha256": document_hash({"candidate": cand["sha256"], "evaluation": ev["sha256"]}),
            "parent_version_id": cand["parent_version_id"],
            "verified_at": ev["finished_at"],
        })
        self.put("versions", version["version_id"], version)
        return version

    def _pointer(self) -> dict[str, Any] | None:
        path = self.root / "active.json"
        if not path.exists():
            return None
        self._reject_symlink_chain(path)
        return validate_document("pointer", json.loads(path.read_text(encoding="utf-8")))

    def _atomic_pointer(self, pointer: Mapping[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".active-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(canonical_bytes(pointer))
                handle.flush()
                os.fsync(handle.fileno())
            atomic_replace(name, self.root / "active.json")
            fsync_parent_directory(self.root / "active.json")
        finally:
            Path(name).unlink(missing_ok=True)

    def _committed(self, prepared: Mapping[str, Any]) -> dict[str, Any]:
        return make_document({**{k: v for k, v in prepared.items() if k != "sha256"}, "phase": "committed"})

    def _reconcile_locked(self) -> None:
        pointer = self._pointer()
        if not pointer or not pointer["decision_id"]:
            return
        prepared_path = self._target("history", f"{pointer['decision_id']}.prepared")
        committed_path = self._target("history", f"{pointer['decision_id']}.committed")
        if committed_path.exists() or not prepared_path.exists():
            return
        prepared = validate_document("decision", json.loads(prepared_path.read_text(encoding="utf-8")))
        if (
            prepared["new_pointer_sha256"] == pointer["sha256"]
            and prepared["to_version_id"] == pointer["active_version_id"]
            and prepared["new_generation"] == pointer["generation"]
        ):
            committed = self._committed(prepared)
            self.put("history", f"{pointer['decision_id']}.committed", committed)

    def status(self) -> dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            self._reconcile_locked()
            return {"active": self._pointer()}

    def _transition(
        self, version_id: str, *, expected_generation: int, reason: str,
        action: str, fail_at: str | None = None
    ) -> dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            self._reconcile_locked()
            prior = self._pointer()
            generation = prior["generation"] if prior else 0
            if generation != expected_generation:
                raise EvolutionError(f"generation mismatch: expected {expected_generation}, current {generation}")
            version = self._read("versions", version_id, "version")
            evaluation_id = f"eval-{version['candidate_sha256'][:16]}"
            evaluation = self._read("evaluations", evaluation_id, "evaluation")
            if evaluation["verdict"] != "PASS" or evaluation["sha256"] != version["evaluation_sha256"]:
                raise EvolutionError("version is not backed by exact PASS evaluation")
            decision_id = uuid.uuid4().hex
            pointer = make_document({
                "schema_version": 1,
                "generation": generation + 1,
                "active_version_id": version_id,
                "bundle_sha256": version["bundle_sha256"],
                "decision_id": decision_id,
            })
            prepared = make_document({
                "schema_version": 1,
                "decision_id": decision_id,
                "phase": "prepared",
                "action": action,
                "from_version_id": prior["active_version_id"] if prior else None,
                "to_version_id": version_id,
                "expected_generation": expected_generation,
                "new_generation": generation + 1,
                "prior_pointer_sha256": prior["sha256"] if prior else None,
                "new_pointer_sha256": pointer["sha256"],
                "evaluation_sha256": evaluation["sha256"],
                "operator_reason": reason,
                "created_at": _now(),
            })
            self.put("history", f"{decision_id}.prepared", prepared)
            if fail_at == "before_replace":
                raise EvolutionError("injected failure before_replace")
            self._atomic_pointer(pointer)
            if fail_at == "after_replace":
                raise EvolutionError("recovery-required: injected failure after_replace")
            self.put("history", f"{decision_id}.committed", self._committed(prepared))
            return pointer

    def promote(self, version_id: str, *, expected_generation: int, reason: str, fail_at: str | None = None) -> dict[str, Any]:
        return self._transition(version_id, expected_generation=expected_generation, reason=reason, action="promote", fail_at=fail_at)

    def rollback(self, version_id: str, *, expected_generation: int, reason: str, fail_at: str | None = None) -> dict[str, Any]:
        return self._transition(version_id, expected_generation=expected_generation, reason=reason, action="rollback", fail_at=fail_at)

    def load_active(self) -> dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            self._reconcile_locked()
            pointer = self._pointer()
            if not pointer:
                raise EvolutionError("no active version")
            version = self._read("versions", pointer["active_version_id"], "version")
            if version["bundle_sha256"] != pointer["bundle_sha256"]:
                raise EvolutionError("active bundle drift")
            candidate = self._read("candidates", f"{version['version_id'].removeprefix('version-')}", "candidate") if False else None
            return {"pointer": pointer, "version": version}
