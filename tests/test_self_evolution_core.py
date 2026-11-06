"""Tests for canonical campaign prompt and read-only CDPA health utilities."""
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "workflows/self-evolution/core.py"
spec = importlib.util.spec_from_file_location("self_evolution_core", MODULE)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def test_only_canonical_prompt_and_health_owners_remain():
    for obsolete in ("DB", "connect", "event", "enqueue", "claim", "handoff",
                     "tick", "status", "main"):
        assert not hasattr(core, obsolete), f"duplicate coordinator state: {obsolete}"


def test_bounded_prompt_includes_complete_policy_path_parent_and_instruction():
    prompt = core.task_prompt("solo_g6", "Inspect safe 9202 ownership", "parent-17")
    assert core.canonical_goal() in prompt
    assert str(core.CONTRACT.resolve()) in prompt
    assert "MANDATORY SPAWN COMMAND RULE" in prompt
    assert "9202_PLUSHCU.bat" in prompt
    assert "Parent task: parent-17" in prompt
    assert "Current bounded instruction: Inspect safe 9202 ownership" in prompt


@pytest.mark.parametrize("kind,brief", [("invalid", "fix"), ("solo_g6", "   ")])
def test_invalid_tasks_rejected(kind, brief):
    with pytest.raises(ValueError):
        core.task_prompt(kind, brief)


def test_health_requires_all_worker_readiness_and_no_drift(monkeypatch):
    import io
    import json

    flags = dict(ok=True, worker_online=True, worker_ready=True,
                 worker_runtime_ready=True, worker_command_loop_ready=True,
                 worker_source_drift={"drifted": False}, worker_status="HEALTHY")

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(core, "urlopen", lambda *a, **k: Response(json.dumps(flags).encode()))
    assert core.health() == {"ready": True, "worker": "HEALTHY"}
    flags["worker_source_drift"]["drifted"] = True
    assert core.health()["ready"] is False
