from pathlib import Path


def test_plan_prompt_does_not_self_route_while_waiting_for_resume():
    prompt = Path("prompts/cdpa/PLAN.md").read_text(encoding="utf-8")

    assert "Never use `PLAN -> PLAN` merely to wait for an operator Resume" in prompt
    assert "receiving a normal PLAN turn after such a PAUSED gate is evidence that the controller released that gate" in prompt
    assert "stale PAUSED wording in the inherited task or handoff" in prompt


def test_plan_prompt_does_not_self_route_as_quiescent_hold():
    prompt = Path("prompts/cdpa/PLAN.md").read_text(encoding="utf-8")

    assert "Never use `PLAN -> PLAN` as a quiescent hold" in prompt
    assert "`DONE` is the workflow lifecycle terminal, not a synonym for PASS" in prompt
    assert "PASS, FAIL, or BLOCKED/INCOMPLETE" in prompt
    assert "must not create an endless PLAN self-route" in prompt
