from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cdpa_evolution_lab import EvolutionError, LabStore, gate, propose, validate_document


def _load(path: str, kind: str) -> dict[str, Any]:
    return validate_document(kind, json.loads(Path(path).read_text(encoding="utf-8")))


def _root(value: str | None) -> Path:
    return Path(value).absolute() if value else Path.cwd().resolve().parent / ".cdpa-evolution"


def _emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cdpa-evolve", description="Offline static CDPA Evolution Lab v0")
    sub = parser.add_subparsers(dest="action", required=True)

    observe_parser = sub.add_parser("observe", help="validate and store a proposer-visible observation")
    observe_parser.add_argument("--observation", required=True)
    observe_parser.add_argument("--root")

    propose_parser = sub.add_parser("propose", help="derive a deterministic descriptive candidate")
    propose_parser.add_argument("--observation", required=True)
    propose_parser.add_argument("--root")

    gate_parser = sub.add_parser("gate", help="evaluate static candidate data")
    gate_parser.add_argument("--observation", required=True)
    gate_parser.add_argument("--oracle", required=True)
    gate_parser.add_argument("--candidate", required=True)
    gate_parser.add_argument("--root")

    status_parser = sub.add_parser("status", help="show the verified lab pointer")
    status_parser.add_argument("--root")

    for name in ("promote", "rollback"):
        transition = sub.add_parser(name, help=f"{name} an immutable verified lab version")
        transition.add_argument("--version", required=True)
        transition.add_argument("--expected-generation", required=True, type=int)
        transition.add_argument("--reason", required=True)
        transition.add_argument("--root")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.action == "observe":
        observation = _load(args.observation, "observation")
        LabStore(_root(args.root)).put("observations", observation["observation_id"], observation)
        _emit(observation)
        return 0
    if args.action == "propose":
        observation = _load(args.observation, "observation")
        candidate = propose(observation)
        LabStore(_root(args.root)).put("candidates", candidate["candidate_id"], candidate)
        _emit(candidate)
        return 0
    if args.action == "gate":
        evaluation = gate(
            _load(args.observation, "observation"),
            _load(args.oracle, "oracle"),
            _load(args.candidate, "candidate"),
        )
        if args.root:
            LabStore(_root(args.root)).put("evaluations", evaluation["evaluation_id"], evaluation)
        _emit(evaluation)
        return 0 if evaluation["verdict"] == "PASS" else 2
    store = LabStore(_root(args.root))
    if args.action == "status":
        _emit(store.status())
        return 0
    transition = store.promote if args.action == "promote" else store.rollback
    pointer = transition(args.version, expected_generation=args.expected_generation, reason=args.reason)
    _emit(pointer)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except (EvolutionError, OSError, json.JSONDecodeError) as exc:
        _emit({"error": type(exc).__name__, "message": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
