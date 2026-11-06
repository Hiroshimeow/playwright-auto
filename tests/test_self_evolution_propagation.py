"""Regression: canonical launcher propagates real Solo goal across many generations."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "workflows/self-evolution/core.py"
SPAWN_PATH = ROOT / "workflows/self-evolution/spawn_cdpa.py"
sys.path.insert(0, str(SPAWN_PATH.parent))

spec = importlib.util.spec_from_file_location("self_evolution_core", CORE_PATH)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
spawn_spec = importlib.util.spec_from_file_location("self_evolution_spawn", SPAWN_PATH)
spawn = importlib.util.module_from_spec(spawn_spec)
spawn_spec.loader.exec_module(spawn)


def test_fifty_generations_preserve_real_solo_goal():
    from playwright_auto.cdpa_prompts import PromptBuilder
    from playwright_auto.cdpa_config import load_cdpa_config

    config = load_cdpa_config(str(ROOT / "cdpa.yaml"), repository_root=ROOT)
    builder = PromptBuilder(config)
    parent = "root"
    for generation in range(50):
        team = f"evolution-gen-{generation}"
        instruction = f"Check safe continuation generation={generation}"
        argv = spawn.command("DEV", team, instruction, parent)
        goal = argv[argv.index("--goal") + 1]
        title = argv[argv.index("--task") + 1]
        actual = builder.build_solo(task_name=title, logical_role="DEV", goal=goal).text
        assert goal.count(core.canonical_goal()) == 1
        assert "Goal: # CDPA Self-Evolution" in actual
        assert core.canonical_goal() in actual
        assert str(core.CONTRACT.resolve()) in actual
        assert f"Parent task: {parent}" in actual
        assert f"Current bounded instruction: {instruction}" in actual
        assert "spawn_cdpa.py" in actual
        assert "9202_PLUSHCU.bat" in actual
        assert len(actual.encode("utf-8")) < 20000
        parent = f"cdpa-idem-test-{generation}"


def test_spawn_command_contains_full_policy():
    cmd = spawn.command("REVIEW", "evolution-review-01", "Audit root cause", "parent-1")
    assert cmd[0].endswith("python.exe")
    assert cmd[1:3] == ["-m", "playwright_auto.cdpa_cli"]
    assert cmd[cmd.index("--config") + 1].endswith("cdpa.yaml")
    assert cmd[cmd.index("--browser") + 1] == "9202"
    assert cmd[cmd.index("--task") + 1] == "CDPA Self-Evolution REVIEW: evolution-review-01"
    assert cmd[cmd.index("--goal") + 1].startswith(core.canonical_goal())
    assert "Audit root cause" in cmd[cmd.index("--goal") + 1]
    assert "parent-1" in cmd[cmd.index("--goal") + 1]


def test_first_solo_turn_receives_canonical_goal_not_just_title():
    from playwright_auto.cdpa_prompts import PromptBuilder
    from playwright_auto.cdpa_config import load_cdpa_config

    argv = spawn.command("DEV", "campaign-dev-5", "Fix the root cause", "parent-4")
    prompt = argv[argv.index("--goal") + 1]
    assert argv[argv.index("--task") + 1].startswith("CDPA Self-Evolution DEV")
    assert core.canonical_goal() in prompt
    config = load_cdpa_config(str(ROOT / "cdpa.yaml"), repository_root=ROOT)
    first_turn = PromptBuilder(config).build_solo(
        task_name=argv[argv.index("--task") + 1], logical_role="DEV", goal=prompt,
    ).text
    assert "Goal: # CDPA Self-Evolution" in first_turn
    assert core.canonical_goal() in first_turn
    assert "Current bounded instruction: Fix the root cause" in first_turn
