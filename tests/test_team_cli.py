import argparse
import asyncio
import json
from pathlib import Path

import pytest

import playwright_auto.team_cli as team_cli


def args(**overrides):
    values = {
        "goal": None,
        "team": None,
        "task_id": None,
        "resume": None,
        "workflow_version": "1",
        "cdp": "http://127.0.0.1:9222",
        "allow_guest": False,
        "dry_run": False,
        "json_output": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_parse_team_spec_defaults_and_overrides():
    assert team_cli.parse_team_spec(None) == {
        "PLAN": 1,
        "DEV": 2,
        "REVIEW": 1,
        "TEST": 1,
    }
    assert team_cli.parse_team_spec("DEV=3, REVIEW=2, TEST=0") == {
        "PLAN": 1,
        "DEV": 3,
        "REVIEW": 2,
    }


def test_team_validation_is_exact_for_saved_manifests():
    assert team_cli.validate_team_mapping(
        {"PLAN": 1, "DEV": 1, "TEST": 2}
    ) == {"PLAN": 1, "DEV": 1, "TEST": 2}

    with pytest.raises(ValueError, match="PLAN=1"):
        team_cli.validate_team_mapping({"DEV": 1, "TEST": 1})
    with pytest.raises(ValueError, match="at least DEV=1"):
        team_cli.validate_team_mapping({"PLAN": 1, "TEST": 1})
    with pytest.raises(ValueError, match="REVIEW or TEST"):
        team_cli.validate_team_mapping({"PLAN": 1, "DEV": 1})
    with pytest.raises(TypeError, match="integer"):
        team_cli.validate_team_mapping({"PLAN": 1, "DEV": True, "TEST": 1})
    with pytest.raises(ValueError, match="unsupported"):
        team_cli.validate_team_mapping({"PLAN": 1, "DEV": 1, "OTHER": 1})


def test_task_id_validation_and_slug():
    assert team_cli.validate_task_id("TASK-1.alpha") == "TASK-1.alpha"
    assert team_cli.slugify("Kiểm tra multi role!") == "ki-m-tra-multi-role"
    with pytest.raises(ValueError, match="task ID"):
        team_cli.validate_task_id("../unsafe")


def test_dry_run_writes_manifest_and_resume_uses_exact_saved_team(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(team_cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(team_cli, "viewer_url", lambda: "http://100.64.0.1:9223/")

    code = asyncio.run(
        team_cli.run(
            args(
                goal="Implement stable team flow",
                team="DEV=3,REVIEW=2,TEST=0",
                task_id="cli-dry-run",
                dry_run=True,
            )
        )
    )
    assert code == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "dry_run"
    assert first["roles"] == ["PLAN", "DEV", "DEV1", "DEV2", "REVIEW", "REVIEW1"]

    manifest = json.loads(
        (tmp_path / ".runtime/team-tasks/cli-dry-run.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["team"] == {"PLAN": 1, "DEV": 3, "REVIEW": 2}

    code = asyncio.run(team_cli.run(args(resume="cli-dry-run", dry_run=True)))
    assert code == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["team"] == manifest["team"]
    assert resumed["roles"] == first["roles"]


def test_guest_gate_persists_task_and_prints_resume_command(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(team_cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(team_cli, "viewer_url", lambda: "http://100.64.0.1:9223/")

    async def fake_auth(_cdp):
        return False, "ChatGPT profile is not logged in"

    monkeypatch.setattr(team_cli, "authenticated_profile", fake_auth)

    code = asyncio.run(
        team_cli.run(
            args(goal="Guest gate", task_id="guest-gate", dry_run=False)
        )
    )
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "waiting_for_login"
    assert payload["viewer"] == "http://100.64.0.1:9223/"
    assert payload["next_command"] == "uv run playwright-team --resume guest-gate"
    assert Path(payload["manifest"]).is_file()


def test_cli_defaults_new_tasks_to_workflow_v2():
    parser = team_cli.build_parser()
    parsed = parser.parse_args(["goal"])
    assert parsed.workflow_version == "2"


def test_extract_final_report_and_task_outcome_from_runner_variables():
    report = "Completed work\nTASK_STATUS: COMPLETED"
    payload = {
        "status": "completed",
        "variables": {
            "team_transcript": {
                "rounds": {
                    "closeout": {
                        "PLAN": {
                            "result": {"response": {"text": report}}
                        }
                    }
                }
            }
        },
    }
    assert team_cli.extract_final_report(payload) == report
    assert team_cli.extract_task_outcome(report) == "completed"
    assert team_cli.extract_task_outcome("TASK_STATUS: BLOCKED") == "blocked"
    assert team_cli.extract_task_outcome("no status") is None
    assert team_cli.extract_task_outcome(
        "TASK_STATUS: COMPLETED\nTASK_STATUS: BLOCKED"
    ) is None


def test_json_output_surfaces_status_outcome_and_final_report(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(team_cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(team_cli, "viewer_url", lambda: "http://100.64.0.1:9223/")

    async def fake_auth(_cdp):
        return True, "authenticated"

    report = "Verified result\nTASK_STATUS: COMPLETED"
    runner_payload = {
        "status": "completed",
        "variables": {
            "team_transcript": {
                "rounds": {
                    "closeout": {
                        "PLAN": {
                            "result": {"response": {"text": report}}
                        }
                    }
                }
            }
        },
        "iterations": [],
    }

    async def fake_run(_path, _cdp):
        return 0, runner_payload

    monkeypatch.setattr(team_cli, "authenticated_profile", fake_auth)
    monkeypatch.setattr(team_cli, "run_chatgpt_loop", fake_run)

    code = asyncio.run(
        team_cli.run(
            args(
                goal="JSON output",
                task_id="json-output",
                workflow_version="2",
                json_output=True,
            )
        )
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["task_outcome"] == "completed"
    assert output["final_report"] == report
    assert output["workflow_version"] == "2"
    assert output["result"] == runner_payload


def test_workflow_version_validation():
    assert team_cli.validate_workflow_version(1) == "1"
    assert team_cli.validate_workflow_version("2") == "2"
    with pytest.raises(ValueError, match="version"):
        team_cli.validate_workflow_version("3")
