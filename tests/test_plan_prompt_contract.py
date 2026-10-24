from pathlib import Path


def test_plan_prompt_does_not_self_route_while_waiting_for_resume():
    prompt = Path("prompts/cdpa/PLAN.md").read_text(encoding="utf-8")

    assert "Do not route PLAN to PLAN merely to wait" in prompt


def test_plan_prompt_does_not_self_route_as_quiescent_hold():
    prompt = Path("prompts/cdpa/PLAN.md").read_text(encoding="utf-8")

    assert "only when the per-turn RouteContract declares this role as `closing_role`" in prompt
